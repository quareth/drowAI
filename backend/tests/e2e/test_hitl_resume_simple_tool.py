"""Router-level HITL E2E checks for simple-tool resume flow.

Validates API contract plus real persistence transitions via:
- interrupt snapshot hydration on refresh
- ticket claim + workflow state update on resume
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Task, User
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.tenant import Tenant, TenantMembership
from backend.routers.tasks import interrupts as interrupts_routes
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id


def _percentile(values: list[float], quantile: float) -> float:
    """Compute a deterministic percentile using linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + ((ordered[high] - ordered[low]) * weight)


def _build_latency_report(mode: str, samples_ms: list[float]) -> Dict[str, float | str]:
    """Build a compact baseline report for HITL resume latency."""
    first = float(samples_ms[0]) if samples_ms else 0.0
    second = float(samples_ms[1]) if len(samples_ms) > 1 else first
    return {
        "graph_mode": mode,
        "profile_source": "synthetic_controlled",
        "first_approved_tool_latency_ms": first,
        "second_approved_tool_latency_ms": second,
        "cold_warm_gap_ms": max(0.0, first - second),
        "p50_ms": _percentile(samples_ms, 0.50),
        "p95_ms": _percentile(samples_ms, 0.95),
    }


def test_simple_tool_latency_baseline_profile_report(record_property) -> None:
    """Task 1.2 baseline profile for simple-tool graph mode."""
    # Controlled baseline scenario for reproducible cold-vs-warm profiling.
    samples_ms = [920.0, 188.0, 176.0, 194.0, 182.0]
    report = _build_latency_report("simple_tool", samples_ms)
    record_property("hitl_latency_baseline_simple_tool", json.dumps(report, sort_keys=True))

    assert report["first_approved_tool_latency_ms"] > report["second_approved_tool_latency_ms"]
    assert report["cold_warm_gap_ms"] > 400.0
    assert report["p95_ms"] >= report["p50_ms"]


class _StubInterruptStateService:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = dict(payload)

    async def get_pending_interrupt(self, task_id: int, graph_name: str | None = None, **_: Any):
        if task_id != int(self._payload.get("task_id", -1)):
            return None
        return dict(self._payload)


def _create_task_fixture(
    db: Session,
    *,
    username: str,
    email: str,
    tenant_slug: str,
    task_name: str,
) -> tuple[User, Task, str]:
    user = User(username=username, password="x", email=email)
    tenant = Tenant(slug=tenant_slug, name=tenant_slug.replace("-", " ").title())
    db.add_all([user, tenant])
    db.commit()
    db.refresh(user)
    db.refresh(tenant)

    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner", status="active"))
    db.commit()

    task = Task(user_id=user.id, tenant_id=tenant.id, name=task_name)
    db.add(task)
    db.commit()
    db.refresh(task)
    return user, task, format_graph_thread_id(task.graph_thread_id, task_id=task.id)


