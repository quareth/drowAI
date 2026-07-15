"""Router-level tests for admission-rejection detail normalization.

Responsibilities:
- Verify task create/start routes normalize admission 409 details to a stable
  `{reason_code, reason_codes, message}` payload.
- Verify non-admission 409 payloads are not rewritten by the mapper.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from backend.routers.tasks import crud as crud_routes
from backend.routers.tasks import container as container_routes
from backend.routers.tasks import runtime as runtime_routes


def _fake_user() -> SimpleNamespace:
    return SimpleNamespace(id=7, username="owner", is_active=True)


def _fake_db():
    yield object()


def _tenant_context() -> SimpleNamespace:
    return SimpleNamespace(tenant_id=12, user_id=7, role="owner")


def test_create_route_normalizes_admission_reason_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")
    app.dependency_overrides[crud_routes.get_current_user] = _fake_user
    app.dependency_overrides[crud_routes.get_db] = _fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = _tenant_context
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class _RejectLifecycleService:
        def __init__(self, _db) -> None:
            pass

        def create_task(self, **_kwargs):  # noqa: ANN003, ANN201
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "USER_QUOTA_EXCEEDED",
                    "message": "",
                },
            )

    monkeypatch.setattr(crud_routes, "TaskLifecycleService", _RejectLifecycleService)

    client = TestClient(app)
    try:
        response = client.post("/api/tasks/", json={"name": "quota-test"})
    finally:
        app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": {
            "reason_code": "USER_QUOTA_EXCEEDED",
            "reason_codes": ["USER_QUOTA_EXCEEDED"],
            "message": "Task admission rejected.",
        }
    }


def test_start_route_normalizes_admission_reason_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(runtime_routes.router, prefix="/api/tasks")
    app.dependency_overrides[runtime_routes.get_current_user] = _fake_user
    app.dependency_overrides[runtime_routes.get_db] = _fake_db
    app.dependency_overrides[runtime_routes.get_tenant_request_context] = _tenant_context
    monkeypatch.setattr(runtime_routes, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime_routes,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    class _RejectRuntimeService:
        def __init__(self, _db) -> None:
            pass

        async def start_task(self, **_kwargs):  # noqa: ANN003, ANN201
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "RUNNER_CAPACITY_EXHAUSTED",
                    "reason_codes": ["RUNNER_CAPACITY_EXHAUSTED"],
                    "message": "Runner active-task ceiling reached.",
                },
            )

    monkeypatch.setattr(runtime_routes, "TaskRuntimeService", _RejectRuntimeService)

    client = TestClient(app)
    try:
        response = client.post("/api/tasks/77/start")
    finally:
        app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": {
            "reason_code": "RUNNER_CAPACITY_EXHAUSTED",
            "reason_codes": ["RUNNER_CAPACITY_EXHAUSTED"],
            "message": "Runner active-task ceiling reached.",
        }
    }


def test_container_create_route_normalizes_admission_reason_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(container_routes.router, prefix="/api/tasks")
    app.dependency_overrides[container_routes.get_current_user] = _fake_user
    app.dependency_overrides[container_routes.get_db] = _fake_db
    app.dependency_overrides[container_routes.get_tenant_request_context] = _tenant_context
    monkeypatch.setattr(container_routes, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **kwargs: SimpleNamespace(
            id=kwargs["task_id"],
            user_id=kwargs["tenant_context"].user_id,
            tenant_id=kwargs["tenant_context"].tenant_id,
        ),
    )

    class _RejectRuntimeService:
        def __init__(self, _db) -> None:
            pass

        async def start_task(self, **_kwargs):  # noqa: ANN003, ANN201
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "TENANT_QUOTA_EXCEEDED",
                    "message": "",
                },
            )

    monkeypatch.setattr(container_routes, "TaskRuntimeService", _RejectRuntimeService)

    client = TestClient(app)
    try:
        response = client.post("/api/tasks/31/container/create")
    finally:
        app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": {
            "reason_code": "TENANT_QUOTA_EXCEEDED",
            "reason_codes": ["TENANT_QUOTA_EXCEEDED"],
            "message": "Task admission rejected.",
        }
    }


def test_create_route_preserves_non_admission_409_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(crud_routes.router, prefix="/api/tasks")
    app.dependency_overrides[crud_routes.get_current_user] = _fake_user
    app.dependency_overrides[crud_routes.get_db] = _fake_db
    app.dependency_overrides[crud_routes.get_tenant_request_context] = _tenant_context
    monkeypatch.setattr(crud_routes, "enforce_tenant_action", lambda **_kwargs: None)

    class _ConflictLifecycleService:
        def __init__(self, _db) -> None:
            pass

        def create_task(self, **_kwargs):  # noqa: ANN003, ANN201
            raise HTTPException(status_code=409, detail="A task with this name already exists.")

    monkeypatch.setattr(crud_routes, "TaskLifecycleService", _ConflictLifecycleService)

    client = TestClient(app)
    try:
        response = client.post("/api/tasks/", json={"name": "duplicate"})
    finally:
        app.dependency_overrides.clear()
        client.close()

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "A task with this name already exists."}
