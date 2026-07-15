"""Router tests for engagement creation CRUD endpoint behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# Ensure backend.models package initialization completes before auth import path.
import backend.models  # noqa: F401
from backend.routers import engagements_crud


class _FakeDBSession:
    def __init__(self) -> None:
        self._next_id = 100
        self.added = None
        self.committed = False

    def add(self, obj) -> None:
        self.added = obj

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj) -> None:
        now = datetime.now(timezone.utc)
        obj.id = self._next_id
        self._next_id += 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = now
        obj.updated_at = now


def _build_app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(engagements_crud.router)

    fake_db = _FakeDBSession()

    def fake_db_dep():
        yield fake_db

    app.dependency_overrides[engagements_crud.get_db] = fake_db_dep

    if authenticated:
        app.dependency_overrides[engagements_crud.get_current_user] = (
            lambda: SimpleNamespace(id=7, username="owner", is_active=True)
        )
        app.dependency_overrides[engagements_crud.get_tenant_request_context] = (
            lambda: SimpleNamespace(tenant_id=1, user_id=7, role="owner")
        )

    return app


def test_create_engagement_success() -> None:
    client = TestClient(_build_app(authenticated=True))
    response = client.post(
        "/api/engagements/",
        json={"name": "Acme Pentest", "description": "Q1 external"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["id"] == 100
    assert payload["user_id"] == 7
    assert payload["name"] == "Acme Pentest"
    assert payload["description"] == "Q1 external"
    assert payload["status"] == "active"
    assert payload["created_at"]
    assert payload["updated_at"]


def test_create_engagement_name_required() -> None:
    client = TestClient(_build_app(authenticated=True))
    response = client.post("/api/engagements/", json={"description": "no name"})
    assert response.status_code == 422, response.text


def test_create_engagement_empty_name_rejected() -> None:
    client = TestClient(_build_app(authenticated=True))
    response = client.post("/api/engagements/", json={"name": ""})
    assert response.status_code == 422, response.text


def test_create_engagement_whitespace_only_name_rejected() -> None:
    client = TestClient(_build_app(authenticated=True))
    response = client.post("/api/engagements/", json={"name": "   "})
    assert response.status_code == 422, response.text


def test_create_engagement_name_trimmed() -> None:
    client = TestClient(_build_app(authenticated=True))
    response = client.post("/api/engagements/", json={"name": "  Acme  "})
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Acme"


def test_create_engagement_unauthenticated() -> None:
    client = TestClient(_build_app(authenticated=False))
    response = client.post("/api/engagements/", json={"name": "Acme Pentest"})
    assert response.status_code == 401, response.text


def test_archive_engagement_sets_archived_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_build_app(authenticated=True))
    engagement = SimpleNamespace(
        id=101,
        user_id=7,
        name="Acme Pentest",
        description="Q1 external",
        status="active",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    class FakeEngagementManagementService:
        def __init__(self, db):
            self.db = db

        def archive_engagement(self, *, engagement_id: int, tenant_id: int, user_id: int):
            assert engagement_id == 101
            assert tenant_id == 1
            assert user_id == 7
            engagement.status = "archived"
            return engagement

    monkeypatch.setattr(engagements_crud, "EngagementManagementService", FakeEngagementManagementService)
    response = client.delete("/api/engagements/101")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "archived"


def test_archive_engagement_rejected_when_tasks_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_build_app(authenticated=True))
    class FakeEngagementManagementService:
        def __init__(self, db):
            self.db = db

        def archive_engagement(self, *, engagement_id: int, tenant_id: int, user_id: int):
            _ = user_id
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot archive engagement while runtime-active tasks exist. "
                    "Stop/retire active tasks before archiving."
                ),
            )

    monkeypatch.setattr(engagements_crud, "EngagementManagementService", FakeEngagementManagementService)
    response = client.delete("/api/engagements/101")
    assert response.status_code == 409, response.text
    payload = response.json()
    assert "runtime-active tasks" in payload["detail"]
    assert "Stop/retire active tasks" in payload["detail"]


def test_restore_engagement_sets_active_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_build_app(authenticated=True))
    engagement = SimpleNamespace(
        id=102,
        user_id=7,
        name="Acme Pentest",
        description="Q1 external",
        status="archived",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    class FakeEngagementManagementService:
        def __init__(self, db):
            self.db = db

        def restore_engagement(self, *, engagement_id: int, tenant_id: int, user_id: int):
            assert engagement_id == 102
            assert tenant_id == 1
            assert user_id == 7
            engagement.status = "active"
            return engagement

    monkeypatch.setattr(engagements_crud, "EngagementManagementService", FakeEngagementManagementService)
    response = client.post("/api/engagements/102/restore")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "active"
