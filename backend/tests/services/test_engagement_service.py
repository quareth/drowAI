"""Tests for engagement ownership resolution during task creation."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.services.engagement.service import EngagementService


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


def test_resolve_for_task_creation_auto_creates_when_id_missing() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "engagement-owner-auto")
        service = EngagementService(db)

        engagement = service.resolve_for_task_creation(
            user_id=user.id,
            task_name="Tenant Baseline Task",
            task_description="Task description",
            requested_engagement_id=None,
        )

        db.commit()
        db.refresh(engagement)

        assert engagement.id is not None
        assert engagement.user_id == user.id
        assert engagement.name == "Tenant Baseline Task"
        assert engagement.description == "Task description"
        assert engagement.status == "active"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_uses_explicit_owner_engagement() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "engagement-owner-explicit")
        engagement = Engagement(user_id=user.id, name="Existing", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementService(db)
        resolved = service.resolve_for_task_creation(
            user_id=user.id,
            task_name="Ignored",
            task_description=None,
            requested_engagement_id=engagement.id,
        )

        assert resolved.id == engagement.id
        assert resolved.user_id == user.id
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_rejects_cross_user_engagement() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "engagement-owner-cross")
        other_user = _seed_user(db, "engagement-other-cross")
        engagement = Engagement(user_id=other_user.id, name="Other", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementService(db)
        try:
            service.resolve_for_task_creation(
                user_id=owner.id,
                task_name="task",
                task_description=None,
                requested_engagement_id=engagement.id,
            )
            assert False, "Expected HTTPException for cross-user engagement attach"
        except HTTPException as exc:
            assert exc.status_code == 403
            assert "does not belong" in str(exc.detail)
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_allows_cross_user_when_tenant_matches() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "engagement-owner-tenant-match")
        other_user = _seed_user(db, "engagement-other-tenant-match")
        engagement = Engagement(user_id=other_user.id, tenant_id=7, name="Shared", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementService(db)
        resolved = service.resolve_for_task_creation(
            user_id=owner.id,
            task_name="task",
            task_description=None,
            requested_engagement_id=engagement.id,
            expected_tenant_id=7,
        )

        assert resolved.id == engagement.id
        assert resolved.user_id == other_user.id
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_rejects_archived_engagement() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "engagement-owner-archived")
        engagement = Engagement(user_id=user.id, name="Archived", status="archived")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementService(db)
        try:
            service.resolve_for_task_creation(
                user_id=user.id,
                task_name="task",
                task_description=None,
                requested_engagement_id=engagement.id,
            )
            assert False, "Expected HTTPException for archived engagement attach"
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "archived engagement" in str(exc.detail)
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_rejects_missing_engagement_id() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "engagement-owner-missing")
        service = EngagementService(db)

        try:
            service.resolve_for_task_creation(
                user_id=user.id,
                task_name="task",
                task_description=None,
                requested_engagement_id=999999,
            )
            assert False, "Expected HTTPException for missing engagement"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Engagement not found"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_task_creation_locks_explicit_engagement_row() -> None:
    captured_stmt = {"value": None}
    expected_engagement = SimpleNamespace(id=10, user_id=7, status="active")

    class _ScalarResult:
        def scalar_one_or_none(self):
            return expected_engagement

    class _FakeDB:
        def execute(self, stmt):
            captured_stmt["value"] = stmt
            return _ScalarResult()

        def add(self, _obj):
            raise AssertionError("add should not run for explicit engagement resolve")

    service = EngagementService(_FakeDB())
    resolved = service.resolve_for_task_creation(
        user_id=7,
        task_name="task",
        task_description=None,
        requested_engagement_id=10,
    )

    assert resolved is expected_engagement
    assert getattr(captured_stmt["value"], "_for_update_arg", None) is not None


def test_resolve_for_task_creation_rejects_cross_tenant_engagement_attach() -> None:
    expected_engagement = SimpleNamespace(id=10, user_id=7, tenant_id=2, status="active")

    class _ScalarResult:
        def scalar_one_or_none(self):
            return expected_engagement

    class _FakeDB:
        def execute(self, _stmt):
            return _ScalarResult()

        def add(self, _obj):
            raise AssertionError("add should not run for explicit engagement resolve")

    service = EngagementService(_FakeDB())
    try:
        service.resolve_for_task_creation(
            user_id=7,
            task_name="task",
            task_description=None,
            requested_engagement_id=10,
            expected_tenant_id=1,
        )
        assert False, "Expected HTTPException for cross-tenant engagement attach"
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "Cross-tenant" in str(exc.detail)
