"""Regression tests for idempotent task resume and explicit interrupt handling."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.models.hitl import InterruptTicket, InterruptTicketState, TurnWorkflow
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import tasks as tasks_router
from backend.routers.tasks.interrupts import TaskInterruptSnapshotResponse
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id


class _UnexpectedInterruptServiceCall:
    async def get_pending_interrupt(self, task_id: int, graph_name: str | None = None):
        raise AssertionError("explicit interrupt_id path should not query pending interrupt")


async def _noop_resume_generation(**kwargs):
    return None


def _drain_task(coro):
    coro.close()
    return None


def _create_tenant(db, *, slug: str) -> Tenant:
    tenant = Tenant(slug=slug, name=slug.replace("-", " ").title())
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _create_membership(db, *, tenant_id: int, user_id: int) -> None:
    db.add(TenantMembership(tenant_id=tenant_id, user_id=user_id, role="owner", status="active"))
    db.commit()


def test_resume_request_accepts_clarify_interrupt_type() -> None:
    request = tasks_router.ResumeRequest(
        interrupt_id="deep_reasoning:checkpoint:cp-clarify-1",
        interrupt_type="clarify_request",
        graph_name="deep_reasoning",
        response={"action": "answer", "answers": {"target": "10.0.0.1"}},
    )
    assert request.interrupt_type == "clarify_request"
    assert request.response.action == "answer"


def test_resume_request_rejects_unknown_interrupt_type() -> None:
    with pytest.raises(ValidationError):
        tasks_router.ResumeRequest(
            interrupt_id="deep_reasoning:checkpoint:cp-invalid-1",
            interrupt_type="invalid_type",
            graph_name="deep_reasoning",
            response={"action": "approve"},
        )


def test_interrupt_snapshot_response_accepts_clarify_interrupt_type() -> None:
    snapshot = TaskInterruptSnapshotResponse(
        has_interrupt=True,
        task_id=42,
        interrupt_id="deep_reasoning:checkpoint:cp-clarify-1",
        interrupt_type="clarify_request",
        payload={"type": "clarify_request"},
        resumable=True,
    )
    assert snapshot.interrupt_type == "clarify_request"


@pytest.mark.asyncio
async def test_resume_graph_execution_rejects_clarify_non_answer_action() -> None:
    request = tasks_router.ResumeRequest(
        interrupt_id="deep_reasoning:checkpoint:cp-clarify-invalid-action",
        interrupt_type="clarify_request",
        graph_name="deep_reasoning",
        response={"action": "approve", "answers": {"target": "10.0.0.1"}},
    )
    with pytest.raises(HTTPException) as error:
        await tasks_router.resume_graph_execution(
            task_id=1,
            request=request,
            current_user=object(),
            db=None,
        )
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_resume_graph_execution_rejects_clarify_empty_answers() -> None:
    request = tasks_router.ResumeRequest(
        interrupt_id="deep_reasoning:checkpoint:cp-clarify-empty-answers",
        interrupt_type="clarify_request",
        graph_name="deep_reasoning",
        response={"action": "answer", "answers": {}},
    )
    with pytest.raises(HTTPException) as error:
        await tasks_router.resume_graph_execution(
            task_id=1,
            request=request,
            current_user=object(),
            db=None,
        )
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_resume_graph_execution_conflicts_on_duplicate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        user = User(username="resume_user", password="x", email="resume@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)

        tenant = _create_tenant(db, slug="resume-duplicate")
        _create_membership(db, tenant_id=tenant.id, user_id=user.id)
        task = Task(user_id=user.id, tenant_id=tenant.id, name="resume-task")
        db.add(task)
        db.commit()
        db.refresh(task)
        thread_id = format_graph_thread_id(task.graph_thread_id, task_id=task.id)
        interrupt_id = "simple_tool:checkpoint:cp-dup-1"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=tenant.id,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            checkpoint_id="cp-dup-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "tool_approval",
                "run_id": "cp-dup-1",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 7001,
                "conversation_id": "conv-dup-1",
            },
        )
        db.add(ticket)
        db.commit()

        monkeypatch.setattr(
            tasks_router,
            "get_interrupt_state_service",
            lambda: _UnexpectedInterruptServiceCall(),
        )
        monkeypatch.setattr(
            "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
            _noop_resume_generation,
        )
        monkeypatch.setattr(tasks_router, "_schedule_background_task", _drain_task)

        request = tasks_router.ResumeRequest(
            interrupt_id=interrupt_id,
            interrupt_type="tool_approval",
            graph_name="simple_tool",
            response={"action": "approve"},
        )

        first = await tasks_router.resume_graph_execution(
            task_id=task.id,
            request=request,
            current_user=user,
            db=db,
        )
        assert first["status"] == "resumed"
        assert first["interrupt_id"] == interrupt_id

        with pytest.raises(HTTPException) as second_error:
            await tasks_router.resume_graph_execution(
                task_id=task.id,
                request=request,
                current_user=user,
                db=db,
            )
        assert second_error.value.status_code == 409

        rows = db.query(TurnWorkflow).filter(TurnWorkflow.task_id == task.id).all()
        assert len(rows) == 1
        assert rows[0].state == "RESUMED"
        refreshed_ticket = (
            db.query(InterruptTicket)
            .filter(InterruptTicket.task_id == task.id, InterruptTicket.interrupt_id == interrupt_id)
            .one()
        )
        assert refreshed_ticket.state == InterruptTicketState.RESUMING
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_resume_graph_execution_conflicts_on_duplicate_clarify(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        user = User(username="clarify_resume_user", password="x", email="clarify-resume@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)

        tenant = _create_tenant(db, slug="clarify-resume-duplicate")
        _create_membership(db, tenant_id=tenant.id, user_id=user.id)
        task = Task(user_id=user.id, tenant_id=tenant.id, name="clarify-resume-task")
        db.add(task)
        db.commit()
        db.refresh(task)
        thread_id = format_graph_thread_id(task.graph_thread_id, task_id=task.id)
        interrupt_id = "deep_reasoning:checkpoint:cp-clarify-dup-1"
        ticket = InterruptTicket(
            interrupt_id=interrupt_id,
            task_id=task.id,
            tenant_id=tenant.id,
            graph_name="deep_reasoning",
            interrupt_type="clarify_request",
            checkpoint_id="cp-clarify-dup-1",
            thread_id=thread_id,
            turn_id=f"task-{task.id}-turn-1",
            turn_sequence=1,
            state=InterruptTicketState.PENDING,
            payload_snapshot={
                "type": "clarify_request",
                "run_id": "cp-clarify-dup-1",
                "turn_id": f"task-{task.id}-turn-1",
                "turn_sequence": 1,
                "reserved_message_id": 8101,
                "conversation_id": "conv-clarify-dup-1",
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

        monkeypatch.setattr(
            tasks_router,
            "get_interrupt_state_service",
            lambda: _UnexpectedInterruptServiceCall(),
        )
        monkeypatch.setattr(
            "backend.services.langgraph_chat.execution.turn_service.run_resume_generation",
            _noop_resume_generation,
        )
        monkeypatch.setattr(tasks_router, "_schedule_background_task", _drain_task)

        request = tasks_router.ResumeRequest(
            interrupt_id=interrupt_id,
            interrupt_type="clarify_request",
            graph_name="deep_reasoning",
            response={"action": "answer", "answers": {"target": "10.0.0.1"}},
        )

        first = await tasks_router.resume_graph_execution(
            task_id=task.id,
            request=request,
            current_user=user,
            db=db,
        )
        assert first["status"] == "resumed"
        assert first["interrupt_id"] == interrupt_id

        with pytest.raises(HTTPException) as second_error:
            await tasks_router.resume_graph_execution(
                task_id=task.id,
                request=request,
                current_user=user,
                db=db,
            )
        assert second_error.value.status_code == 409

        rows = db.query(TurnWorkflow).filter(TurnWorkflow.task_id == task.id).all()
        assert len(rows) == 1
        assert rows[0].state == "RESUMED"
        refreshed_ticket = (
            db.query(InterruptTicket)
            .filter(InterruptTicket.task_id == task.id, InterruptTicket.interrupt_id == interrupt_id)
            .one()
        )
        assert refreshed_ticket.state == InterruptTicketState.RESUMING
    finally:
        db.close()
        engine.dispose()
