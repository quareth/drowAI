"""API tests for cross-task interrupt inbox summaries."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.hitl import InterruptTicket, InterruptTicketState
from backend.routers.tasks import interrupt_inbox as inbox_router


def test_interrupt_inbox_returns_only_owned_pending_interrupts() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="owner", password="secret")
        other = User(username="other", password="secret")
        db.add_all([owner, other])
        db.flush()
        owner_task = Task(user_id=owner.id, name="owner-task", status="running")
        other_task = Task(user_id=other.id, name="other-task", status="running")
        db.add_all([owner_task, other_task])
        db.flush()
        db.add_all(
            [
                InterruptTicket(
                    interrupt_id="intr-owner-pending",
                    task_id=owner_task.id,
                    graph_name="simple_tool",
                    interrupt_type="tool_approval",
                    state=InterruptTicketState.PENDING,
                ),
                InterruptTicket(
                    interrupt_id="intr-owner-resumed",
                    task_id=owner_task.id,
                    graph_name="simple_tool",
                    interrupt_type="tool_approval",
                    state=InterruptTicketState.RESUMED,
                ),
                InterruptTicket(
                    interrupt_id="intr-other-pending",
                    task_id=other_task.id,
                    graph_name="deep_reasoning",
                    interrupt_type="plan_review",
                    state=InterruptTicketState.PENDING,
                ),
            ]
        )
        db.commit()
        owner_id = owner.id

    app = FastAPI()
    app.include_router(inbox_router.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user():
        return SimpleNamespace(id=owner_id, username="owner", is_active=True)

    app.dependency_overrides[inbox_router.get_db] = fake_get_db
    app.dependency_overrides[inbox_router.get_current_user] = fake_get_current_user

    client = TestClient(app)
    try:
        response = client.get("/interrupts/inbox")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["count"] == 1
        item = payload["items"][0]
        assert item["interrupt_id"] == "intr-owner-pending"
        assert item["task_id"] > 0
        assert item["interrupt_type"] == "tool_approval"
        assert item["graph_name"] == "simple_tool"
    finally:
        client.close()
        app.dependency_overrides.clear()
        engine.dispose()
