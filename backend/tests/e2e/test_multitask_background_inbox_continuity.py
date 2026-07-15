"""End-to-end regression checks for background multi-task chat continuity.

Covers the quality-gate scenarios:
- Task A run context survives while user views Task B and approves A from inbox.
- Explicit cancel remains idempotent and does not corrupt persisted chat history."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.chat import ChatMessage
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import chat as chat_routes
from backend.routers.tasks import interrupt_inbox as inbox_routes
from backend.routers.tasks import interrupts as interrupt_routes
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id
from backend.services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service


class _StubInterruptStateService:
    def __init__(self, payload: dict) -> None:
        self._payload = dict(payload)

    async def get_pending_interrupt(self, task_id: int, graph_name: str | None = None, **_):
        if task_id != int(self._payload.get("task_id", -1)):
            return None
        return dict(self._payload)


def _create_owner_with_tasks(
    db: Session,
    *,
    username: str,
    email: str,
    tenant_slug: str,
    task_names: tuple[str, ...],
) -> tuple[User, list[Task]]:
    owner = User(username=username, password="x", email=email)
    tenant = Tenant(slug=tenant_slug, name=tenant_slug.replace("-", " ").title())
    db.add_all([owner, tenant])
    db.commit()
    db.refresh(owner)
    db.refresh(tenant)
    db.add(TenantMembership(tenant_id=tenant.id, user_id=owner.id, role="owner", status="active"))
    db.commit()

    tasks = [Task(user_id=owner.id, tenant_id=tenant.id, name=name) for name in task_names]
    db.add_all(tasks)
    db.commit()
    for task in tasks:
        db.refresh(task)
    return owner, tasks


def _insert_chat_message(
    db: Session,
    *,
    task_id: int,
    tenant_id: int,
    conversation_id: str,
    turn_number: int,
    message_type: str,
    content: str,
) -> None:
    db.add(
        ChatMessage(
            task_id=task_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turn_number=turn_number,
            message_type=message_type,
            message=content,
        )
    )


def test_task_a_background_approval_from_inbox_preserves_continuity(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    owner_id: int
    owner_username: str
    task_a_id: int
    task_b_id: int
    task_a_tenant_id: int
    task_a_thread_id: str
    try:
        owner, tasks = _create_owner_with_tasks(
            db,
            username="owner_bg",
            email="owner_bg@example.com",
            tenant_slug="owner-bg",
            task_names=("task-a-bg", "task-b-bg"),
        )
        task_a, task_b = tasks

        owner_id = int(owner.id)
        owner_username = str(owner.username)
        task_a_id = int(task_a.id)
        task_b_id = int(task_b.id)
        task_a_tenant_id = int(task_a.tenant_id)
        task_a_thread_id = format_graph_thread_id(task_a.graph_thread_id, task_id=task_a_id)

        turn_id_a = f"task-{task_a_id}-turn-1"
        conv_a = "conv-task-a"
        _insert_chat_message(
            db,
            task_id=task_a_id,
            tenant_id=task_a_tenant_id,
            conversation_id=conv_a,
            turn_number=1,
            message_type="assistant",
            content="Task A partial answer",
        )
        _insert_chat_message(
            db,
            task_id=task_a_id,
            tenant_id=task_a_tenant_id,
            conversation_id=conv_a,
            turn_number=2,
            message_type="assistant",
            content="Task A final answer",
        )

        interrupt_id = "simple_tool:checkpoint:cp-task-a"
        db.add(
            InterruptTicket(
                interrupt_id=interrupt_id,
                task_id=task_a_id,
                tenant_id=task_a_tenant_id,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
                checkpoint_id="cp-task-a",
                thread_id=task_a_thread_id,
                turn_id=turn_id_a,
                turn_sequence=1,
                state=InterruptTicketState.PENDING,
                payload_snapshot={
                    "type": "tool_approval",
                    "turn_id": turn_id_a,
                    "turn_sequence": 1,
                    "reserved_message_id": 333,
                    "conversation_id": conv_a,
                    "tool_id": "shell.exec",
                    "tool_name": "shell.exec",
                    "parameters": {"command": "echo task-a"},
                },
            )
        )
        db.commit()
    finally:
        db.close()

    captured: dict = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api")
    app.include_router(interrupt_routes.router, prefix="/api/tasks")
    app.include_router(inbox_routes.router, prefix="/api/tasks")

    def _current_user():
        return SimpleNamespace(id=owner_id, username=owner_username, is_active=True)

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[chat_routes.get_current_user] = _current_user
    app.dependency_overrides[chat_routes.get_db] = _db_override
    app.dependency_overrides[interrupt_routes.get_current_user] = _current_user
    app.dependency_overrides[interrupt_routes.get_db] = _db_override
    app.dependency_overrides[inbox_routes.get_current_user] = _current_user
    app.dependency_overrides[inbox_routes.get_db] = _db_override

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )
    monkeypatch.setattr(
        interrupt_routes,
        "get_interrupt_state_service",
        lambda: _StubInterruptStateService(
            {
                "has_interrupt": True,
                "task_id": task_a_id,
                "thread_id": task_a_thread_id,
                "graph_name": "simple_tool",
                "interrupt_id": interrupt_id,
                "checkpoint_id": "cp-task-a",
                "interrupt_type": "tool_approval",
                "payload": {
                    "type": "tool_approval",
                    "tool_id": "shell.exec",
                    "tool_name": "shell.exec",
                    "parameters": {"command": "echo task-a"},
                },
                "resumable": True,
            }
        ),
    )

    with TestClient(app) as client:
        # "Switch to Task B": user context/view is task B while A is pending.
        task_b_snapshot = client.get(f"/api/tasks/{task_b_id}/interrupt")
        assert task_b_snapshot.status_code == 200, task_b_snapshot.text
        assert task_b_snapshot.json()["has_interrupt"] is False

        inbox_resp = client.get("/api/tasks/interrupts/inbox")
        assert inbox_resp.status_code == 200, inbox_resp.text
        inbox_payload = inbox_resp.json()
        assert inbox_payload["count"] == 1
        inbox_item = inbox_payload["items"][0]
        assert inbox_item["task_id"] == task_a_id
        assert inbox_item["interrupt_id"] == interrupt_id

        # Approve Task A from inbox while not viewing Task A.
        resume_resp = client.post(
            f"/api/tasks/{task_a_id}/graph/resume",
            json={
                "interrupt_id": interrupt_id,
                "interrupt_type": "tool_approval",
                "graph_name": "simple_tool",
                "response": {"action": "approve"},
            },
        )
        assert resume_resp.status_code == 200, resume_resp.text
        assert resume_resp.json()["status"] == "resumed"

        # Duplicate resume is deterministic conflict.
        dup_resume = client.post(
            f"/api/tasks/{task_a_id}/graph/resume",
            json={
                "interrupt_id": interrupt_id,
                "interrupt_type": "tool_approval",
                "graph_name": "simple_tool",
                "response": {"action": "approve"},
            },
        )
        assert dup_resume.status_code == 409, dup_resume.text

        # "Return to Task A": stream history continuity remains intact.
        history_resp = client.get(
            f"/api/tasks/{task_a_id}/chat/history?conversation_id={conv_a}"
        )
        assert history_resp.status_code == 200, history_resp.text
        history_payload = history_resp.json()
        transcript_items = history_payload.get("items") or history_payload.get("events") or []
        contents = [event.get("content") for event in transcript_items]
        assert "Task A partial answer" in contents
        assert "Task A final answer" in contents

    verify = SessionFactory()
    try:
        ticket = (
            verify.query(InterruptTicket)
            .filter(
                InterruptTicket.task_id == task_a_id,
                InterruptTicket.interrupt_id == interrupt_id,
            )
            .one()
        )
        assert ticket.state == InterruptTicketState.RESUMING
        workflow = (
            verify.query(TurnWorkflow)
            .filter(TurnWorkflow.task_id == task_a_id)
            .one()
        )
        assert workflow.state == "RESUMED"
        assert captured["resume_kwargs"]["task_id"] == task_a_id
        assert captured["resume_kwargs"]["interrupt_id"] == interrupt_id
    finally:
        verify.close()
        engine.dispose()


def test_task_a_sequential_interrupt_after_inbox_approval_no_stale_replay(monkeypatch) -> None:
    """After approving Task A from inbox, next distinct interrupt for A appears without old replay."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    owner_id: int
    owner_username: str
    task_a_id: int
    task_b_id: int
    task_a_tenant_id: int
    task_a_thread_id: str
    interrupt_id_1: str
    try:
        owner, tasks = _create_owner_with_tasks(
            db,
            username="owner_seq",
            email="owner_seq@example.com",
            tenant_slug="owner-seq",
            task_names=("task-a-seq", "task-b-seq"),
        )
        task_a, task_b = tasks

        owner_id = int(owner.id)
        owner_username = str(owner.username)
        task_a_id = int(task_a.id)
        task_b_id = int(task_b.id)
        task_a_tenant_id = int(task_a.tenant_id)
        task_a_thread_id = format_graph_thread_id(task_a.graph_thread_id, task_id=task_a_id)

        interrupt_id_1 = "simple_tool:checkpoint:cp-task-a-seq-1"
        db.add(
            InterruptTicket(
                interrupt_id=interrupt_id_1,
                task_id=task_a_id,
                tenant_id=task_a_tenant_id,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
                checkpoint_id="cp-task-a-seq-1",
                thread_id=task_a_thread_id,
                turn_id=f"task-{task_a_id}-turn-1",
                turn_sequence=1,
                state=InterruptTicketState.PENDING,
                payload_snapshot={
                    "type": "tool_approval",
                    "tool_id": "shell.exec",
                    "tool_name": "shell.exec",
                    "parameters": {"command": "echo first"},
                },
            )
        )
        db.commit()
    finally:
        db.close()

    captured: dict = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    # Stub returns payload for first interrupt only; second interrupt uses ticket authority.
    stub_payload = {
        "has_interrupt": True,
        "task_id": task_a_id,
        "thread_id": task_a_thread_id,
        "graph_name": "simple_tool",
        "interrupt_id": interrupt_id_1,
        "checkpoint_id": "cp-task-a-seq-1",
        "interrupt_type": "tool_approval",
        "payload": {
            "type": "tool_approval",
            "tool_id": "shell.exec",
            "tool_name": "shell.exec",
            "parameters": {"command": "echo first"},
        },
        "resumable": True,
    }

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api")
    app.include_router(interrupt_routes.router, prefix="/api/tasks")
    app.include_router(inbox_routes.router, prefix="/api/tasks")

    def _current_user():
        return SimpleNamespace(id=owner_id, username=owner_username, is_active=True)

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[chat_routes.get_current_user] = _current_user
    app.dependency_overrides[chat_routes.get_db] = _db_override
    app.dependency_overrides[interrupt_routes.get_current_user] = _current_user
    app.dependency_overrides[interrupt_routes.get_db] = _db_override
    app.dependency_overrides[inbox_routes.get_current_user] = _current_user
    app.dependency_overrides[inbox_routes.get_db] = _db_override

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )
    monkeypatch.setattr(
        interrupt_routes,
        "get_interrupt_state_service",
        lambda: _StubInterruptStateService(stub_payload),
    )

    with TestClient(app) as client:
        # Approve Task A from inbox (user viewing Task B).
        resume_resp = client.post(
            f"/api/tasks/{task_a_id}/graph/resume",
            json={
                "interrupt_id": interrupt_id_1,
                "interrupt_type": "tool_approval",
                "graph_name": "simple_tool",
                "response": {"action": "approve"},
            },
        )
        assert resume_resp.status_code == 200, resume_resp.text

        # No pending interrupt for A after approve.
        task_a_snapshot = client.get(f"/api/tasks/{task_a_id}/interrupt")
        assert task_a_snapshot.status_code == 200, task_a_snapshot.text
        assert task_a_snapshot.json()["has_interrupt"] is False

        # Insert second distinct interrupt for Task A.
        db2 = SessionFactory()
        interrupt_id_2 = "simple_tool:checkpoint:cp-task-a-seq-2"
        db2.add(
            InterruptTicket(
                interrupt_id=interrupt_id_2,
                task_id=task_a_id,
                tenant_id=task_a_tenant_id,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
                checkpoint_id="cp-task-a-seq-2",
                thread_id=task_a_thread_id,
                turn_id=f"task-{task_a_id}-turn-2",
                turn_sequence=2,
                state=InterruptTicketState.PENDING,
                payload_snapshot={
                    "type": "tool_approval",
                    "tool_id": "shell.exec",
                    "tool_name": "shell.exec",
                    "parameters": {"command": "echo second"},
                },
            )
        )
        db2.commit()
        db2.close()

        # Next interrupt appears for Task A; no stale replay.
        task_a_snapshot2 = client.get(f"/api/tasks/{task_a_id}/interrupt")
        assert task_a_snapshot2.status_code == 200, task_a_snapshot2.text
        payload2 = task_a_snapshot2.json()
        assert payload2["has_interrupt"] is True
        assert payload2["interrupt_id"] == interrupt_id_2
        assert payload2["interrupt_id"] != interrupt_id_1

        # Task B still has no interrupt.
        task_b_snapshot = client.get(f"/api/tasks/{task_b_id}/interrupt")
        assert task_b_snapshot.status_code == 200, task_b_snapshot.text
        assert task_b_snapshot.json()["has_interrupt"] is False

    engine.dispose()


