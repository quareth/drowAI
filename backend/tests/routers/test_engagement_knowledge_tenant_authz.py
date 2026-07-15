"""Tenant/user-authorization matrix tests for engagement knowledge routes.

Responsibilities:
- Verify knowledge-read route policy for same-tenant owner allow/deny behavior.
- Verify cross-owner and cross-tenant engagement ids fail closed.
- Preserve compatibility response shapes for default-tenant requests.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers import engagement_knowledge as engagement_routes


class _FakeKnowledgeQueryService:
    def list_engagements(self, *, user_id, tenant_id, filters):
        _ = user_id, filters
        return {
            "items": [{"id": tenant_id * 10 + 1, "name": f"eng-{tenant_id}"}],
            "total": 1,
            "limit": 20,
            "offset": 0,
        }

    def get_engagement(self, *, engagement_id, tenant_id, user_id=None):
        _ = user_id
        return {"id": engagement_id, "name": f"eng-{engagement_id}", "tenant_id": tenant_id}


@pytest.fixture
def engagement_authz_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(engagement_routes.router)

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
    engagement_tenants = {501: 701, 601: 702, 701: 1}
    engagement_owners = {501: 1, 601: 4, 701: 4}

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

    def fake_get_owned_engagement_or_404(*, db, engagement_id: int, user_id: int, tenant_id: int):
        _ = db
        expected_tenant_id = engagement_tenants.get(int(engagement_id))
        expected_user_id = engagement_owners.get(int(engagement_id))
        if (
            expected_tenant_id is None
            or expected_user_id is None
            or int(expected_tenant_id) != int(tenant_id)
            or int(expected_user_id) != int(user_id)
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Engagement not found")
        return SimpleNamespace(id=int(engagement_id), tenant_id=int(tenant_id), user_id=int(user_id))

    monkeypatch.setattr(engagement_routes, "_query_service", lambda _db: _FakeKnowledgeQueryService())
    monkeypatch.setattr(
        engagement_routes,
        "get_owned_engagement_or_404",
        fake_get_owned_engagement_or_404,
    )

    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[engagement_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[engagement_routes.get_db] = fake_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_same_tenant_allowed_role_succeeds(engagement_authz_client: TestClient) -> None:
    response = engagement_authz_client.get(
        "/api/engagements",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("items", "total", "limit", "offset")
    assert payload["total"] == 1


def test_same_tenant_denied_role_fails(engagement_authz_client: TestClient) -> None:
    response = engagement_authz_client.get(
        "/api/engagements",
        headers={"Authorization": "Bearer blocked-token"},
    )
    assert response.status_code == 403, response.text


def test_foreign_tenant_resource_id_fails_without_leakage(engagement_authz_client: TestClient) -> None:
    response = engagement_authz_client.get(
        "/api/engagements/601",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"


def test_same_tenant_non_owner_resource_id_fails_without_leakage(engagement_authz_client: TestClient) -> None:
    response = engagement_authz_client.get(
        "/api/engagements/501",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"


def test_default_tenant_standalone_parity_and_response_shape(engagement_authz_client: TestClient) -> None:
    response = engagement_authz_client.get(
        "/api/engagements/701",
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("id", "name", "tenant_id")
    assert payload["tenant_id"] == 1
