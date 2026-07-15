"""Tenant-authorization tests for usage router task and export endpoints.

These tests cover tenant-scoped task usage reads and tenant usage export
permission gating for Tenant Isolation Task 3.4.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.llm import LLMUsageRecord
from backend.routers import usage as usage_routes


@pytest.fixture
def usage_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="usage-owner", password="secret")
        viewer = User(username="usage-viewer", password="secret")
        foreign = User(username="usage-foreign", password="secret")
        db.add_all([owner, viewer, foreign])
        db.flush()

        task_one = Task(user_id=owner.id, tenant_id=701, name="tenant-701-task-1")
        task_two = Task(user_id=viewer.id, tenant_id=701, name="tenant-701-task-2")
        foreign_task = Task(user_id=foreign.id, tenant_id=702, name="tenant-702-task")
        db.add_all([task_one, task_two, foreign_task])
        db.flush()

        db.add_all(
            [
                LLMUsageRecord(
                    task_id=task_one.id,
                    tenant_id=701,
                    user_id=owner.id,
                    prompt_tokens=100,
                    completion_tokens=20,
                    total_tokens=120,
                    cached_tokens=10,
                    reasoning_tokens=0,
                    model="gpt-4o-mini",
                    provider="openai",
                    source="chat_router",
                    created_at=datetime.now(timezone.utc),
                ),
                LLMUsageRecord(
                    task_id=task_two.id,
                    tenant_id=701,
                    user_id=viewer.id,
                    prompt_tokens=50,
                    completion_tokens=10,
                    total_tokens=60,
                    cached_tokens=0,
                    reasoning_tokens=0,
                    model="gpt-4o-mini",
                    provider="openai",
                    source="chat_router",
                    created_at=datetime.now(timezone.utc),
                ),
                LLMUsageRecord(
                    task_id=foreign_task.id,
                    tenant_id=702,
                    user_id=foreign.id,
                    prompt_tokens=999,
                    completion_tokens=1,
                    total_tokens=1000,
                    cached_tokens=0,
                    reasoning_tokens=0,
                    model="gpt-4o-mini",
                    provider="openai",
                    source="chat_router",
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        seeded = {
            "owner_id": owner.id,
            "viewer_id": viewer.id,
            "task_one_id": task_one.id,
            "task_two_id": task_two.id,
            "foreign_task_id": foreign_task.id,
        }

    app = FastAPI()
    app.include_router(usage_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(request: Request):
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer owner-token":
            return SimpleNamespace(id=seeded["owner_id"], username="owner", is_active=True)
        if auth == "Bearer viewer-token":
            return SimpleNamespace(id=seeded["viewer_id"], username="viewer", is_active=True)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    def fake_get_tenant_context(request: Request):
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer owner-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["owner_id"], role="owner")
        if auth == "Bearer viewer-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["viewer_id"], role="viewer")
        return SimpleNamespace(tenant_id=702, user_id=999, role="owner")

    app.dependency_overrides[usage_routes.get_db] = fake_get_db
    app.dependency_overrides[usage_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[usage_routes.get_tenant_request_context] = fake_get_tenant_context

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_task_usage_rejects_foreign_tenant_task_id(usage_client) -> None:
    client, seeded = usage_client
    response = client.get(
        f"/api/tasks/{seeded['foreign_task_id']}/usage",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_tenant_usage_export_aggregates_current_tenant_tasks_only(usage_client) -> None:
    client, _seeded = usage_client
    response = client.get(
        "/api/usage/tenant/export",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["tenant_id"] == 701
    assert payload["task_count"] == 2
    assert payload["call_count"] == 2
    assert payload["prompt_tokens"] == 150
    assert payload["completion_tokens"] == 30
    assert payload["total_tokens"] == 180
    assert payload["cached_tokens"] == 10
    assert payload["models"] == ["gpt-4o-mini"]


def test_tenant_usage_export_requires_usage_export_permission(usage_client) -> None:
    client, _seeded = usage_client
    response = client.get(
        "/api/usage/tenant/export",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 403, response.text
