"""Tests for interrupt ticket state transitions and lookup helper behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.hitl import InterruptTicketState
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    InterruptTicketClaimConflictError,
    InterruptTicketService,
    resolve_interrupt_tool_call_id_best_effort,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def test_create_or_update_pending_persists_and_updates_fields() -> None:
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        created = service.create_or_update_pending(
            interrupt_id="int-1",
            task_id=1,
            graph_name="deep_reasoning",
            interrupt_type="tool_approval",
            checkpoint_id="cp-1",
            thread_id="thread-1",
            turn_id="turn-1",
            turn_sequence=1,
            tool_call_id="tool-1",
            payload_snapshot={"kind": "initial"},
        )
        assert created.state == InterruptTicketState.PENDING
        assert created.checkpoint_id == "cp-1"
        assert created.payload_snapshot == {"kind": "initial"}

        updated = service.create_or_update_pending(
            interrupt_id="int-1",
            task_id=1,
            graph_name="deep_reasoning",
            interrupt_type="tool_approval",
            checkpoint_id="cp-2",
            turn_sequence=2,
            payload_snapshot={"kind": "updated"},
        )
        assert updated.id == created.id
        assert updated.state == InterruptTicketState.PENDING
        assert updated.checkpoint_id == "cp-2"
        assert updated.turn_sequence == 2
        assert updated.payload_snapshot == {"kind": "updated"}
    finally:
        db.close()
        engine.dispose()


def test_claim_for_resume_is_atomic_and_rejects_duplicate_claim() -> None:
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        service.create_or_update_pending(
            interrupt_id="int-2",
            task_id=2,
            graph_name="simple_tool",
            interrupt_type="plan_review",
        )

        claimed = service.claim_for_resume(interrupt_id="int-2", task_id=2)
        assert claimed.state == InterruptTicketState.RESUMING

        with pytest.raises(InterruptTicketClaimConflictError):
            service.claim_for_resume(interrupt_id="int-2", task_id=2)
    finally:
        db.close()
        engine.dispose()


def test_clarify_ticket_lifecycle_and_conflict_behavior() -> None:
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        created = service.create_or_update_pending(
            interrupt_id="int-clarify-1",
            task_id=21,
            graph_name="deep_reasoning",
            interrupt_type="clarify_request",
            checkpoint_id="cp-clarify-1",
            payload_snapshot={
                "type": "clarify_request",
                "questions": [
                    {
                        "question_id": "target",
                        "input_type": "select",
                        "label": "What host should I scan?",
                        "options": ["10.0.0.1", "10.0.0.2"],
                    }
                ],
            },
        )
        assert created.state == InterruptTicketState.PENDING
        assert created.interrupt_type == "clarify_request"

        claimed = service.claim_for_resume(interrupt_id="int-clarify-1", task_id=21)
        assert claimed.state == InterruptTicketState.RESUMING

        with pytest.raises(InterruptTicketClaimConflictError):
            service.claim_for_resume(interrupt_id="int-clarify-1", task_id=21)

        resumed = service.mark_resumed(interrupt_id="int-clarify-1", task_id=21)
        assert resumed.state == InterruptTicketState.RESUMED

        completed = service.mark_completed(interrupt_id="int-clarify-1", task_id=21)
        assert completed.state == InterruptTicketState.COMPLETED
    finally:
        db.close()
        engine.dispose()


def test_state_transitions_and_expire_stale() -> None:
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        service.create_or_update_pending(
            interrupt_id="int-3",
            task_id=3,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
        )
        service.claim_for_resume(interrupt_id="int-3", task_id=3)
        resumed = service.mark_resumed(interrupt_id="int-3", task_id=3)
        assert resumed.state == InterruptTicketState.RESUMED
        completed = service.mark_completed(interrupt_id="int-3", task_id=3)
        assert completed.state == InterruptTicketState.COMPLETED

        with pytest.raises(InterruptTicketClaimConflictError):
            service.mark_resumed(interrupt_id="int-3", task_id=3)

        stale = service.create_or_update_pending(
            interrupt_id="int-4",
            task_id=4,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
        )
        stale.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()

        expired_count = service.expire_stale(stale_before=datetime.now(timezone.utc) - timedelta(minutes=5))
        assert expired_count == 1
        refreshed = service.create_or_update_pending(
            interrupt_id="int-4",
            task_id=4,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
        )
        assert refreshed.state == InterruptTicketState.EXPIRED
    finally:
        db.close()
        engine.dispose()


def test_observed_upsert_does_not_downgrade_resumed_to_pending() -> None:
    """REGRESSION: create_or_update_pending must never downgrade RESUMED to PENDING.

    Original bug: stale observation could re-pend already-resumed tickets.
    CI must fail if this guard is removed; single-authority lifecycle depends on it.
    """
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        service.create_or_update_pending(
            interrupt_id="int-5",
            task_id=5,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
            payload_snapshot={"version": "initial"},
        )
        service.claim_for_resume(interrupt_id="int-5", task_id=5)
        service.mark_resumed(interrupt_id="int-5", task_id=5)

        stale_observation = service.create_or_update_pending(
            interrupt_id="int-5",
            task_id=5,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            payload_snapshot={"version": "stale"},
        )

        assert stale_observation.state == InterruptTicketState.RESUMED
        assert stale_observation.interrupt_type == "plan_review"
        assert stale_observation.graph_name == "deep_reasoning"
        assert stale_observation.payload_snapshot == {"version": "initial"}
    finally:
        db.close()
        engine.dispose()


def test_observed_upsert_does_not_downgrade_resuming_to_pending() -> None:
    """REGRESSION: observed upsert must never move RESUMING back to PENDING."""
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        service.create_or_update_pending(
            interrupt_id="int-5b",
            task_id=55,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
            payload_snapshot={"version": "initial"},
        )
        service.claim_for_resume(interrupt_id="int-5b", task_id=55)

        stale_observation = service.create_or_update_pending(
            interrupt_id="int-5b",
            task_id=55,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
            payload_snapshot={"version": "stale"},
        )

        assert stale_observation.state == InterruptTicketState.RESUMING
        assert stale_observation.interrupt_type == "plan_review"
        assert stale_observation.graph_name == "deep_reasoning"
        assert stale_observation.payload_snapshot == {"version": "initial"}
    finally:
        db.close()
        engine.dispose()


def test_observed_upsert_does_not_downgrade_completed_or_failed_to_pending() -> None:
    """REGRESSION: create_or_update_pending must never downgrade COMPLETED/FAILED to PENDING."""
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        for terminal_state, interrupt_id, task_id in [
            (InterruptTicketState.COMPLETED, "int-completed", 98),
            (InterruptTicketState.FAILED, "int-failed", 99),
        ]:
            service.create_or_update_pending(
                interrupt_id=interrupt_id,
                task_id=task_id,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
            )
            service.claim_for_resume(interrupt_id=interrupt_id, task_id=task_id)
            service.mark_resumed(interrupt_id=interrupt_id, task_id=task_id)
            if terminal_state == InterruptTicketState.COMPLETED:
                service.mark_completed(interrupt_id=interrupt_id, task_id=task_id)
            else:
                service.mark_failed(interrupt_id=interrupt_id, task_id=task_id)

            stale = service.create_or_update_pending(
                interrupt_id=interrupt_id,
                task_id=task_id,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
                payload_snapshot={"stale": True},
            )
            assert stale.state == terminal_state
    finally:
        db.close()
        engine.dispose()


def test_create_or_update_pending_returns_existing_pending_when_task_already_has_pending() -> None:
    """Task-level invariant: never create a second PENDING ticket for one task."""
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        first = service.create_or_update_pending(
            interrupt_id="int-pending-a",
            task_id=77,
            graph_name="simple_tool",
            interrupt_type="tool_approval",
        )

        second_attempt = service.create_or_update_pending(
            interrupt_id="int-pending-b",
            task_id=77,
            graph_name="deep_reasoning",
            interrupt_type="plan_review",
        )

        assert second_attempt.id == first.id
        assert second_attempt.interrupt_id == "int-pending-a"
        assert second_attempt.state == InterruptTicketState.PENDING
    finally:
        db.close()
        engine.dispose()


def test_mark_pending_rejects_resumed_state() -> None:
    engine, db = _build_session()
    try:
        service = InterruptTicketService(db)
        service.create_or_update_pending(
            interrupt_id="int-6",
            task_id=6,
            graph_name="deep_reasoning",
            interrupt_type="clarify_request",
        )
        service.claim_for_resume(interrupt_id="int-6", task_id=6)
        service.mark_resumed(interrupt_id="int-6", task_id=6)

        with pytest.raises(InterruptTicketClaimConflictError):
            service.mark_pending(interrupt_id="int-6", task_id=6)
    finally:
        db.close()
        engine.dispose()


def test_resolve_interrupt_tool_call_id_best_effort_returns_stripped_value() -> None:
    row = SimpleNamespace(tool_call_id=" tool-call-1 ")
    query = Mock()
    query.filter.return_value = query
    query.first.return_value = row

    session = Mock()
    session.query.return_value = query

    resolved = resolve_interrupt_tool_call_id_best_effort(
        task_id=7,
        interrupt_id="interrupt-1",
        session_factory=lambda: session,
    )

    assert resolved == "tool-call-1"
    session.close.assert_called_once()


def test_resolve_interrupt_tool_call_id_best_effort_short_circuits_on_blank_interrupt_id() -> None:
    session_factory = Mock()

    resolved = resolve_interrupt_tool_call_id_best_effort(
        task_id=7,
        interrupt_id=" ",
        session_factory=session_factory,
    )

    assert resolved is None
    session_factory.assert_not_called()


def test_resolve_interrupt_tool_call_id_best_effort_returns_none_on_query_error() -> None:
    session = Mock()
    session.query.side_effect = RuntimeError("query failed")

    resolved = resolve_interrupt_tool_call_id_best_effort(
        task_id=7,
        interrupt_id="interrupt-1",
        session_factory=lambda: session,
    )

    assert resolved is None
    session.close.assert_called_once()
