"""Router contract tests for additive task engagement request/response behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# Ensure backend.models package initialization completes before auth import path.
import backend.models  # noqa: F401

from backend.routers.tasks import crud as crud_routes


def _task_obj(*, task_id: int, user_id: int, engagement_id: int | None, tenant_id: int = 1):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
        engagement_id=engagement_id,
        name="task",
        description="desc",
        scope="scope",
        status="created",
        mode="automatic",
        created_at=now,
        updated_at=now,
        started_at=None,
        paused_at=None,
        stopped_at=None,
        completed_at=None,
        container_id=None,
        agent_pid=None,
        resource_usage=None,
        error_message=None,
        failure_reason=None,
        retry_count=0,
        current_step=None,
        total_steps=None,
        progress_percentage=0,
        timeout_seconds=3600,
        max_retries=3,
        priority=1,
    )


@pytest.fixture
def app_with_overrides():
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")

    def fake_user():
        return SimpleNamespace(id=1, username="owner", is_active=True)

    def fake_db():
        yield object()

    def fake_tenant_context():
        return SimpleNamespace(tenant_id=1, user_id=1, role="owner")

    app.dependency_overrides[crud_routes.get_current_user] = fake_user
    app.dependency_overrides[crud_routes.get_db] = fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = fake_tenant_context
    return app


def test_create_task_backward_compatible_without_engagement_id(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    class FakeLifecycleService:
        def __init__(self, db):
            self.db = db

        def create_task(self, task_data, user_id: int, *, tenant_context):
            assert getattr(task_data, "engagement_id", None) is None
            assert user_id == 1
            assert tenant_context.tenant_id == 1
            return _task_obj(task_id=101, user_id=1, engagement_id=501)

    monkeypatch.setattr(crud_routes, "TaskLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_with_engagement_or_404",
        lambda **kwargs: _task_obj(
            task_id=kwargs["task_id"],
            user_id=1,
            engagement_id=501,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    client = TestClient(app_with_overrides)
    resp = client.post(
        "/api/tasks/",
        json={"name": "task-a", "description": "desc", "scope": "net"},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["id"] == 101
    assert payload["engagement_id"] == 501


def test_create_task_accepts_explicit_engagement_id_additively(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    class FakeLifecycleService:
        def __init__(self, db):
            self.db = db

        def create_task(self, task_data, user_id: int, *, tenant_context):
            assert task_data.engagement_id == 77
            assert user_id == 1
            assert tenant_context.tenant_id == 1
            return _task_obj(task_id=102, user_id=1, engagement_id=77)

    monkeypatch.setattr(crud_routes, "TaskLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_with_engagement_or_404",
        lambda **kwargs: _task_obj(
            task_id=kwargs["task_id"],
            user_id=1,
            engagement_id=77,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    client = TestClient(app_with_overrides)
    resp = client.post(
        "/api/tasks/",
        json={"name": "task-b", "description": "desc", "scope": "net", "engagement_id": 77},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["engagement_id"] == 77


def test_update_task_allows_owned_engagement_attach(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    task = _task_obj(task_id=11, user_id=1, engagement_id=None)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeEngagementService:
        def __init__(self, db):
            self.db = db

        def resolve_for_task_creation(self, **kwargs):
            assert kwargs["user_id"] == 1
            assert kwargs["requested_engagement_id"] == 999
            assert kwargs["expected_tenant_id"] == 1
            return SimpleNamespace(id=999)

    monkeypatch.setattr(crud_routes, "EngagementService", FakeEngagementService)

    class FakeDB:
        def commit(self):
            pass

        def refresh(self, _task):
            pass

    def fake_db():
        yield FakeDB()

    app_with_overrides.dependency_overrides[crud_routes.get_db] = fake_db
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_with_engagement_or_404",
        lambda **kwargs: _task_obj(
            task_id=kwargs["task_id"],
            user_id=1,
            engagement_id=999,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/11", json={"engagement_id": 999})
    assert resp.status_code == 200, resp.text
    assert resp.json()["engagement_id"] == 999


def test_update_task_rejects_cross_user_engagement_attach_without_tenant_scope(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    task = _task_obj(task_id=16, user_id=1, engagement_id=None)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeEngagementService:
        def __init__(self, db):
            self.db = db

        def resolve_for_task_creation(self, **kwargs):
            raise HTTPException(status_code=403, detail="Engagement does not belong to the current user")

    monkeypatch.setattr(crud_routes, "EngagementService", FakeEngagementService)

    class FakeDB:
        def commit(self):
            raise AssertionError("commit should not happen on forbidden engagement attach")

        def refresh(self, _task):
            raise AssertionError("refresh should not happen on forbidden engagement attach")

    def fake_db():
        yield FakeDB()

    app_with_overrides.dependency_overrides[crud_routes.get_db] = fake_db

    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/16", json={"engagement_id": 999})
    assert resp.status_code == 403, resp.text
    assert "does not belong" in resp.json()["detail"]


def test_update_task_rejects_engagement_reassignment_for_existing_task(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    task = _task_obj(task_id=13, user_id=1, engagement_id=200)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeEngagementService:
        def __init__(self, db):
            self.db = db

        def resolve_for_task_creation(self, **kwargs):
            raise AssertionError("engagement lookup should not run for immutable reassignment")

    monkeypatch.setattr(crud_routes, "EngagementService", FakeEngagementService)

    class FakeDB:
        def commit(self):
            raise AssertionError("commit should not happen on immutable engagement reassignment")

        def refresh(self, _task):
            raise AssertionError("refresh should not happen on immutable engagement reassignment")

    def fake_db():
        yield FakeDB()

    app_with_overrides.dependency_overrides[crud_routes.get_db] = fake_db

    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/13", json={"engagement_id": 201})
    assert resp.status_code == 409, resp.text
    assert "immutable" in resp.json()["detail"]


def test_update_task_attaches_engagement_and_reloads_for_response(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    """Successful attach must commit/refresh then return TaskResponse via eager reload."""
    task = _task_obj(task_id=14, user_id=1, engagement_id=None)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeEngagementService:
        def __init__(self, db):
            self.db = db

        def resolve_for_task_creation(self, **kwargs):
            return SimpleNamespace(id=88)

    monkeypatch.setattr(crud_routes, "EngagementService", FakeEngagementService)

    reload_calls: list[tuple[int, int]] = []

    def fake_reload(**kwargs):
        reload_calls.append((kwargs["task_id"], kwargs["tenant_context"].tenant_id))
        return _task_obj(
            task_id=kwargs["task_id"],
            user_id=1,
            engagement_id=88,
            tenant_id=kwargs["tenant_context"].tenant_id,
        )

    monkeypatch.setattr(crud_routes, "get_tenant_task_with_engagement_or_404", fake_reload)

    class FakeDB:
        def commit(self) -> None:
            pass

        def refresh(self, _task) -> None:
            pass

    app_with_overrides.dependency_overrides[crud_routes.get_db] = lambda: (yield FakeDB())

    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/14", json={"engagement_id": 88})
    assert resp.status_code == 200, resp.text
    assert resp.json()["engagement_id"] == 88
    assert reload_calls == [(14, 1)]


def test_update_task_rejects_cross_tenant_engagement_attach_with_409(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    """Route-level contract: attach path returns 409 for tenant boundary mismatch."""
    task = _task_obj(task_id=15, user_id=1, engagement_id=None, tenant_id=1)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeEngagementService:
        def __init__(self, db):
            self.db = db

        def resolve_for_task_creation(self, **kwargs):
            assert kwargs["expected_tenant_id"] == 1
            raise HTTPException(
                status_code=409,
                detail=(
                    "Engagement tenant does not match task tenant boundary. "
                    "Cross-tenant task attachment is not allowed."
                ),
            )

    monkeypatch.setattr(crud_routes, "EngagementService", FakeEngagementService)

    class FakeDB:
        def commit(self):
            raise AssertionError("commit should not happen on cross-tenant engagement attach")

        def refresh(self, _task):
            raise AssertionError("refresh should not happen on cross-tenant engagement attach")

    def fake_db():
        yield FakeDB()

    app_with_overrides.dependency_overrides[crud_routes.get_db] = fake_db

    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/15", json={"engagement_id": 501})
    assert resp.status_code == 409, resp.text
    assert "Cross-tenant" in resp.json()["detail"]


def test_update_task_rejects_null_engagement_id(
    monkeypatch: pytest.MonkeyPatch,
    app_with_overrides: FastAPI,
) -> None:
    task = _task_obj(task_id=12, user_id=1, engagement_id=200)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: task,
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class FakeDB:
        def commit(self):
            raise AssertionError("commit should not happen when engagement_id is null")

        def refresh(self, _task):
            raise AssertionError("refresh should not happen when engagement_id is null")

    def fake_db():
        yield FakeDB()

    app_with_overrides.dependency_overrides[crud_routes.get_db] = fake_db
    client = TestClient(app_with_overrides)
    resp = client.put("/api/tasks/12", json={"engagement_id": None})
    assert resp.status_code == 400, resp.text
    assert "cannot be null" in resp.json()["detail"]
