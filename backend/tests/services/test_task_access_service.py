"""Tests for task access helpers, including tenant-aware ownership checks."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.tenant import Tenant, TenantMembership
from backend.services.task.access_service import (
    get_tenant_task,
    get_tenant_task_or_404,
    get_tenant_task_with_engagement,
    get_tenant_task_with_engagement_or_404,
    get_owned_task,
    get_owned_task_or_404,
    get_owned_task_with_engagement,
    get_owned_task_with_engagement_or_404,
    list_tenant_tasks_for_user,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, username: str) -> User:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    return user


def _seed_tenant(db, tenant_id: int, slug: str) -> Tenant:
    tenant = Tenant(id=tenant_id, slug=slug, name=f"{slug}-name")
    db.add(tenant)
    db.flush()
    return tenant


def _seed_membership(db, tenant_id: int, user_id: int) -> TenantMembership:
    membership = TenantMembership(tenant_id=tenant_id, user_id=user_id, role="owner")
    db.add(membership)
    db.flush()
    return membership


def test_get_tenant_task_allows_same_tenant_same_user() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "tenant-access-owner")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        task = Task(user_id=owner.id, tenant_id=1, name="Owned Task")
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_tenant_task(db=db, task_id=task.id, user_id=owner.id, tenant_id=1)

        assert resolved is not None
        assert resolved.id == task.id
        assert resolved.user_id == owner.id
        assert resolved.tenant_id == 1
    finally:
        db.close()
        engine.dispose()


def test_get_tenant_task_rejects_same_tenant_different_user() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "tenant-access-owner-2")
        foreign = _seed_user(db, "tenant-access-foreign-2")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=1, user_id=foreign.id)
        task = Task(user_id=owner.id, tenant_id=1, name="Owned Task")
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_tenant_task(db=db, task_id=task.id, user_id=foreign.id, tenant_id=1)

        assert resolved is None
    finally:
        db.close()
        engine.dispose()


def test_list_tenant_tasks_for_user_excludes_same_tenant_teammate_tasks() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "tenant-list-owner")
        teammate = _seed_user(db, "tenant-list-teammate")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=1, user_id=teammate.id)
        owner_task = Task(user_id=owner.id, tenant_id=1, name="Owner Task")
        teammate_task = Task(user_id=teammate.id, tenant_id=1, name="Teammate Task")
        db.add_all([owner_task, teammate_task])
        db.commit()

        rows = list_tenant_tasks_for_user(db=db, tenant_id=1, user_id=owner.id)

        assert {row.id for row in rows} == {owner_task.id}
    finally:
        db.close()
        engine.dispose()


def test_get_tenant_task_rejects_foreign_tenant_for_same_user() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owner-foreign-tenant")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        task = Task(user_id=owner.id, tenant_id=2, name="Foreign Tenant Task")
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_tenant_task(db=db, task_id=task.id, user_id=owner.id, tenant_id=1)

        assert resolved is None
    finally:
        db.close()
        engine.dispose()


def test_get_tenant_task_with_engagement_rejects_cross_tenant_engagement_link() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owner-engagement-mismatch")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        engagement = Engagement(user_id=owner.id, tenant_id=2, name="Foreign Engagement", status="active")
        db.add(engagement)
        db.flush()
        task = Task(
            user_id=owner.id,
            tenant_id=1,
            engagement_id=engagement.id,
            name="Mismatched Tenant Task",
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_tenant_task_with_engagement(db=db, task_id=task.id, user_id=owner.id, tenant_id=1)

        assert resolved is None
    finally:
        db.close()
        engine.dispose()


def test_get_tenant_task_or_404_raises_for_missing_task() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "tenant-access-missing-task")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        db.commit()

        try:
            get_tenant_task_or_404(db=db, task_id=999_999, user_id=owner.id, tenant_id=1)
            assert False, "Expected HTTPException for missing task"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Task not found"
    finally:
        db.close()
        engine.dispose()


def test_get_tenant_task_with_engagement_or_404_raises_for_cross_tenant_engagement() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owner-engagement-mismatch-404")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        engagement = Engagement(
            user_id=owner.id,
            tenant_id=2,
            name="Foreign Engagement",
            status="active",
        )
        db.add(engagement)
        db.flush()
        task = Task(
            user_id=owner.id,
            tenant_id=1,
            engagement_id=engagement.id,
            name="Mismatched Tenant Task",
        )
        db.add(task)
        db.commit()

        try:
            get_tenant_task_with_engagement_or_404(db=db, task_id=task.id, user_id=owner.id, tenant_id=1)
            assert False, "Expected HTTPException for cross-tenant engagement link"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Task not found"
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_respects_tenant_scoped_resolution() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owned-wrapper")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=2, user_id=owner.id)
        foreign_tenant_task = Task(user_id=owner.id, tenant_id=2, name="Secondary Tenant Task")
        db.add(foreign_tenant_task)
        db.commit()
        db.refresh(foreign_tenant_task)

        resolved = get_owned_task(
            db=db,
            task_id=foreign_tenant_task.id,
            user_id=owner.id,
            tenant_id=1,
        )

        assert resolved is None
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_accepts_explicit_active_tenant_scope() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owned-explicit")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=2, user_id=owner.id)
        secondary_task = Task(user_id=owner.id, tenant_id=2, name="Secondary Tenant Task")
        db.add(secondary_task)
        db.commit()
        db.refresh(secondary_task)

        resolved = get_owned_task(
            db=db,
            task_id=secondary_task.id,
            user_id=owner.id,
            tenant_id=2,
        )

        assert resolved is not None
        assert resolved.id == secondary_task.id
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_or_404_raises_for_foreign_tenant_task() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        _seed_tenant(db, tenant_id=2, slug="secondary")
        owner = _seed_user(db, "tenant-access-owned-wrapper-404")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=2, user_id=owner.id)
        foreign_tenant_task = Task(user_id=owner.id, tenant_id=2, name="Secondary Tenant Task")
        db.add(foreign_tenant_task)
        db.commit()
        db.refresh(foreign_tenant_task)

        try:
            get_owned_task_or_404(
                db=db,
                task_id=foreign_tenant_task.id,
                user_id=owner.id,
                tenant_id=1,
            )
            assert False, "Expected HTTPException for foreign-tenant task via compatibility wrapper"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Task not found"
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_with_engagement_returns_owned_task() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "task-access-owner")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        engagement = Engagement(user_id=owner.id, tenant_id=1, name="Owned Engagement", status="active")
        db.add(engagement)
        db.flush()
        task = Task(user_id=owner.id, tenant_id=1, engagement_id=engagement.id, name="Owned Task")
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_owned_task_with_engagement(db=db, task_id=task.id, user_id=owner.id, tenant_id=1)

        assert resolved is not None
        assert resolved.id == task.id
        assert resolved.engagement_id == engagement.id
        assert resolved.engagement is not None
        assert resolved.engagement.name == "Owned Engagement"
        assert "engagement" in resolved.__dict__
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_with_engagement_returns_none_for_foreign_user() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        owner = _seed_user(db, "task-access-owner-foreign")
        foreign = _seed_user(db, "task-access-foreign-user")
        _seed_membership(db, tenant_id=1, user_id=owner.id)
        _seed_membership(db, tenant_id=1, user_id=foreign.id)
        engagement = Engagement(user_id=owner.id, tenant_id=1, name="Owned Engagement", status="active")
        db.add(engagement)
        db.flush()
        task = Task(user_id=owner.id, tenant_id=1, engagement_id=engagement.id, name="Owned Task")
        db.add(task)
        db.commit()
        db.refresh(task)

        resolved = get_owned_task_with_engagement(db=db, task_id=task.id, user_id=foreign.id, tenant_id=1)
        assert resolved is None
    finally:
        db.close()
        engine.dispose()


def test_get_owned_task_with_engagement_or_404_raises_for_missing_task() -> None:
    engine, db = _build_session()
    try:
        _seed_tenant(db, tenant_id=1, slug="default")
        user = _seed_user(db, "task-access-missing")
        _seed_membership(db, tenant_id=1, user_id=user.id)
        db.commit()

        try:
            get_owned_task_with_engagement_or_404(db=db, task_id=999_999, user_id=user.id, tenant_id=1)
            assert False, "Expected HTTPException for missing task"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Task not found"
    finally:
        db.close()
        engine.dispose()