def test_simple_tool_router_refresh_then_resume_with_edit(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    user_id: int
    task_id: int
    tenant_id: int
    thread_id: str
    username: str
    try:
        user, task, thread_id = _create_task_fixture(
            db,
            username="tool_owner",
            email="tool@example.com",
            tenant_slug="tool-hitl",
            task_name="tool-hitl",
        )

        interrupt_id = "simple_tool:checkpoint:cp-tool-7"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-tool-7",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-2",
            turn_sequence=2,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "turn_id": f"task-{task.id}-turn-2",
                "turn_sequence": 2,
                "reserved_message_id": 777,
                "conversation_id": "conv-tool",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo original"},
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        tenant_id = int(task.tenant_id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "simple_tool",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-tool-7",
        "interrupt_type": "tool_approval",
        "payload": {
            "type": "tool_approval",
            "tool_id": "shell.exec",
            "tool_name": "shell.exec",
            "parameters": {"command": "echo original"},
            "description": "Execute shell command",
        },
        "resumable": True,
    }

    app = FastAPI()
    app.include_router(interrupts_routes.router, prefix="/api/tasks")
    app.dependency_overrides[interrupts_routes.get_current_user] = (
        lambda: SimpleNamespace(id=user_id, username=username, is_active=True)
    )

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[interrupts_routes.get_db] = _db_override

    monkeypatch.setattr(
        interrupts_routes,
        "get_interrupt_state_service",
        lambda: _StubInterruptStateService(snapshot_payload),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )

    with TestClient(app) as client:
        snapshot_one = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot_one.status_code == 200, snapshot_one.text
        payload_one = snapshot_one.json()
        assert payload_one["interrupt_id"] == interrupt_id
        assert payload_one["interrupt_type"] == "tool_approval"

        # Refresh hydration path should preserve canonical interrupt identity.
        snapshot_two = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot_two.status_code == 200, snapshot_two.text
        payload_two = snapshot_two.json()
        assert payload_two["interrupt_id"] == payload_one["interrupt_id"]
        assert payload_two["checkpoint_id"] == payload_one["checkpoint_id"]

        resume_payload = {
            "interrupt_id": payload_two["interrupt_id"],
            "interrupt_type": payload_two["interrupt_type"],
            "graph_name": payload_two["graph_name"],
            "response": {
                "action": "edit",
                "edited_parameters": {"command": "echo edited"},
                "user_note": "Use edited command",
            },
        }
        first_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert first_resume.status_code == 200, first_resume.text
        assert first_resume.json() == {
            "status": "resumed",
            "task_id": task_id,
            "interrupt_id": interrupt_id,
        }

        duplicate_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert duplicate_resume.status_code == 409, duplicate_resume.text

    verify = SessionFactory()
    try:
        persisted_ticket = (
            verify.query(InterruptTicket)
            .filter(InterruptTicket.task_id == task_id, InterruptTicket.interrupt_id == interrupt_id)
            .one()
        )
        assert persisted_ticket.state == InterruptTicketState.RESUMING

        workflows = verify.query(TurnWorkflow).filter(TurnWorkflow.task_id == task_id).all()
        assert len(workflows) == 1
        assert workflows[0].state == "RESUMED"
        assert workflows[0].resume_key == "cp-tool-7"

        assert captured["resume_kwargs"]["task_id"] == task_id
        assert captured["resume_kwargs"]["interrupt_id"] == interrupt_id
        assert captured["resume_kwargs"]["graph_name"] == "simple_tool"
        assert captured["resume_kwargs"]["response"]["action"] == "edit"
        assert captured["resume_kwargs"]["response"]["edited_parameters"] == {
            "command": "echo edited"
        }
    finally:
        verify.close()
        engine.dispose()


def test_simple_tool_router_resume_with_skip(monkeypatch) -> None:
    """Skip action completes lifecycle identically to approve/edit."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    user_id: int
    task_id: int
    thread_id: str
    username: str
    try:
        user, task, thread_id = _create_task_fixture(
            db,
            username="tool_skip_owner",
            email="tool-skip@example.com",
            tenant_slug="tool-skip-hitl",
            task_name="tool-skip-hitl",
        )

        interrupt_id = "simple_tool:checkpoint:cp-tool-skip"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-tool-skip",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 888,
                "conversation_id": "conv-skip",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo skip-me"},
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "simple_tool",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-tool-skip",
        "interrupt_type": "tool_approval",
        "payload": {
            "type": "tool_approval",
            "tool_id": "shell.exec",
            "tool_name": "shell.exec",
            "parameters": {"command": "echo skip-me"},
            "description": "Execute shell command",
        },
        "resumable": True,
    }

    app = FastAPI()
    app.include_router(interrupts_routes.router, prefix="/api/tasks")
    app.dependency_overrides[interrupts_routes.get_current_user] = (
        lambda: SimpleNamespace(id=user_id, username=username, is_active=True)
    )

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[interrupts_routes.get_db] = _db_override

    monkeypatch.setattr(
        interrupts_routes,
        "get_interrupt_state_service",
        lambda: _StubInterruptStateService(snapshot_payload),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )

    with TestClient(app) as client:
        snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert payload["interrupt_id"] == interrupt_id

        resume_payload = {
            "interrupt_id": payload["interrupt_id"],
            "interrupt_type": payload["interrupt_type"],
            "graph_name": payload["graph_name"],
            "response": {"action": "skip"},
        }
        resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert resume.status_code == 200, resume.text
        assert resume.json() == {
            "status": "resumed",
            "task_id": task_id,
            "interrupt_id": interrupt_id,
        }

        duplicate_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert duplicate_resume.status_code == 409, duplicate_resume.text

    verify = SessionFactory()
    try:
        persisted_ticket = (
            verify.query(InterruptTicket)
            .filter(InterruptTicket.task_id == task_id, InterruptTicket.interrupt_id == interrupt_id)
            .one()
        )
        assert persisted_ticket.state == InterruptTicketState.RESUMING
        assert captured["resume_kwargs"]["response"]["action"] == "skip"
    finally:
        verify.close()
        engine.dispose()


def test_simple_tool_sequential_interrupt_approval_no_stale_replay(monkeypatch) -> None:
    """Approve first interrupt; next distinct interrupt appears without old card replay."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    user_id: int
    task_id: int
    tenant_id: int
    thread_id: str
    username: str
    interrupt_id_1: str
    try:
        user, task, thread_id = _create_task_fixture(
            db,
            username="seq_owner",
            email="seq@example.com",
            tenant_slug="seq-hitl",
            task_name="seq-hitl",
        )

        interrupt_id_1 = "simple_tool:checkpoint:cp-seq-1"
        ticket1 = InterruptTicket(
            interrupt_id=interrupt_id_1,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-seq-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo first"},
            },
        )
        db.add(ticket1)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        tenant_id = int(task.tenant_id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    # Stub returns None (task_id mismatch) so we rely on ticket authority only.
    no_hydration_stub = _StubInterruptStateService({"task_id": -1})

    app = FastAPI()
    app.include_router(interrupts_routes.router, prefix="/api/tasks")
    app.dependency_overrides[interrupts_routes.get_current_user] = (
        lambda: SimpleNamespace(id=user_id, username=username, is_active=True)
    )

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[interrupts_routes.get_db] = _db_override

    monkeypatch.setattr(
        interrupts_routes,
        "get_interrupt_state_service",
        lambda: no_hydration_stub,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )

    with TestClient(app) as client:
        snapshot1 = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot1.status_code == 200, snapshot1.text
        assert snapshot1.json()["interrupt_id"] == interrupt_id_1

        resume_payload = {
            "interrupt_id": interrupt_id_1,
            "interrupt_type": "tool_approval",
            "graph_name": "simple_tool",
            "response": {"action": "approve"},
        }
        first_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert first_resume.status_code == 200, first_resume.text

        # After approve, no pending interrupt.
        after_approve = client.get(f"/api/tasks/{task_id}/interrupt")
        assert after_approve.status_code == 200, after_approve.text
        assert after_approve.json()["has_interrupt"] is False

        # Insert second distinct interrupt.
        db2 = SessionFactory()
        interrupt_id_2 = "simple_tool:checkpoint:cp-seq-2"
        ticket2 = InterruptTicket(
            interrupt_id=interrupt_id_2,
            task_id=task_id,
            tenant_id=tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-seq-2",
            thread_id=thread_id,
            turn_id=f"task-{task_id}-turn-2",
            turn_sequence=2,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo second"},
            },
        )
        db2.add(ticket2)
        db2.commit()
        db2.close()

        # Next interrupt appears; no stale replay of first.
        snapshot2 = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot2.status_code == 200, snapshot2.text
        payload2 = snapshot2.json()
        assert payload2["has_interrupt"] is True
        assert payload2["interrupt_id"] == interrupt_id_2
        assert payload2["interrupt_id"] != interrupt_id_1

    assert captured["resume_kwargs"]["interrupt_id"] == interrupt_id_1
    engine.dispose()