def test_explicit_cancel_is_idempotent_and_preserves_history() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    owner_id: int
    owner_username: str
    task_id: int
    tenant_id: int
    try:
        owner, tasks = _create_owner_with_tasks(
            db,
            username="owner_cancel",
            email="owner_cancel@example.com",
            tenant_slug="owner-cancel",
            task_names=("task-cancel",),
        )
        task = tasks[0]
        owner_id = int(owner.id)
        owner_username = str(owner.username)
        task_id = int(task.id)
        tenant_id = int(task.tenant_id)
        conv_id = "conv-cancel"
        turn_id = f"task-{task_id}-turn-3"
        _insert_chat_message(
            db,
            task_id=task_id,
            tenant_id=tenant_id,
            conversation_id=conv_id,
            turn_number=1,
            message_type="assistant",
            content="History survives cancel",
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(chat_routes.router, prefix="/api")

    def _current_user():
        return SimpleNamespace(id=owner_id, username=owner_username, is_active=True)

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[chat_routes.get_current_user] = _current_user
    app.dependency_overrides[chat_routes.get_db] = _db_override

    lifecycle = get_run_lifecycle_service()
    lifecycle.start_run(task_id=task_id, turn_id=turn_id, conversation_id=conv_id)
    try:
        with TestClient(app) as client:
            cancel_one = client.post(
                f"/api/tasks/{task_id}/chat/cancel",
                json={"turn_id": turn_id, "reason": "manual_stop"},
            )
            assert cancel_one.status_code == 200, cancel_one.text
            assert cancel_one.json()["status"] == "cancel_requested"

            cancel_two = client.post(
                f"/api/tasks/{task_id}/chat/cancel",
                json={"turn_id": turn_id, "reason": "manual_stop"},
            )
            assert cancel_two.status_code == 200, cancel_two.text
            assert cancel_two.json()["status"] == "already_cancelled"

            history_resp = client.get(
                f"/api/tasks/{task_id}/chat/history?conversation_id={conv_id}"
            )
            assert history_resp.status_code == 200, history_resp.text
            history_payload = history_resp.json()
            transcript_items = history_payload.get("items") or history_payload.get("events") or []
            assert any(event.get("content") == "History survives cancel" for event in transcript_items)
    finally:
        lifecycle.end_run(task_id=task_id, turn_id=turn_id, status="cancelled")
        engine.dispose()
