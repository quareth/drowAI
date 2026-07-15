"""Tests for engagement management service archive/restore transition rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.services.engagement.management_service import EngagementManagementService


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


def test_archive_engagement_sets_archived_when_no_tasks() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "eng-mgmt-owner")
        engagement = Engagement(user_id=owner.id, tenant_id=301, name="Owned", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        archived = service.archive_engagement(engagement_id=engagement.id, tenant_id=engagement.tenant_id)

        assert archived.id == engagement.id
        assert archived.status == "archived"
    finally:
        db.close()
        engine.dispose()


def test_archive_engagement_allows_stopped_only_tasks() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "eng-mgmt-owner-stopped-only")
        engagement = Engagement(user_id=owner.id, tenant_id=302, name="Owned", status="active")
        db.add(engagement)
        db.flush()
        db.add(
            Task(
                user_id=owner.id,
                engagement_id=engagement.id,
                name="Stopped Task",
                status="stopped",
            )
        )
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        archived = service.archive_engagement(engagement_id=engagement.id, tenant_id=engagement.tenant_id)

        assert archived.id == engagement.id
        assert archived.status == "archived"
    finally:
        db.close()
        engine.dispose()


def test_archive_engagement_allows_created_only_tasks() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "eng-mgmt-owner-created-only")
        engagement = Engagement(user_id=owner.id, tenant_id=303, name="Owned", status="active")
        db.add(engagement)
        db.flush()
        db.add(
            Task(
                user_id=owner.id,
                engagement_id=engagement.id,
                name="Created Task",
                status="created",
            )
        )
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        archived = service.archive_engagement(engagement_id=engagement.id, tenant_id=engagement.tenant_id)

        assert archived.id == engagement.id
        assert archived.status == "archived"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize("runtime_active_status", ["queued", "running", "paused"])
def test_archive_engagement_rejects_when_runtime_active_tasks_exist(runtime_active_status: str) -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, f"eng-mgmt-owner-with-{runtime_active_status}")
        engagement = Engagement(user_id=owner.id, tenant_id=304, name="Owned", status="active")
        db.add(engagement)
        db.flush()
        db.add(
            Task(
                user_id=owner.id,
                engagement_id=engagement.id,
                name=f"{runtime_active_status} task",
                status=runtime_active_status,
            )
        )
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        with pytest.raises(HTTPException) as exc_info:
            service.archive_engagement(engagement_id=engagement.id, tenant_id=engagement.tenant_id)

        assert exc_info.value.status_code == 409
        assert "runtime-active tasks" in str(exc_info.value.detail)
        assert "Stop/retire active tasks" in str(exc_info.value.detail)
    finally:
        db.close()
        engine.dispose()


def test_restore_engagement_sets_active() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "eng-mgmt-owner-restore")
        engagement = Engagement(user_id=owner.id, tenant_id=305, name="Owned", status="archived")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        restored = service.restore_engagement(engagement_id=engagement.id, tenant_id=engagement.tenant_id)

        assert restored.id == engagement.id
        assert restored.status == "active"
    finally:
        db.close()
        engine.dispose()


def test_archive_engagement_raises_404_for_foreign_tenant() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "eng-mgmt-owner-foreign")
        _seed_user(db, "eng-mgmt-foreign")
        engagement = Engagement(user_id=owner.id, tenant_id=401, name="Owned", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        service = EngagementManagementService(db)
        try:
            service.archive_engagement(engagement_id=engagement.id, tenant_id=402)
            assert False, "Expected HTTPException for foreign tenant engagement access"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Engagement not found"
    finally:
        db.close()
        engine.dispose()


def test_archive_engagement_requests_row_lock(monkeypatch) -> None:
    lock_flags: list[bool] = []
    engagement = SimpleNamespace(id=33, status="active")

    class _FakeDB:
        def commit(self) -> None:
            pass

        def refresh(self, _engagement) -> None:
            pass

    def _fake_lookup(*, db, engagement_id: int, tenant_id: int, lock_for_update: bool = False):
        assert engagement_id == 33
        assert tenant_id == 77
        lock_flags.append(lock_for_update)
        return engagement

    monkeypatch.setattr(
        "backend.services.engagement.management_service.get_engagement_in_tenant_or_404",
        _fake_lookup,
    )

    service = EngagementManagementService(_FakeDB())
    monkeypatch.setattr(service, "_engagement_has_runtime_active_tasks", lambda *, engagement_id: False)
    archived = service.archive_engagement(engagement_id=33, tenant_id=77)

    assert archived is engagement
    assert lock_flags == [True]