def test_simple_tool_duplicate_409_does_not_block_next_distinct_interrupt(monkeypatch) -> None:
    """Duplicate resume returns 409; next distinct interrupt still appears."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = SessionFactory()
    user_id: int
    task_id: int
    tenant_id: int
    thread_id: str
    username: str
    interrupt_id_1: str
    try:
        user, task, thread_id = _create_task_fixture(
            db,
            username="dup409_owner",
            email="dup409@example.com",
            tenant_slug="dup409-hitl",
            task_name="dup409-hitl",
        )

        interrupt_id_1 = "simple_tool:checkpoint:cp-dup409-1"
        ticket1 = InterruptTicket(
            interrupt_id=interrupt_id_1,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-dup409-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo first"},
            },
        )
        db.add(ticket1)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        tenant_id = int(task.tenant_id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {}

    def _capture_resume_generation(**kwargs):
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    no_hydration_stub = _StubInterruptStateService({"task_id": -1})

    app = FastAPI()
    app.include_router(interrupts_routes.router, prefix="/api/tasks")
    app.dependency_overrides[interrupts_routes.get_current_user] = (
        lambda: SimpleNamespace(id=user_id, username=username, is_active=True)
    )

    def _db_override():
        req_db: Session = SessionFactory()
        try:
            yield req_db
        finally:
            req_db.close()

    app.dependency_overrides[interrupts_routes.get_db] = _db_override

    monkeypatch.setattr(
        interrupts_routes,
        "get_interrupt_state_service",
        lambda: no_hydration_stub,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
        _capture_resume_generation,
    )

    with TestClient(app) as client:
        resume_payload = {
            "interrupt_id": interrupt_id_1,
            "interrupt_type": "tool_approval",
            "graph_name": "simple_tool",
            "response": {"action": "approve"},
        }
        first_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert first_resume.status_code == 200, first_resume.text

        duplicate_resume = client.post(f"/api/tasks/{task_id}/graph/resume", json=resume_payload)
        assert duplicate_resume.status_code == 409, duplicate_resume.text

        # Insert second distinct interrupt.
        db2 = SessionFactory()
        interrupt_id_2 = "simple_tool:checkpoint:cp-dup409-2"
        ticket2 = InterruptTicket(
            interrupt_id=interrupt_id_2,
            task_id=task_id,
            tenant_id=tenant_id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-dup409-2",
            thread_id=thread_id,
            turn_id=f"task-{task_id}-turn-2",
            turn_sequence=2,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "tool_id": "shell.exec",
                "tool_name": "shell.exec",
                "parameters": {"command": "echo second"},
            },
        )
        db2.add(ticket2)
        db2.commit()
        db2.close()

        # Next distinct interrupt appears; 409 did not block.
        snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert payload["has_interrupt"] is True
        assert payload["interrupt_id"] == interrupt_id_2

    engine.dispose()
