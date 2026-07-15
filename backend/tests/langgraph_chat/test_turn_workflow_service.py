"""Tests for turn workflow transitions and best-effort lookup helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    CheckpointRetryClaimResult,
    DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS,
    TurnWorkflowService,
    TurnWorkflowState,
    build_checkpoint_retry_identity,
    resolve_reserved_message_id_from_workflow_best_effort,
    resolve_turn_id_from_workflow_best_effort,
    sanitize_previous_failure,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    _seed_task_rows(db)
    return engine, db


def _seed_task_rows(db, *, task_ids=range(1, 21)) -> None:
    for task_id in task_ids:
        db.add(Task(id=task_id, user_id=1, tenant_id=1, name=f"task-{task_id}"))
    db.commit()


def test_turn_workflow_transitions_running_waiting_resumed_completed() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        started = service.start_turn(
            task_id=1,
            conversation_id="conv-1",
            turn_id="task-1-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=11,
            metadata={"source": "test"},
        )
        assert started.state == TurnWorkflowState.RUNNING.value

        waiting = service.mark_waiting_for_human(
            workflow_id=started.id,
            checkpoint_id="cp-1",
            interrupt_type="tool_approval",
            graph_name="simple_tool",
            reserved_message_id=11,
            resume_key="cp-1",
        )
        assert waiting is not None
        assert waiting.state == TurnWorkflowState.WAITING_FOR_HUMAN.value

        resumed = service.try_begin_resume(
            task_id=1,
            resume_key="cp-1",
            checkpoint_id="cp-1",
            graph_name="simple_tool",
            reserved_message_id=11,
        )
        assert resumed is not None
        assert resumed.state == TurnWorkflowState.RESUMED.value

        completed = service.mark_completed(workflow_id=resumed.id)
        assert completed is not None
        assert completed.state == TurnWorkflowState.COMPLETED.value
    finally:
        db.close()
        engine.dispose()


def test_mark_resumed_transitions_waiting_to_resumed() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        waiting = service.ensure_waiting_workflow(
            task_id=4,
            conversation_id="conv-4",
            turn_id="task-4-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            checkpoint_id="cp-4",
            interrupt_type="tool_approval",
            reserved_message_id=44,
            resume_key="cp-4",
        )
        assert waiting.state == TurnWorkflowState.WAITING_FOR_HUMAN.value

        resumed = service.mark_resumed(
            workflow_id=waiting.id,
            resume_key="cp-4",
            checkpoint_id="cp-4",
            graph_name="simple_tool",
            reserved_message_id=44,
        )
        assert resumed is not None
        assert resumed.state == TurnWorkflowState.RESUMED.value
    finally:
        db.close()
        engine.dispose()


def test_try_begin_resume_is_cas_idempotent() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        waiting = service.ensure_waiting_workflow(
            task_id=2,
            conversation_id="conv-2",
            turn_id="task-2-turn-1",
            turn_sequence=1,
            graph_name="deep_reasoning",
            checkpoint_id="cp-2",
            interrupt_type="plan_review",
            reserved_message_id=22,
            resume_key="cp-2",
        )
        assert waiting.state == TurnWorkflowState.WAITING_FOR_HUMAN.value

        first = service.try_begin_resume(
            task_id=2,
            resume_key="cp-2",
            checkpoint_id="cp-2",
            graph_name="deep_reasoning",
            reserved_message_id=22,
        )
        assert first is not None
        assert first.state == TurnWorkflowState.RESUMED.value

        second = service.try_begin_resume(
            task_id=2,
            resume_key="cp-2",
            checkpoint_id="cp-2",
            graph_name="deep_reasoning",
            reserved_message_id=22,
        )
        assert second is None
    finally:
        db.close()
        engine.dispose()


def test_mark_failed_persists_checkpoint_anchor_for_retryable_failure() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        started = service.start_turn(
            task_id=19,
            conversation_id="conv-19",
            turn_id="task-19-turn-1",
            turn_sequence=1,
            graph_name="simple_tool_execution",
            reserved_message_id=191,
        )

        failed = service.mark_failed(
            workflow_id=started.id,
            checkpoint_id="  ckpt-non-hitl-19  ",
            graph_name="simple_tool",
            metadata={"retryable": True, "retry_mode": "checkpoint"},
        )

        assert failed is not None
        assert failed.state == TurnWorkflowState.FAILED.value
        assert failed.checkpoint_id == "ckpt-non-hitl-19"
        assert failed.graph_name == "simple_tool"
        assert failed.workflow_metadata["retryable"] is True
        assert failed.workflow_metadata["retry_mode"] == "checkpoint"
    finally:
        db.close()
        engine.dispose()


def test_mark_failed_can_replace_stale_error_metadata_for_refusal() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        started = service.start_turn(
            task_id=18,
            conversation_id="conv-18",
            turn_id="task-18-turn-1",
            turn_sequence=1,
            graph_name="normal_chat",
            reserved_message_id=181,
            metadata={"error": "stale", "error_code": "stale"},
        )

        failed = service.mark_failed(
            workflow_id=started.id,
            metadata={
                "outcome_type": "provider_refusal",
                "retryable": False,
                "refusal": {"provider": "openai", "model": "gpt-5.6"},
            },
            replace_metadata=True,
        )

        assert failed is not None
        assert failed.workflow_metadata["outcome_type"] == "provider_refusal"
        assert "error" not in failed.workflow_metadata
        assert "error_code" not in failed.workflow_metadata
    finally:
        db.close()
        engine.dispose()


def test_ensure_waiting_workflow_does_not_downgrade_resumed_state() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        waiting = service.ensure_waiting_workflow(
            task_id=5,
            conversation_id="conv-5",
            turn_id="task-5-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            checkpoint_id="cp-5",
            interrupt_type="tool_approval",
            reserved_message_id=55,
            resume_key="cp-5",
        )
        resumed = service.try_begin_resume(
            task_id=5,
            resume_key="cp-5",
            checkpoint_id="cp-5",
            graph_name="simple_tool",
            reserved_message_id=55,
        )
        assert resumed is not None
        assert resumed.state == TurnWorkflowState.RESUMED.value

        ensured = service.ensure_waiting_workflow(
            task_id=5,
            conversation_id="conv-5",
            turn_id="task-5-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            checkpoint_id="cp-5",
            interrupt_type="tool_approval",
            reserved_message_id=55,
            resume_key="cp-5",
        )
        assert ensured.state == TurnWorkflowState.RESUMED.value
    finally:
        db.close()
        engine.dispose()


def test_try_begin_resume_survives_service_reinstantiation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db_one = session_factory()
    try:
        _seed_task_rows(db_one, task_ids=(3,))
        service_one = TurnWorkflowService(db_one)
        service_one.ensure_waiting_workflow(
            task_id=3,
            conversation_id="conv-3",
            turn_id="task-3-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            checkpoint_id="cp-3",
            interrupt_type="tool_approval",
            reserved_message_id=33,
            resume_key="cp-3",
        )
    finally:
        db_one.close()

    db_two = session_factory()
    try:
        service_two = TurnWorkflowService(db_two)
        resumed = service_two.try_begin_resume(
            task_id=3,
            resume_key="cp-3",
            checkpoint_id="cp-3",
            graph_name="simple_tool",
            reserved_message_id=33,
        )
        assert resumed is not None
        assert resumed.state == TurnWorkflowState.RESUMED.value
    finally:
        db_two.close()
        engine.dispose()


def test_resolve_reserved_message_id_best_effort_returns_int_and_closes_session() -> (
    None
):
    session = Mock()
    service = Mock()
    service.get_workflow.return_value = SimpleNamespace(reserved_message_id=77)

    with patch(
        "backend.services.langgraph_chat.checkpoint.turn_workflow_service.TurnWorkflowService",
        return_value=service,
    ):
        resolved = resolve_reserved_message_id_from_workflow_best_effort(
            42,
            session_factory=lambda: session,
        )

    assert resolved == 77
    session.close.assert_called_once()


def test_resolve_reserved_message_id_best_effort_returns_none_for_non_int() -> None:
    session = Mock()
    service = Mock()
    service.get_workflow.return_value = SimpleNamespace(reserved_message_id="77")

    with patch(
        "backend.services.langgraph_chat.checkpoint.turn_workflow_service.TurnWorkflowService",
        return_value=service,
    ):
        resolved = resolve_reserved_message_id_from_workflow_best_effort(
            42,
            session_factory=lambda: session,
        )

    assert resolved is None
    session.close.assert_called_once()


def test_resolve_turn_id_best_effort_returns_stripped_id_and_closes_session() -> None:
    session = Mock()
    service = Mock()
    service.get_workflow.return_value = SimpleNamespace(turn_id=" task-8-turn-4 ")

    with patch(
        "backend.services.langgraph_chat.checkpoint.turn_workflow_service.TurnWorkflowService",
        return_value=service,
    ):
        resolved = resolve_turn_id_from_workflow_best_effort(
            42,
            session_factory=lambda: session,
        )

    assert resolved == "task-8-turn-4"
    session.close.assert_called_once()


def test_resolve_turn_id_best_effort_returns_none_for_empty_or_non_string() -> None:
    session = Mock()
    service = Mock()
    service.get_workflow.return_value = SimpleNamespace(turn_id=" ")

    with patch(
        "backend.services.langgraph_chat.checkpoint.turn_workflow_service.TurnWorkflowService",
        return_value=service,
    ):
        resolved = resolve_turn_id_from_workflow_best_effort(
            42,
            session_factory=lambda: session,
        )

    assert resolved is None
    session.close.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 1.1: build_checkpoint_retry_identity
# ---------------------------------------------------------------------------


def test_build_checkpoint_retry_identity_returns_canonical_fields() -> None:
    workflow = SimpleNamespace(
        id=14,
        turn_id="task-1-turn-3",
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
        state="RETRYING",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )

    identity = build_checkpoint_retry_identity(workflow, task_id=1)

    assert identity == {
        "task_id": 1,
        "turn_id": "task-1-turn-3",
        "workflow_id": 14,
        "graph_name": "simple_tool",
        "checkpoint_id": "ckpt-stable-abc123",
        "retry_mode": "checkpoint",
        "retry_attempt": 1,
        "retry_max_attempts": 2,
        "state": "retrying",
        "already_in_flight": False,
    }


def test_build_checkpoint_retry_identity_flags_already_in_flight_and_normalizes_checkpoint() -> (
    None
):
    workflow = SimpleNamespace(
        id=14,
        turn_id="  task-1-turn-3  ",
        graph_name="  simple_tool  ",
        checkpoint_id="  ckpt-stable-abc123  ",
        state="retrying",
        workflow_metadata={
            "retry_mode": "checkpoint",
            "retry_attempt_count": "1",
            "retry_max_attempts": "2",
        },
    )

    identity = build_checkpoint_retry_identity(
        workflow, task_id=1, already_in_flight=True
    )

    assert identity["checkpoint_id"] == "ckpt-stable-abc123"
    assert identity["graph_name"] == "simple_tool"
    assert identity["turn_id"] == "task-1-turn-3"
    assert identity["already_in_flight"] is True
    assert identity["retry_attempt"] == 1
    assert identity["retry_max_attempts"] == 2


def test_build_checkpoint_retry_identity_falls_back_to_default_max_attempts() -> None:
    workflow = SimpleNamespace(
        id=99,
        turn_id="task-9-turn-1",
        graph_name="simple_tool",
        checkpoint_id=None,
        state="FAILED",
        workflow_metadata=None,
    )

    identity = build_checkpoint_retry_identity(workflow, task_id=9)

    assert identity["retry_attempt"] == 0
    assert identity["retry_max_attempts"] == DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS
    assert identity["checkpoint_id"] is None
    assert identity["state"] == "failed"
    assert identity["already_in_flight"] is False
    assert identity["retry_mode"] == "checkpoint"


# ---------------------------------------------------------------------------
# Phase 1.1: sanitize_previous_failure
# ---------------------------------------------------------------------------


def test_sanitize_previous_failure_keeps_whitelisted_fields_and_drops_secrets() -> None:
    raw = {
        "error_code": "tool_argument_invalid",
        "failure_stage": "graph_continuation",
        "graph_name": "simple_tool",
        "tool_name": "http_get",
        "tool_call_id": "call-77",
        "summary": "tool rejected malformed url argument",
        "raw_request": {"headers": {"Authorization": "Bearer LEAK"}},
        "raw_response": {"set-cookie": "auth=LEAK"},
        "auth_token": "Bearer LEAK",
        "api_key": "sk-LEAK",
        "jwt": "eyJ.LEAK",
    }
    sanitized = sanitize_previous_failure(raw)
    assert sanitized == {
        "error_code": "tool_argument_invalid",
        "failure_stage": "graph_continuation",
        "graph_name": "simple_tool",
        "tool_name": "http_get",
        "tool_call_id": "call-77",
        "summary": "tool rejected malformed url argument",
    }


def test_sanitize_previous_failure_returns_none_when_no_whitelisted_fields() -> None:
    assert sanitize_previous_failure(None) is None
    assert sanitize_previous_failure({}) is None
    assert sanitize_previous_failure({"raw_response": "leak"}) is None


# ---------------------------------------------------------------------------
# Phase 1.2: claim_checkpoint_retry CAS semantics
# ---------------------------------------------------------------------------


def _seed_failed_workflow(
    service: TurnWorkflowService,
    *,
    task_id: int,
    turn_id: str,
    checkpoint_id: str,
    retry_attempt_count: int = 0,
    retry_max_attempts: int = 2,
    retryable: bool = True,
):
    started = service.start_turn(
        task_id=task_id,
        conversation_id=f"conv-{task_id}",
        turn_id=turn_id,
        turn_sequence=1,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        reserved_message_id=task_id * 11,
        metadata={
            "retryable": retryable,
            "retry_mode": "checkpoint",
            "retry_attempt_count": retry_attempt_count,
            "retry_max_attempts": retry_max_attempts,
            "last_failure": {
                "error_code": "tool_argument_invalid",
                "failure_stage": "graph_continuation",
                "graph_name": "simple_tool",
                "tool_name": "http_get",
                "tool_call_id": "call-77",
                "summary": "tool rejected malformed url argument",
            },
        },
    )
    failed = service.mark_failed(
        workflow_id=started.id,
        metadata={
            "retryable": retryable,
            "retry_mode": "checkpoint",
            "retry_attempt_count": retry_attempt_count,
            "retry_max_attempts": retry_max_attempts,
        },
    )
    assert failed is not None
    assert failed.state == TurnWorkflowState.FAILED.value
    return failed


def test_claim_checkpoint_retry_atomically_transitions_failed_to_retrying() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        failed = _seed_failed_workflow(
            service,
            task_id=1,
            turn_id="task-1-turn-1",
            checkpoint_id="ckpt-stable-1",
            retry_attempt_count=0,
            retry_max_attempts=2,
        )

        result = service.claim_checkpoint_retry(
            task_id=1,
            turn_id="task-1-turn-1",
            graph_name="simple_tool",
        )
        assert isinstance(result, CheckpointRetryClaimResult)
        assert result.status == "claimed"
        assert result.workflow is not None
        assert result.workflow.state == TurnWorkflowState.RETRYING.value
        assert result.workflow.workflow_metadata["retry_attempt_count"] == 1
        assert result.workflow.workflow_metadata["retry_max_attempts"] == 2
        assert result.identity is not None
        assert result.identity["retry_attempt"] == 1
        assert result.identity["retry_max_attempts"] == 2
        assert result.identity["checkpoint_id"] == "ckpt-stable-1"
        assert result.identity["state"] == "retrying"
        assert result.identity["already_in_flight"] is False
        assert result.identity["workflow_id"] == failed.id
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_is_idempotent_for_active_retry() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        _seed_failed_workflow(
            service,
            task_id=2,
            turn_id="task-2-turn-1",
            checkpoint_id="ckpt-stable-2",
        )
        first = service.claim_checkpoint_retry(
            task_id=2,
            turn_id="task-2-turn-1",
            graph_name="simple_tool",
        )
        assert first.status == "claimed"
        assert first.identity is not None
        assert first.identity["retry_attempt"] == 1

        second = service.claim_checkpoint_retry(
            task_id=2,
            turn_id="task-2-turn-1",
            graph_name="simple_tool",
        )
        # Duplicate claim must observe RETRYING and NOT bump the attempt count.
        assert second.status == "already_retrying"
        assert second.identity is not None
        assert second.identity["already_in_flight"] is True
        assert second.identity["retry_attempt"] == 1
        assert second.workflow is not None
        assert second.workflow.workflow_metadata["retry_attempt_count"] == 1, (
            "duplicate claim must not increment retry_attempt_count"
        )
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_rejects_missing_checkpoint_id() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        # Seed a FAILED retryable workflow with no checkpoint_id.
        started = service.start_turn(
            task_id=3,
            conversation_id="conv-3",
            turn_id="task-3-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=33,
            metadata={
                "retryable": True,
                "retry_mode": "checkpoint",
                "retry_attempt_count": 0,
                "retry_max_attempts": 2,
            },
        )
        service.mark_failed(
            workflow_id=started.id,
            metadata={
                "retryable": True,
                "retry_mode": "checkpoint",
                "retry_attempt_count": 0,
                "retry_max_attempts": 2,
            },
        )

        result = service.claim_checkpoint_retry(
            task_id=3,
            turn_id="task-3-turn-1",
            graph_name="simple_tool",
        )
        assert result.status == "not_retryable"
        # Row stays FAILED — claim must not mutate state.
        refreshed = service.get_workflow(started.id)
        assert refreshed is not None
        assert refreshed.state == TurnWorkflowState.FAILED.value
        assert refreshed.workflow_metadata["retry_attempt_count"] == 0
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_refuses_when_budget_exhausted() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        _seed_failed_workflow(
            service,
            task_id=4,
            turn_id="task-4-turn-1",
            checkpoint_id="ckpt-stable-4",
            retry_attempt_count=2,
            retry_max_attempts=2,
        )
        result = service.claim_checkpoint_retry(
            task_id=4,
            turn_id="task-4-turn-1",
            graph_name="simple_tool",
        )
        assert result.status == "retry_exhausted"
        assert result.workflow is not None
        # Row stays FAILED with attempt count untouched.
        assert result.workflow.state == TurnWorkflowState.FAILED.value
        assert result.workflow.workflow_metadata["retry_attempt_count"] == 2
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_returns_not_retryable_when_metadata_flag_missing() -> (
    None
):
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        _seed_failed_workflow(
            service,
            task_id=5,
            turn_id="task-5-turn-1",
            checkpoint_id="ckpt-5",
            retryable=False,
        )
        result = service.claim_checkpoint_retry(
            task_id=5,
            turn_id="task-5-turn-1",
            graph_name="simple_tool",
        )
        assert result.status == "not_retryable"
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_returns_missing_when_no_workflow_exists() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        result = service.claim_checkpoint_retry(
            task_id=99,
            turn_id="task-99-turn-1",
            graph_name="simple_tool",
        )
        assert result.status == "missing"
    finally:
        db.close()
        engine.dispose()


def test_claim_checkpoint_retry_returns_invalid_state_for_non_failed_row() -> None:
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        # Workflow is RUNNING — claim should refuse.
        service.start_turn(
            task_id=6,
            conversation_id="conv-6",
            turn_id="task-6-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            checkpoint_id="ckpt-6",
            reserved_message_id=66,
            metadata={
                "retryable": True,
                "retry_mode": "checkpoint",
                "retry_attempt_count": 0,
                "retry_max_attempts": 2,
            },
        )
        result = service.claim_checkpoint_retry(
            task_id=6,
            turn_id="task-6-turn-1",
            graph_name="simple_tool",
        )
        assert result.status == "invalid_state"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Phase 4.3: active_retry block persisted on claim and cleared on transitions
# ---------------------------------------------------------------------------


def test_claim_checkpoint_retry_persists_active_retry_block_with_canonical_fields() -> (
    None
):
    """The CAS claim must stamp a self-describing active_retry block on the
    workflow row so transcript bootstrap can derive in-flight retry state from
    one workflow read without scanning stream events.
    """
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        _seed_failed_workflow(
            service,
            task_id=10,
            turn_id="task-10-turn-1",
            checkpoint_id="ckpt-10",
            retry_attempt_count=0,
            retry_max_attempts=3,
        )

        result = service.claim_checkpoint_retry(
            task_id=10,
            turn_id="task-10-turn-1",
            graph_name="simple_tool",
        )

        assert result.status == "claimed"
        assert result.workflow is not None
        metadata = result.workflow.workflow_metadata
        assert isinstance(metadata, dict)
        active_retry = metadata.get("active_retry")
        assert isinstance(active_retry, dict)
        assert active_retry["attempt"] == 1
        assert active_retry["max_attempts"] == 3
        assert active_retry["state"] == "retrying"
        assert active_retry["checkpoint_id"] == "ckpt-10"
        assert active_retry["graph_name"] == "simple_tool"
        # last_failure was sanitized into the active_retry block as
        # previous_error_code / previous_failure_stage.
        assert active_retry.get("previous_error_code") == "tool_argument_invalid"
        assert active_retry.get("previous_failure_stage") == "graph_continuation"
    finally:
        db.close()
        engine.dispose()


def test_mark_completed_clears_active_retry_when_completion_metadata_carries_none() -> (
    None
):
    """Successful retry completion must clear the active_retry block so the
    transcript bootstrap never reopens the retry CTA after success.
    """
    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        _seed_failed_workflow(
            service,
            task_id=11,
            turn_id="task-11-turn-1",
            checkpoint_id="ckpt-11",
            retry_attempt_count=0,
            retry_max_attempts=2,
        )
        claim = service.claim_checkpoint_retry(
            task_id=11,
            turn_id="task-11-turn-1",
            graph_name="simple_tool",
        )
        assert claim.status == "claimed"
        assert claim.workflow is not None
        assert isinstance(claim.workflow.workflow_metadata.get("active_retry"), dict)

        # Phase 4.3: orchestrator passes ``active_retry: None`` when retry
        # completes successfully (see TurnExecutionService._finalize_*).
        completed = service.mark_completed(
            workflow_id=claim.workflow.id,
            metadata={
                "completion_source": "checkpoint_retry",
                "active_retry": None,
                "retry_state": "completed",
            },
        )
        assert completed is not None
        assert completed.state == TurnWorkflowState.COMPLETED.value
        merged = completed.workflow_metadata
        assert isinstance(merged, dict)
        assert merged.get("active_retry") is None
        assert merged.get("retry_state") == "completed"
        # retry_attempt_count is preserved so the post-retry transcript still
        # carries the retry telemetry needed for CTA gating decisions.
        assert merged.get("retry_attempt_count") == 1
        assert merged.get("retry_max_attempts") == 2
    finally:
        db.close()
        engine.dispose()


def test_terminal_metadata_preserves_newest_measured_context_snapshot() -> None:
    """Bootstrap, malformed, and equal-revision writes cannot regress occupancy."""

    engine, db = _build_session()
    try:
        service = TurnWorkflowService(db)
        measured = {
            "conversation_id": "conv-context",
            "max_tokens": 32_768,
            "used_tokens": 8_723,
            "remaining_tokens": 24_045,
            "ratio": 8_723 / 32_768,
            "ceiling_reached": False,
            "recommended_next_action": "none",
            "compression_candidate": False,
            "turn_sequence": 1,
            "revision": 1,
            "snapshot_kind": "measured",
        }
        started = service.start_turn(
            task_id=12,
            conversation_id="conv-context",
            turn_id="context-turn-1",
            turn_sequence=1,
            metadata={"context_window": measured, "source": "test"},
        )

        failed = service.mark_failed(
            workflow_id=started.id,
            replace_metadata=True,
            metadata={
                "context_window": {
                    **measured,
                    "used_tokens": 37,
                    "remaining_tokens": 32_731,
                    "ratio": 37 / 32_768,
                    "turn_sequence": None,
                    "revision": -1,
                    "snapshot_kind": "bootstrap_estimate",
                },
                "error": "run_cancelled",
            },
        )

        assert failed is not None
        assert failed.workflow_metadata["context_window"] == measured
        assert failed.workflow_metadata["error"] == "run_cancelled"
        assert "source" not in failed.workflow_metadata
    finally:
        db.close()
        engine.dispose()
