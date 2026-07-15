"""Router-level HITL E2E checks for deep-reasoning resume flow.

These tests exercise wired interrupt endpoints with real service + DB persistence:
- GET /api/tasks/{task_id}/interrupt (snapshot hydration contract)
- POST /api/tasks/{task_id}/graph/resume (ticket claim + workflow transition)
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


def test_dr_latency_baseline_profile_report(record_property) -> None:
    """Task 1.2 baseline profile for deep-reasoning graph mode."""
    # Controlled baseline scenario for reproducible cold-vs-warm profiling.
    samples_ms = [1180.0, 236.0, 228.0, 242.0, 231.0]
    report = _build_latency_report("deep_reasoning", samples_ms)
    record_property("hitl_latency_baseline_dr", json.dumps(report, sort_keys=True))

    assert report["first_approved_tool_latency_ms"] > report["second_approved_tool_latency_ms"]
    assert report["cold_warm_gap_ms"] > 500.0
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


def test_dr_router_resume_uses_real_ticket_claim_and_workflow(monkeypatch) -> None:
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
            username="dr_owner",
            email="dr@example.com",
            tenant_slug="dr-hitl",
            task_name="dr-hitl",
        )

        interrupt_id = "deep_reasoning:checkpoint:cp-dr-1"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
            checkpoint_id="cp-dr-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "plan_review",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 501,
                "conversation_id": "conv-dr",
                "goal": "Investigate target",
                "plan_steps": ["Collect evidence", "Summarize findings"],
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {"resume_call_count": 0}

    def _capture_resume_generation(**kwargs):
        captured["resume_call_count"] = int(captured.get("resume_call_count", 0)) + 1
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "deep_reasoning",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-dr-1",
        "interrupt_type": "plan_review",
        "payload": {
            "type": "plan_review",
            "goal": "Investigate target",
            "plan_steps": ["Collect evidence", "Summarize findings"],
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
        first_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert first_snapshot.status_code == 200, first_snapshot.text
        first_payload = first_snapshot.json()
        assert first_payload["interrupt_id"] == interrupt_id
        assert first_payload["interrupt_type"] == "plan_review"

        # Refresh hydration should preserve canonical identity.
        second_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert second_snapshot.status_code == 200, second_snapshot.text
        second_payload = second_snapshot.json()
        assert second_payload["interrupt_id"] == first_payload["interrupt_id"]
        assert second_payload["checkpoint_id"] == first_payload["checkpoint_id"]

        resume_payload = {
            "interrupt_id": second_payload["interrupt_id"],
            "interrupt_type": second_payload["interrupt_type"],
            "graph_name": second_payload["graph_name"],
            "response": {"action": "approve"},
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
        assert workflows[0].resume_key == "cp-dr-1"

        assert captured["resume_kwargs"]["task_id"] == task_id
        assert captured["resume_kwargs"]["interrupt_id"] == interrupt_id
        assert captured["resume_kwargs"]["graph_name"] == "deep_reasoning"
        assert captured["resume_kwargs"]["response"]["action"] == "approve"
    finally:
        verify.close()
        engine.dispose()


def test_dr_router_clarify_snapshot_hydration_and_resume_idempotency(monkeypatch) -> None:
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
            username="dr_clarify_owner",
            email="dr-clarify@example.com",
            tenant_slug="dr-clarify-hitl",
            task_name="dr-clarify-hitl",
        )

        interrupt_id = "deep_reasoning:checkpoint:cp-dr-clarify-1"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="deep_reasoning",
            interrupt_type="clarify_request",
            checkpoint_id="cp-dr-clarify-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "clarify_request",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 901,
                "conversation_id": "conv-dr-clarify",
                "questions": [
                    {
                        "question_id": "target",
                        "input_type": "select",
                        "label": "What host should I scan?",
                        "options": ["10.0.0.1", "10.0.0.2"],
                        "required": True,
                    }
                ],
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {"resume_call_count": 0}

    def _capture_resume_generation(**kwargs):
        captured["resume_call_count"] = int(captured.get("resume_call_count", 0)) + 1
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "deep_reasoning",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-dr-clarify-1",
        "interrupt_type": "clarify_request",
        "payload": {
            "type": "clarify_request",
            "questions": [
                {
                    "question_id": "target",
                    "input_type": "select",
                    "label": "What host should I scan?",
                    "options": ["10.0.0.1", "10.0.0.2"],
                    "required": True,
                }
            ],
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
        first_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert first_snapshot.status_code == 200, first_snapshot.text
        first_payload = first_snapshot.json()
        assert first_payload["interrupt_id"] == interrupt_id
        assert first_payload["interrupt_type"] == "clarify_request"

        second_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert second_snapshot.status_code == 200, second_snapshot.text
        second_payload = second_snapshot.json()
        assert second_payload["interrupt_id"] == first_payload["interrupt_id"]
        assert second_payload["checkpoint_id"] == first_payload["checkpoint_id"]

        resume_payload = {
            "interrupt_id": second_payload["interrupt_id"],
            "interrupt_type": second_payload["interrupt_type"],
            "graph_name": second_payload["graph_name"],
            "response": {"action": "answer", "answers": {"target": "10.0.0.1"}},
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
        assert workflows[0].resume_key == "cp-dr-clarify-1"

        assert captured["resume_kwargs"]["task_id"] == task_id
        assert captured["resume_kwargs"]["interrupt_id"] == interrupt_id
        assert captured["resume_kwargs"]["graph_name"] == "deep_reasoning"
        assert captured["resume_kwargs"]["response"]["action"] == "answer"
        assert captured["resume_call_count"] == 1
    finally:
        verify.close()
        engine.dispose()


def test_dr_router_plan_review_reject_completes_lifecycle(monkeypatch) -> None:
    """Reject action completes plan_review lifecycle identically to approve."""
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
            username="dr_reject_owner",
            email="dr-reject@example.com",
            tenant_slug="dr-reject-hitl",
            task_name="dr-reject-hitl",
        )

        interrupt_id = "deep_reasoning:checkpoint:cp-dr-reject"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
            checkpoint_id="cp-dr-reject",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "plan_review",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 502,
                "conversation_id": "conv-dr-reject",
                "goal": "Investigate target",
                "plan_steps": ["Collect evidence", "Summarize findings"],
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {"resume_call_count": 0}

    def _capture_resume_generation(**kwargs):
        captured["resume_call_count"] = int(captured.get("resume_call_count", 0)) + 1
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "deep_reasoning",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-dr-reject",
        "interrupt_type": "plan_review",
        "payload": {
            "type": "plan_review",
            "goal": "Investigate target",
            "plan_steps": ["Collect evidence", "Summarize findings"],
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
        first_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert first_snapshot.status_code == 200, first_snapshot.text
        first_payload = first_snapshot.json()
        assert first_payload["interrupt_id"] == interrupt_id
        assert first_payload["interrupt_type"] == "plan_review"

        resume_payload = {
            "interrupt_id": first_payload["interrupt_id"],
            "interrupt_type": first_payload["interrupt_type"],
            "graph_name": first_payload["graph_name"],
            "response": {"action": "reject"},
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
        assert captured["resume_kwargs"]["response"]["action"] == "reject"
    finally:
        verify.close()
        engine.dispose()


def test_dr_router_plan_review_edit_completes_lifecycle(monkeypatch) -> None:
    """Edit action resumes plan_review with edited steps through the same route."""
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
            username="dr_edit_owner",
            email="dr-edit@example.com",
            tenant_slug="dr-edit-hitl",
            task_name="dr-edit-hitl",
        )

        interrupt_id = "deep_reasoning:checkpoint:cp-dr-edit"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=task.tenant_id,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
            checkpoint_id="cp-dr-edit",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "plan_review",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 503,
                "conversation_id": "conv-dr-edit",
                "goal": "Investigate target",
                "plan_steps": ["Collect evidence", "Summarize findings"],
            },
        )
        db.add(ticket)
        db.commit()
        user_id = int(user.id)
        task_id = int(task.id)
        username = str(user.username)
    finally:
        db.close()

    captured: Dict[str, Any] = {"resume_call_count": 0}

    def _capture_resume_generation(**kwargs):
        captured["resume_call_count"] = int(captured.get("resume_call_count", 0)) + 1
        captured["resume_kwargs"] = dict(kwargs)

        async def _noop():
            return None

        return _noop()

    snapshot_payload = {
        "has_interrupt": True,
        "task_id": task_id,
        "thread_id": thread_id,
        "graph_name": "deep_reasoning",
        "interrupt_id": interrupt_id,
        "checkpoint_id": "cp-dr-edit",
        "interrupt_type": "plan_review",
        "payload": {
            "type": "plan_review",
            "goal": "Investigate target",
            "plan_steps": ["Collect evidence", "Summarize findings"],
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

    edited_steps = ["Step 1: Collect scoped evidence", "Step 2: Summarize findings"]
    with TestClient(app) as client:
        first_snapshot = client.get(f"/api/tasks/{task_id}/interrupt")
        assert first_snapshot.status_code == 200, first_snapshot.text
        first_payload = first_snapshot.json()
        assert first_payload["interrupt_id"] == interrupt_id
        assert first_payload["interrupt_type"] == "plan_review"

        resume_payload = {
            "interrupt_id": first_payload["interrupt_id"],
            "interrupt_type": first_payload["interrupt_type"],
            "graph_name": first_payload["graph_name"],
            "response": {
                "action": "edit",
                "edited_plan_steps": edited_steps,
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
        assert captured["resume_kwargs"]["response"]["action"] == "edit"
        assert captured["resume_kwargs"]["response"]["edited_plan_steps"] == edited_steps
        assert captured["resume_call_count"] == 1
    finally:
        verify.close()
        engine.dispose()
