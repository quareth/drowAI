"""Regression tests that lock task cleanup and router error contracts."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

from backend.routers.tasks import container as container_routes
from backend.routers.tasks import crud as crud_routes
from backend.routers.tasks import scope as scope_routes


@pytest.fixture
def auth_db_overrides():
    """Provide reusable auth/db dependency overrides."""

    def fake_user():
        return SimpleNamespace(id=1, username="owner", is_active=True)

    def fake_db():
        yield object()

    return fake_user, fake_db


def test_container_status_endpoint_uses_string_status_contract(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(container_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[container_routes.get_current_user] = fake_user
    app.dependency_overrides[container_routes.get_db] = fake_db
    app.dependency_overrides[container_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=1,
        user_id=1,
        role="owner",
    )

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    async def fake_get_container_status(_task_id: int) -> str:
        return "running"

    class FakeRuntimeOperationService:
        def __init__(self, _db):
            pass

        @staticmethod
        def context_from_authorized_task(*, task, user_id):
            return SimpleNamespace(task_id=task.id, user_id=user_id, tenant_id=task.tenant_id)

        async def run_for_context(self, **_kwargs):
            return SimpleNamespace(ok=True, metadata={"delegate_result": "running"})

    monkeypatch.setattr(container_routes, "RuntimeOperationService", FakeRuntimeOperationService)

    client = TestClient(app)
    resp = client.get("/api/tasks/42/container/status")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "task_id": 42,
        "container_exists": True,
        "status": "running",
        "details": {},
    }


def test_scope_endpoint_preserves_owned_task_not_found_contract(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(scope_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[scope_routes.get_current_user] = fake_user
    app.dependency_overrides[scope_routes.get_db] = fake_db
    app.dependency_overrides[scope_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=1,
        user_id=1,
        role="owner",
    )

    def fake_not_found(**_kwargs):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    monkeypatch.setattr(scope_routes, "get_tenant_task_or_404", fake_not_found)

    client = TestClient(app)
    resp = client.get("/api/tasks/999/scope")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Task not found"


def test_container_create_endpoint_preserves_generic_error_contract(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(container_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[container_routes.get_current_user] = fake_user
    app.dependency_overrides[container_routes.get_db] = fake_db
    app.dependency_overrides[container_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=1,
        user_id=1,
        role="owner",
    )

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    class FakeTaskRuntimeService:
        def __init__(self, _db):
            pass

        async def start_task(self, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(container_routes, "TaskRuntimeService", FakeTaskRuntimeService)

    client = TestClient(app)
    resp = client.post("/api/tasks/42/container/create")
    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == "Failed to create container"


def test_container_create_endpoint_normalizes_admission_reason_payload(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(container_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[container_routes.get_current_user] = fake_user
    app.dependency_overrides[container_routes.get_db] = fake_db
    app.dependency_overrides[container_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=1,
        user_id=1,
        role="owner",
    )

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    class FakeTaskRuntimeService:
        def __init__(self, _db):
            pass

        async def start_task(self, **_kwargs):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "USER_QUOTA_EXCEEDED",
                    "message": "",
                },
            )

    monkeypatch.setattr(container_routes, "TaskRuntimeService", FakeTaskRuntimeService)

    client = TestClient(app)
    resp = client.post("/api/tasks/42/container/create")
    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": {
            "reason_code": "USER_QUOTA_EXCEEDED",
            "message": "Task admission rejected.",
        }
    }


def test_task_delete_endpoint_uses_cleanup_service_contract(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[crud_routes.get_current_user] = fake_user
    app.dependency_overrides[crud_routes.get_db] = fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=44,
        user_id=1,
        role="owner",
    )
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=42, user_id=99, tenant_id=44),
    )

    class FakeCleanupService:
        def __init__(self, db):
            self.db = db

        async def delete_task(self, task_id: int, user_id: int, *, tenant_id: int | None = None):
            assert task_id == 42
            assert user_id == 1
            assert tenant_id == 44
            return {"message": "Task and container deleted successfully"}

    monkeypatch.setattr(
        "backend.services.task.cleanup_service.TaskCleanupService",
        FakeCleanupService,
    )

    client = TestClient(app)
    resp = client.delete("/api/tasks/42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"message": "Task and container deleted successfully"}


def test_task_delete_endpoint_delegates_after_owned_task_authorization(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[crud_routes.get_current_user] = fake_user
    app.dependency_overrides[crud_routes.get_db] = fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=55,
        user_id=1,
        role="admin",
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(
        crud_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=42, user_id=1, tenant_id=55),
    )

    class FakeCleanupService:
        def __init__(self, db):
            self.db = db

        async def delete_task(self, task_id: int, user_id: int, *, tenant_id: int | None = None):
            assert task_id == 42
            assert user_id == 1
            assert tenant_id == 55
            return {"message": "Task and container deleted successfully"}

    monkeypatch.setattr("backend.services.task.cleanup_service.TaskCleanupService", FakeCleanupService)

    client = TestClient(app)
    resp = client.delete("/api/tasks/42")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"message": "Task and container deleted successfully"}


def test_task_delete_endpoint_preserves_foreign_tenant_not_found_contract(
    monkeypatch: pytest.MonkeyPatch,
    auth_db_overrides,
) -> None:
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")
    fake_user, fake_db = auth_db_overrides
    app.dependency_overrides[crud_routes.get_current_user] = fake_user
    app.dependency_overrides[crud_routes.get_db] = fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = lambda: SimpleNamespace(
        tenant_id=55,
        user_id=1,
        role="admin",
    )
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    called = {"cleanup": False}

    class FakeCleanupService:
        def __init__(self, db):
            self.db = db

        async def delete_task(self, task_id: int, user_id: int, *, tenant_id: int | None = None):
            called["cleanup"] = True
            return {"message": "Task and container deleted successfully"}

    monkeypatch.setattr("backend.services.task.cleanup_service.TaskCleanupService", FakeCleanupService)

    def _foreign_tenant(*_args, **_kwargs):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    monkeypatch.setattr(crud_routes, "get_tenant_task_or_404", _foreign_tenant)

    client = TestClient(app)
    resp = client.delete("/api/tasks/42")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Task not found"
    assert called["cleanup"] is False
