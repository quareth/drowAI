"""Tenant/user-authorization matrix tests for artifact provenance routes.

Responsibilities:
- Verify artifact read routes enforce tenant action policy.
- Verify cross-owner and cross-tenant task ids fail closed with no data leakage.
- Preserve default-tenant response envelope compatibility.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers import artifact_provenance as artifact_routes


class _FakeArtifactQueryService:
    def get_execution_by_id(self, *, execution_id, task_id, tenant_id, include_artifacts):
        _ = include_artifacts
        return {
            "execution": {
                "execution_id": execution_id,
                "task_id": task_id,
                "tenant_id": tenant_id,
            },
            "artifacts": [],
        }


@pytest.fixture
def artifact_authz_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(artifact_routes.router)

    users = {
        "owner-token": SimpleNamespace(id=1, username="owner", is_active=True),
        "viewer-token": SimpleNamespace(id=2, username="viewer", is_active=True),
        "blocked-token": SimpleNamespace(id=1, username="blocked-owner-role", is_active=True),
        "default-owner-token": SimpleNamespace(id=4, username="default", is_active=True),
    }
    tenant_contexts = {
        "owner-token": SimpleNamespace(tenant_id=701, user_id=1, role="owner"),
        "viewer-token": SimpleNamespace(tenant_id=701, user_id=2, role="viewer"),
        "blocked-token": SimpleNamespace(tenant_id=701, user_id=1, role="unknown"),
        "default-owner-token": SimpleNamespace(tenant_id=1, user_id=4, role="owner"),
    }
    task_tenants = {11: 701, 21: 702, 31: 1}
    task_owners = {11: 1, 21: 4, 31: 4}

    def fake_get_current_user(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        user = users.get(token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        return user

    def fake_get_tenant_context(request: Request):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        context = tenant_contexts.get(token)
        if context is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")
        return context

    def fake_get_db():
        yield object()

    def fake_get_tenant_task_or_404(*, db, task_id: int, tenant_context):
        _ = db
        tenant_id = task_tenants.get(int(task_id))
        owner_id = task_owners.get(int(task_id))
        if (
            tenant_id is None
            or owner_id is None
            or int(tenant_id) != int(tenant_context.tenant_id)
            or int(owner_id) != int(tenant_context.user_id)
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return SimpleNamespace(id=int(task_id), tenant_id=int(tenant_id))

    monkeypatch.setattr(artifact_routes, "_query_service", lambda _db: _FakeArtifactQueryService())
    monkeypatch.setattr(artifact_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)

    app.dependency_overrides[artifact_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[artifact_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[artifact_routes.get_db] = fake_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_same_tenant_owner_allowed_role_succeeds(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/11/executions/exec-1",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("execution", "artifacts")
    assert payload["execution"]["execution_id"] == "exec-1"


def test_same_tenant_non_owner_fails_without_leakage(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/11/executions/exec-2",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_same_tenant_owner_denied_role_fails(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/11/executions/exec-2",
        headers={"Authorization": "Bearer blocked-token"},
    )
    assert response.status_code == 403, response.text


def test_foreign_tenant_resource_id_fails_without_leakage(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/21/executions/exec-3",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_default_tenant_standalone_parity_and_response_shape(artifact_authz_client: TestClient) -> None:
    response = artifact_authz_client.get(
        "/api/artifact-provenance/tasks/31/executions/exec-4",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("execution", "artifacts")
    assert payload["execution"]["tenant_id"] == 1
