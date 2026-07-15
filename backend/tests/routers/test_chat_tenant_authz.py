"""Tenant-authorization matrix tests for chat submission routes.

Responsibilities:
- Verify chat route action checks for same-tenant allowed and denied roles.
- Verify cross-tenant task ids fail closed without data leakage.
- Preserve default-tenant response shape compatibility for accepted submits.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest

from backend.routers import chat as chat_routes
from backend.routers.chat import submit as chat_submit_routes


@pytest.fixture
def chat_authz_client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(chat_routes.router)

    users = {
        "owner-token": SimpleNamespace(id=1, username="owner", is_active=True),
        "viewer-token": SimpleNamespace(id=2, username="viewer", is_active=True),
        "default-owner-token": SimpleNamespace(id=3, username="default-owner", is_active=True),
    }
    tenant_contexts = {
        "owner-token": SimpleNamespace(tenant_id=701, user_id=1, role="owner"),
        "viewer-token": SimpleNamespace(tenant_id=701, user_id=2, role="viewer"),
        "default-owner-token": SimpleNamespace(tenant_id=1, user_id=3, role="owner"),
    }
    task_tenants = {11: 701, 12: 701, 21: 702, 31: 1}

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
        if tenant_id is None or int(tenant_id) != int(tenant_context.tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        return SimpleNamespace(id=int(task_id), tenant_id=int(tenant_id), name=f"task-{task_id}")

    async def fake_submit_chat_request(*, task_id, payload, current_user, db, task):
        _ = payload, current_user, db, task
        return {"task_id": task_id, "accepted": True, "conversation_id": f"conv-{task_id}"}

    monkeypatch.setattr(chat_submit_routes, "get_tenant_task_or_404", fake_get_tenant_task_or_404)
    monkeypatch.setattr(chat_submit_routes, "_submit_chat_request", fake_submit_chat_request)

    app.dependency_overrides[chat_submit_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_submit_routes.get_tenant_request_context] = fake_get_tenant_context
    app.dependency_overrides[chat_submit_routes.get_db] = fake_get_db

    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_same_tenant_allowed_role_succeeds(chat_authz_client: TestClient) -> None:
    response = chat_authz_client.post(
        "/tasks/11/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload == {"task_id": 11, "accepted": True, "conversation_id": "conv-11"}


def test_same_tenant_denied_role_fails(chat_authz_client: TestClient) -> None:
    response = chat_authz_client.post(
        "/tasks/12/chat",
        json={"message": "blocked"},
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 403, response.text


def test_foreign_tenant_task_id_fails_without_leakage(chat_authz_client: TestClient) -> None:
    response = chat_authz_client.post(
        "/tasks/21/chat",
        json={"message": "cross-tenant"},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_default_tenant_standalone_parity_and_response_shape(chat_authz_client: TestClient) -> None:
    response = chat_authz_client.post(
        "/tasks/31/chat",
        json={"message": "default tenant"},
        headers={"Authorization": "Bearer default-owner-token"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("task_id", "accepted", "conversation_id")
    assert payload["accepted"] is True
