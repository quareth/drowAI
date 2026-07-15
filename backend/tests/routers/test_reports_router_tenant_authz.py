"""Tenant/user-authorization tests for report CRUD router endpoints.

These tests verify reports are filtered by active tenant and owner context
and that write/delete actions enforce centralized tenant role policy.
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
from backend.models.core import Report, Task, User
from backend.routers import reports as reports_routes


@pytest.fixture
def reports_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="reports-owner", password="secret")
        teammate = User(username="reports-teammate", password="secret")
        foreign = User(username="reports-foreign", password="secret")
        db.add_all([owner, teammate, foreign])
        db.flush()

        owner_task = Task(user_id=owner.id, tenant_id=701, name="owner-task")
        teammate_task = Task(user_id=teammate.id, tenant_id=701, name="teammate-task")
        foreign_task = Task(user_id=foreign.id, tenant_id=702, name="foreign-task")
        db.add_all([owner_task, teammate_task, foreign_task])
        db.flush()

        db.add_all(
            [
                Report(
                    task_id=owner_task.id,
                    tenant_id=701,
                    user_id=owner.id,
                    title="owner-report",
                    content="owner content",
                    findings={},
                    severity="info",
                    created_at=datetime.now(timezone.utc),
                ),
                Report(
                    task_id=teammate_task.id,
                    tenant_id=701,
                    user_id=teammate.id,
                    title="teammate-report",
                    content="teammate content",
                    findings={},
                    severity="info",
                    created_at=datetime.now(timezone.utc),
                ),
                Report(
                    task_id=foreign_task.id,
                    tenant_id=702,
                    user_id=foreign.id,
                    title="foreign-report",
                    content="foreign content",
                    findings={},
                    severity="info",
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )
        db.commit()

        seeded = {
            "owner_id": owner.id,
            "teammate_id": teammate.id,
            "owner_task_id": owner_task.id,
            "foreign_task_id": foreign_task.id,
        }

    app = FastAPI()
    app.include_router(reports_routes.router, prefix="/api/reports")

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
        if auth == "Bearer teammate-token":
            return SimpleNamespace(id=seeded["teammate_id"], username="teammate", is_active=True)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    def fake_get_tenant_context(request: Request):
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer owner-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["owner_id"], role="owner")
        if auth == "Bearer teammate-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["teammate_id"], role="viewer")
        return SimpleNamespace(tenant_id=702, user_id=999, role="owner")

    app.dependency_overrides[reports_routes.get_db] = fake_get_db
    app.dependency_overrides[reports_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[reports_routes.get_tenant_request_context] = fake_get_tenant_context

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_list_reports_returns_active_tenant_owner_rows_only(reports_client) -> None:
    client, _seeded = reports_client
    response = client.get(
        "/api/reports/",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    titles = {item["title"] for item in payload}
    assert titles == {"owner-report"}


def test_create_report_requires_report_write_permission(reports_client) -> None:
    client, seeded = reports_client
    response = client.post(
        "/api/reports/",
        headers={"Authorization": "Bearer teammate-token"},
        json={
            "task_id": seeded["owner_task_id"],
            "title": "blocked",
            "content": "blocked content",
            "findings": {},
            "severity": "info",
        },
    )
    assert response.status_code == 403, response.text


def test_get_task_reports_hides_foreign_tenant_task(reports_client) -> None:
    client, seeded = reports_client
    response = client.get(
        f"/api/reports/task/{seeded['foreign_task_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_task_reports_hides_same_tenant_non_owner_task(reports_client) -> None:
    client, seeded = reports_client
    response = client.get(
        f"/api/reports/task/{seeded['owner_task_id']}",
        headers={"Authorization": "Bearer teammate-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"
