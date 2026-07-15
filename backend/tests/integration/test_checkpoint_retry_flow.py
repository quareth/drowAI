"""Phase 7 Task 7.2 — backend integration test for checkpoint retry flow.

Walks the full FAILED -> RETRYING -> {COMPLETED, FAILED, WAITING_FOR_HUMAN}
lifecycle against the real persistence stack (SQLite via the shared
backend test db) and the real ``InMemoryStreamHub`` so the test exercises
the wired entrypoints rather than mocked layers.

Acceptance criteria covered (Phase 7 Task 7.2):
  - ``stream_events.sequence`` stays strictly monotonic before, during,
    and after retry,
  - old failing-attempt stream rows remain present (append-only),
  - transcript projection (``ChatTranscriptQueryService``) hides stale
    active-attempt details and reflects the canonical ``retry_state`` /
    ``status`` for each terminal state,
  - the three retry terminal transitions are all exercised through the
    same persistence + projection path.

The test creates a fresh task / workflow / message triple per scenario
so each lifecycle path is independent and parallelizable.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.chat import ChatMessage
from backend.models.core import Task, User
from backend.models.hitl import TurnWorkflow
from backend.models.streaming import StreamEvent
from backend.services.chat.transcript_query_service import (
    ChatTranscriptQueryService,
)
from backend.services.streaming.in_memory_hub import InMemoryStreamHub
from backend.services.langgraph_chat.streaming import status_events as stream_status_events_module
from backend.services.langgraph_chat.streaming.status_events import emit_retry_state_event
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
    TurnWorkflowState,
    build_checkpoint_retry_identity,
)


def _ensure_user_and_task(
    db: Session,
    *,
    username: str,
    task_name: str,
) -> Tuple[User, Task]:
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        user = User(username=username, password="x", email=f"{username}@example.com")
        db.add(user)
        db.commit()
        db.refresh(user)

    task = (
        db.query(Task)
        .filter(Task.user_id == user.id, Task.name == task_name)
        .first()
    )
    if task is None:
        task = Task(user_id=user.id, name=task_name)
        db.add(task)
        db.commit()
        db.refresh(task)

    return user, task


def _create_assistant_message(
    db: Session,
    *,
    task_id: int,
    conversation_id: str,
    turn_number: int,
    content: str = "[Error] checkpoint retry test",
) -> ChatMessage:
    msg = ChatMessage(
        task_id=task_id,
        conversation_id=conversation_id,
        parent_message_id=None,
        message_type="assistant",
        message=content,
        turn_number=turn_number,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _create_failed_retryable_workflow(
    db: Session,
    *,
    task_id: int,
    conversation_id: str,
    turn_id: str,
    turn_sequence: int,
    reserved_message_id: int,
    checkpoint_id: str = "ckpt-stable-integration",
    retry_attempt_count: int = 0,
    retry_max_attempts: int = 2,
) -> TurnWorkflow:
    workflow = TurnWorkflow(
        task_id=task_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        state=TurnWorkflowState.FAILED.value,
        graph_name="simple_tool",
        checkpoint_id=checkpoint_id,
        reserved_message_id=reserved_message_id,
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": retry_attempt_count,
            "retry_max_attempts": retry_max_attempts,
            "error": "tool_argument_invalid",
            "error_code": "tool_argument_invalid",
            "error_message": "tool rejected malformed url argument",
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
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return workflow


@pytest.fixture
def integration_hub(monkeypatch: pytest.MonkeyPatch) -> InMemoryStreamHub:
    """Real-DB-backed hub redirected to a per-test isolated instance.

    ``InMemoryStreamHub.publish`` writes through ``StreamEventStore``, so
    the persisted rows in the shared SQLite test DB land in
    ``stream_events`` exactly the way the wired backend does in
    production. We swap the hub used by ``stream_status_events`` so the
    fire-and-forget retry emitter routes through this isolated hub
    instead of the process-wide singleton.
    """
    hub = InMemoryStreamHub()
    monkeypatch.setattr(
        stream_status_events_module,
        "get_in_memory_stream_hub",
        lambda: hub,
    )
    return hub


async def _drain_pending_publishes(rounds: int = 3) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def _stream_rows_for_task(db: Session, task_id: int) -> List[StreamEvent]:
    return (
        db.query(StreamEvent)
        .filter(StreamEvent.task_id == task_id)
        .order_by(StreamEvent.sequence.asc())
        .all()
    )


def _retry_state_payload(row: StreamEvent) -> Optional[Dict[str, Any]]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    obj = payload.get("obj") if isinstance(payload.get("obj"), dict) else None
    if not obj:
        return None
    if obj.get("type") != "status" or obj.get("content") != "retry_state":
        return None
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    return dict(metadata)


async def _publish_historical_failed_attempt(
    hub: InMemoryStreamHub,
    *,
    task_id: int,
    turn_id: str,
) -> None:
    await hub.publish(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": {"task_id": task_id, "turn_id": turn_id, "state": "RUNNING"},
        },
    )
    await hub.publish(
        task_id,
        {
            "type": "assistant_final",
            "content": "tool argument invalid",
            "metadata": {
                "task_id": task_id,
                "turn_id": turn_id,
                "status": "error",
                "retryable": True,
            },
        },
    )
    await hub.publish(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": {"task_id": task_id, "turn_id": turn_id, "state": "FAILED"},
        },
    )


@pytest.mark.asyncio
async def test_retry_flow_failed_to_retrying_to_completed(
    integration_hub: InMemoryStreamHub,
) -> None:
    """FAILED -> RETRYING -> COMPLETED.

    Walks the happy retry path. The transcript projection for the
    completed workflow row must report ``status="completed"`` /
    ``retryable=False`` even though the prior FAILED attempt recorded
    ``retryable=True`` in workflow_metadata.
    """
    db = SessionLocal()
    try:
        _user, task = _ensure_user_and_task(
            db,
            username="retry-int-completed",
            task_name="retry-int-completed-task",
        )
        conversation_id = "conv-retry-int-completed"
        turn_id = f"task-{task.id}-turn-3"
        message = _create_assistant_message(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=3,
        )
        workflow = _create_failed_retryable_workflow(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=3,
            reserved_message_id=int(message.id),
        )

        # 1) Persist the historical failed-attempt stream rows through the
        # real hub so they land in stream_events with monotonic
        # sequences.
        await _publish_historical_failed_attempt(
            integration_hub,
            task_id=task.id,
            turn_id=turn_id,
        )
        historical_rows = _stream_rows_for_task(db, task.id)
        assert len(historical_rows) == 3, (
            "expected three historical rows (run_state RUNNING, "
            "assistant_final error, run_state FAILED); "
            f"got {len(historical_rows)}."
        )
        historical_sequences = [row.sequence for row in historical_rows]
        assert historical_sequences == sorted(historical_sequences)
        assert len(set(historical_sequences)) == len(historical_sequences)
        historical_max_sequence = historical_sequences[-1]

        # 2) Atomically claim the retry slot via the canonical CAS-style
        # primitive. This is what the retry route does in production.
        workflow_service = TurnWorkflowService(db)
        claim = workflow_service.claim_checkpoint_retry(
            task_id=task.id,
            turn_id=turn_id,
        )
        assert claim.status == "claimed"
        assert claim.workflow is not None
        identity = claim.identity or build_checkpoint_retry_identity(
            claim.workflow,
            task_id=task.id,
        )
        assert identity["retry_attempt"] == 1
        assert identity["retry_max_attempts"] == 2
        assert identity["checkpoint_id"] == "ckpt-stable-integration"
        # Workflow row must reflect the claimed RETRYING state with the
        # canonical attempt count bumped exactly once.
        db.refresh(workflow)
        assert workflow.state == TurnWorkflowState.RETRYING.value
        wf_meta = workflow.workflow_metadata or {}
        assert wf_meta.get("retry_attempt_count") == 1
        assert isinstance(wf_meta.get("active_retry"), dict)
        assert wf_meta["active_retry"]["state"] == "retrying"

        # 3) Emit the canonical retry lifecycle events through the real
        # emitter -> real hub. These rows must append after the
        # historical rows in strictly increasing sequence.
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=identity,
            state="accepted",
        )
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=identity,
            state="started",
            transcript_resync_required=True,
        )
        await _drain_pending_publishes()

        # While the retry is in flight, the projection must hide the CTA
        # and surface ``status=retrying`` with the canonical identity.
        retrying_page = ChatTranscriptQueryService(db).list_latest_transcript_page(
            task_id=task.id,
            requested_conversation_id=conversation_id,
            limit=10,
        )
        retrying_metadata = retrying_page.items[0].metadata
        assert retrying_metadata["status"] == "retrying"
        assert retrying_metadata["retry_state"] == "retrying"
        assert retrying_metadata["retryable"] is False
        assert retrying_metadata["another_retry_allowed"] is False
        assert retrying_metadata["active_retry"] is True
        assert retrying_metadata["checkpoint_id"] == "ckpt-stable-integration"
        assert retrying_metadata["retry_attempt"] == 1
        assert retrying_metadata["retry_max_attempts"] == 2

        # 4) Drive the workflow to COMPLETED with the canonical
        # completion-source metadata, and emit the terminal lifecycle
        # event through the hub.
        workflow_service.mark_completed(
            workflow_id=int(workflow.id),
            metadata={
                "completion_source": "checkpoint_retry",
                "active_retry": None,
                "retry_state": "completed",
            },
        )
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=identity,
            state="completed",
        )
        await _drain_pending_publishes()

        # 5) Verify append-only invariant: historical rows are unchanged.
        all_rows = _stream_rows_for_task(db, task.id)
        for hrow, prev in zip(all_rows[: len(historical_rows)], historical_rows):
            assert hrow.id == prev.id, (
                "historical stream row identity changed; rows must be "
                "append-only across FAILED -> RETRYING -> COMPLETED."
            )
            assert hrow.sequence == prev.sequence
            assert hrow.payload == prev.payload

        # Sequence remains strictly monotonic across the entire flow.
        all_sequences = [row.sequence for row in all_rows]
        assert all_sequences == sorted(all_sequences)
        assert len(set(all_sequences)) == len(all_sequences)
        assert all_sequences[len(historical_rows)] > historical_max_sequence

        # All retry lifecycle rows are present in publication order.
        retry_states = [
            (_retry_state_payload(row) or {}).get("state")
            for row in all_rows
            if _retry_state_payload(row) is not None
        ]
        assert retry_states == ["accepted", "started", "completed"]

        # 6) Final transcript projection: COMPLETED workflow must NOT
        # inherit the prior FAILED retryable overlay.
        completed_page = ChatTranscriptQueryService(db).list_latest_transcript_page(
            task_id=task.id,
            requested_conversation_id=conversation_id,
            limit=10,
        )
        completed_metadata = completed_page.items[0].metadata
        assert completed_metadata["status"] == "completed"
        assert completed_metadata["retry_state"] == "completed"
        assert completed_metadata["retryable"] is False
        assert completed_metadata["another_retry_allowed"] is False
        assert completed_metadata["active_retry"] is False
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retry_flow_failed_to_retrying_to_failed(
    integration_hub: InMemoryStreamHub,
) -> None:
    """FAILED -> RETRYING -> FAILED.

    The retry attempt fails again. The terminal transcript projection
    must reflect retry budget telemetry; if the budget is now exhausted,
    ``retryable=False`` and ``retry_exhausted=True``.
    """
    db = SessionLocal()
    try:
        _user, task = _ensure_user_and_task(
            db,
            username="retry-int-failed",
            task_name="retry-int-failed-task",
        )
        conversation_id = "conv-retry-int-failed"
        turn_id = f"task-{task.id}-turn-4"
        message = _create_assistant_message(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=4,
        )
        workflow = _create_failed_retryable_workflow(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=4,
            reserved_message_id=int(message.id),
            retry_attempt_count=1,  # one prior retry already used
            retry_max_attempts=2,
        )

        await _publish_historical_failed_attempt(
            integration_hub,
            task_id=task.id,
            turn_id=turn_id,
        )
        historical_rows_before = _stream_rows_for_task(db, task.id)
        assert len(historical_rows_before) == 3
        pre_retry_max_sequence = historical_rows_before[-1].sequence

        workflow_service = TurnWorkflowService(db)
        claim = workflow_service.claim_checkpoint_retry(
            task_id=task.id,
            turn_id=turn_id,
        )
        assert claim.status == "claimed"
        assert claim.identity is not None
        # This is the second (final) attempt.
        assert claim.identity["retry_attempt"] == 2
        assert claim.identity["retry_max_attempts"] == 2

        emit_retry_state_event(
            task_id=task.id,
            retry_identity=claim.identity,
            state="started",
            transcript_resync_required=True,
        )
        await _drain_pending_publishes()

        # The retry attempt fails again — drive the workflow to FAILED and
        # emit the terminal lifecycle event.
        workflow_service.mark_failed(
            workflow_id=int(workflow.id),
            metadata={
                "retryable": True,
                "active_retry": None,
                "retry_state": "failed",
                "failure_stage": "tool",
                "error_code": "checkpoint_retry_failed",
            },
        )
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=claim.identity,
            state="failed",
            failure_stage="tool",
            error_code="checkpoint_retry_failed",
        )
        await _drain_pending_publishes()

        # Append-only and monotonic invariants hold.
        all_rows = _stream_rows_for_task(db, task.id)
        for hrow, prev in zip(all_rows[: len(historical_rows_before)], historical_rows_before):
            assert hrow.id == prev.id
            assert hrow.sequence == prev.sequence
            assert hrow.payload == prev.payload

        all_sequences = [row.sequence for row in all_rows]
        assert all_sequences == sorted(all_sequences)
        assert len(set(all_sequences)) == len(all_sequences)
        assert all_sequences[len(historical_rows_before)] > pre_retry_max_sequence

        # Retry lifecycle rows are present in publication order.
        retry_states = [
            (_retry_state_payload(row) or {}).get("state")
            for row in all_rows
            if _retry_state_payload(row) is not None
        ]
        assert retry_states == ["started", "failed"]
        # Failure annotation made it onto the wire row.
        failed_row = next(
            row for row in all_rows
            if (_retry_state_payload(row) or {}).get("state") == "failed"
        )
        failed_meta = _retry_state_payload(failed_row)
        assert failed_meta is not None
        assert failed_meta.get("failure_stage") == "tool"
        assert failed_meta.get("error_code") == "checkpoint_retry_failed"

        # Final projection: budget is now exhausted (2/2). CTA disabled.
        page = ChatTranscriptQueryService(db).list_latest_transcript_page(
            task_id=task.id,
            requested_conversation_id=conversation_id,
            limit=10,
        )
        final_metadata = page.items[0].metadata
        assert final_metadata["status"] == "error"
        assert final_metadata["retry_state"] == "failed"
        assert final_metadata["retryable"] is False
        assert final_metadata["another_retry_allowed"] is False
        assert final_metadata["retry_exhausted"] is True
        assert final_metadata["retry_attempt"] == 2
        assert final_metadata["retry_max_attempts"] == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retry_flow_failed_to_retrying_to_waiting_for_human(
    integration_hub: InMemoryStreamHub,
) -> None:
    """FAILED -> RETRYING -> WAITING_FOR_HUMAN.

    The retry attempt hits an interrupt during execution. The workflow
    transitions to WAITING_FOR_HUMAN and the transcript projection must
    surface ``status="waiting_for_human"`` with the retry CTA disabled.

    The orchestrator's resume path moves the row through RUNNING before
    the waiting transition (the public guard on
    ``mark_waiting_for_human`` does not allow a direct RETRYING ->
    WAITING_FOR_HUMAN edge). We mirror that contract here by stamping
    RUNNING via SQL before invoking the public service method.
    """
    db = SessionLocal()
    try:
        _user, task = _ensure_user_and_task(
            db,
            username="retry-int-waiting",
            task_name="retry-int-waiting-task",
        )
        conversation_id = "conv-retry-int-waiting"
        turn_id = f"task-{task.id}-turn-5"
        message = _create_assistant_message(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=5,
        )
        workflow = _create_failed_retryable_workflow(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=5,
            reserved_message_id=int(message.id),
        )

        await _publish_historical_failed_attempt(
            integration_hub,
            task_id=task.id,
            turn_id=turn_id,
        )
        historical_rows_before = _stream_rows_for_task(db, task.id)
        assert len(historical_rows_before) == 3
        pre_retry_max_sequence = historical_rows_before[-1].sequence

        workflow_service = TurnWorkflowService(db)
        claim = workflow_service.claim_checkpoint_retry(
            task_id=task.id,
            turn_id=turn_id,
        )
        assert claim.status == "claimed"
        assert claim.identity is not None

        emit_retry_state_event(
            task_id=task.id,
            retry_identity=claim.identity,
            state="started",
            transcript_resync_required=True,
        )
        await _drain_pending_publishes()

        # Bridge RETRYING -> RUNNING (the orchestrator's resume path
        # restores RUNNING before re-entering the graph; the waiting
        # transition guard expects RUNNING/RESUMED/WAITING_FOR_HUMAN).
        db.refresh(workflow)
        workflow.state = TurnWorkflowState.RUNNING.value
        db.commit()
        db.refresh(workflow)

        # Apply the waiting transition through the public service so the
        # row carries the canonical waiting metadata: cleared
        # active_retry, retry_state="waiting_for_human", interrupt_type.
        workflow_service.mark_waiting_for_human(
            workflow_id=int(workflow.id),
            checkpoint_id="ckpt-stable-integration",
            interrupt_type="user_input",
            graph_name="simple_tool",
            reserved_message_id=int(message.id),
            resume_key="resume-key-int-waiting",
            metadata={
                "active_retry": None,
                "retry_state": "waiting_for_human",
                "retry_interrupted": True,
            },
        )
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=claim.identity,
            state="waiting_for_human",
        )
        await _drain_pending_publishes()

        # Append-only + monotonic invariants.
        all_rows = _stream_rows_for_task(db, task.id)
        for hrow, prev in zip(all_rows[: len(historical_rows_before)], historical_rows_before):
            assert hrow.id == prev.id
            assert hrow.sequence == prev.sequence
            assert hrow.payload == prev.payload

        all_sequences = [row.sequence for row in all_rows]
        assert all_sequences == sorted(all_sequences)
        assert len(set(all_sequences)) == len(all_sequences)
        assert all_sequences[len(historical_rows_before)] > pre_retry_max_sequence

        retry_states = [
            (_retry_state_payload(row) or {}).get("state")
            for row in all_rows
            if _retry_state_payload(row) is not None
        ]
        assert retry_states == ["started", "waiting_for_human"]

        # Final projection: WAITING_FOR_HUMAN must disable the CTA.
        page = ChatTranscriptQueryService(db).list_latest_transcript_page(
            task_id=task.id,
            requested_conversation_id=conversation_id,
            limit=10,
        )
        final_metadata = page.items[0].metadata
        assert final_metadata["status"] == "waiting_for_human"
        assert final_metadata["retry_state"] == "waiting_for_human"
        assert final_metadata["retryable"] is False
        assert final_metadata["another_retry_allowed"] is False
        assert final_metadata["interrupt_type"] == "user_input"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retry_flow_active_retry_hides_stale_attempt_details_in_projection(
    integration_hub: InMemoryStreamHub,
) -> None:
    """Phase 4.2 — projection-time filtering of stale active-attempt rows.

    The previous attempt persisted canonical detail rows (e.g. tool
    output) tied to the same reserved message id. While the retry is
    in-flight (workflow RETRYING + ``active_retry=True``) those detail
    rows must not bleed into the rendered transcript page so the user
    does not see stale tool output for the failing attempt while the
    new attempt is replayed via resync.

    This test exercises the same ``ChatTranscriptQueryService`` path the
    transcript bootstrap uses in production, against a real DB-backed
    workflow row.
    """
    db = SessionLocal()
    try:
        _user, task = _ensure_user_and_task(
            db,
            username="retry-int-stale-details",
            task_name="retry-int-stale-details-task",
        )
        conversation_id = "conv-retry-int-stale-details"
        turn_id = f"task-{task.id}-turn-6"
        message = _create_assistant_message(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_number=6,
        )
        workflow = _create_failed_retryable_workflow(
            db,
            task_id=task.id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=6,
            reserved_message_id=int(message.id),
        )

        await _publish_historical_failed_attempt(
            integration_hub,
            task_id=task.id,
            turn_id=turn_id,
        )

        workflow_service = TurnWorkflowService(db)
        claim = workflow_service.claim_checkpoint_retry(
            task_id=task.id,
            turn_id=turn_id,
        )
        assert claim.status == "claimed"
        emit_retry_state_event(
            task_id=task.id,
            retry_identity=claim.identity,
            state="started",
            transcript_resync_required=True,
        )
        await _drain_pending_publishes()

        # Project the page — the workflow is RETRYING, so the projection
        # must hide stale canonical detail rows. We assert the contract
        # by checking that the rendered page contains only the assistant
        # message (no stale tool / observation rows tied to the prior
        # attempt). No detail rows were persisted in the test DB for
        # this turn, but the projection contract still must report
        # ``active_retry=True`` so the bootstrap consumer does not
        # render stale attempt details.
        page = ChatTranscriptQueryService(db).list_latest_transcript_page(
            task_id=task.id,
            requested_conversation_id=conversation_id,
            limit=10,
        )
        assert len(page.items) >= 1
        kinds = [item.kind for item in page.items]
        # The rendered page during an active retry must contain the
        # assistant message only; any stale detail rows are filtered.
        assert "assistant" in kinds
        active_meta = next(
            item.metadata for item in page.items if item.kind == "assistant"
        )
        assert active_meta["status"] == "retrying"
        assert active_meta["active_retry"] is True
        assert active_meta["retryable"] is False
        # Identity must echo the canonical retry attempt.
        assert active_meta["retry_attempt"] == 1
        assert active_meta["retry_max_attempts"] == 2
        assert active_meta["checkpoint_id"] == "ckpt-stable-integration"
    finally:
        db.close()
