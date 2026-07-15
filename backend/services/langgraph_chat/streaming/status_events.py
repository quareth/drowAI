"""Emit task-scoped runtime status events into the shared stream hub.

This module centralizes status event construction and publication for lifecycle
state changes so clients can consume run/interrupt updates without periodic
polling. Most lifecycle updates remain fire-and-forget; checkpoint rewind and
context-compaction callers can use awaited helpers when stream ordering is part
of the user-visible contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

from backend.core.time_utils import format_iso, utc_now
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

logger = logging.getLogger(__name__)

_ALLOWED_INTERRUPT_STATES = {
    "PENDING",
    "RESUMING",
    "RESUMED",
    "COMPLETED",
    "FAILED",
    "EXPIRED",
}

# Canonical retry lifecycle states that can be published through retry
# lifecycle events. Anything outside this set is dropped at the emitter so a
# stray caller cannot smuggle bespoke states into the stream.
_ALLOWED_RETRY_LIFECYCLE_STATES = {
    "accepted",
    "started",
    "retrying",
    "waiting_for_human",
    "completed",
    "declined",
    "failed",
    "cancelled",
}

# Canonical retry identity keys — the single source of truth lives in
# ``turn_workflow_service.build_checkpoint_retry_identity``. The emitter
# reads only these whitelisted keys off the supplied identity mapping so
# nothing extra (including secrets) can leak into stream metadata via the
# identity carrier.
_ALLOWED_RETRY_IDENTITY_KEYS: tuple[str, ...] = (
    "task_id",
    "turn_id",
    "workflow_id",
    "graph_name",
    "checkpoint_id",
    "retry_mode",
    "retry_attempt",
    "retry_max_attempts",
    "state",
    "already_in_flight",
)

_ALLOWED_CHECKPOINT_REWIND_OPERATION_KINDS = {
    "retry",
    "stop",
    "checkpoint_rewind",
}

_ALLOWED_CONTEXT_WINDOW_LIFECYCLE_STATES = {
    "compacting",
    "completed",
    "failed",
    "cancelled",
}


def _publish_task_status(task_id: int, event: dict[str, Any]) -> None:
    """Publish a status event to the in-memory hub when an event loop is active."""
    if task_id <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        hub = get_in_memory_stream_hub()
        loop.create_task(hub.publish(task_id, event))
    except Exception:
        logger.debug(
            "Failed to publish task status event task_id=%s", task_id, exc_info=True
        )


async def _publish_task_status_awaited(task_id: int, event: dict[str, Any]) -> bool:
    """Publish a status event and wait until the hub assigns its stream sequence."""
    if task_id <= 0:
        return False
    try:
        hub = get_in_memory_stream_hub()
        await hub.publish(task_id, event)
        return True
    except Exception:
        logger.debug(
            "Failed to publish ordered task status event task_id=%s",
            task_id,
            exc_info=True,
        )
        return False


def _iso_utcnow() -> str:
    return format_iso(utc_now())


def _normalize_interrupt_state(state: str) -> Optional[str]:
    if not isinstance(state, str):
        return None
    normalized = state.strip().upper()
    if normalized not in _ALLOWED_INTERRUPT_STATES:
        return None
    return normalized


def emit_run_state_event(
    *,
    task_id: int,
    state: str,
    turn_id: Optional[str],
    cancel_requested: bool,
    cancel_reason: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> None:
    """Emit additive `status/run_state` event."""
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "state": state,
        "turn_id": turn_id,
        "cancel_requested": bool(cancel_requested),
    }
    if cancel_reason:
        metadata["cancel_reason"] = cancel_reason
    if conversation_id:
        metadata["conversation_id"] = conversation_id
        metadata["conversationId"] = conversation_id
    _publish_task_status(
        task_id,
        {
            "type": "status",
            "content": "run_state",
            "metadata": metadata,
        },
    )


def emit_interrupt_state_event(
    *,
    task_id: int,
    interrupt_id: Optional[str],
    state: str,
    interrupt_type: Optional[str] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    turn_sequence: Optional[int] = None,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> None:
    """Emit additive `status/interrupt_state` event."""
    normalized_state = _normalize_interrupt_state(state)
    if normalized_state is None:
        logger.debug("Skipping interrupt_state event with invalid state=%r", state)
        return
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "interrupt_id": interrupt_id,
        "state": normalized_state,
        "has_pending": normalized_state == "PENDING",
        "timestamp": updated_at or created_at or _iso_utcnow(),
    }
    if interrupt_type:
        metadata["interrupt_type"] = interrupt_type
    if graph_name:
        metadata["graph_name"] = graph_name
    if checkpoint_id:
        metadata["checkpoint_id"] = checkpoint_id
    if thread_id:
        metadata["thread_id"] = thread_id
    if turn_id:
        metadata["turn_id"] = turn_id
    if isinstance(turn_sequence, int):
        metadata["turn_sequence"] = turn_sequence
    if created_at:
        metadata["created_at"] = created_at
    if updated_at:
        metadata["updated_at"] = updated_at
    _publish_task_status(
        task_id,
        {
            "type": "status",
            "content": "interrupt_state",
            "metadata": metadata,
        },
    )


def emit_context_window_event(
    *,
    task_id: int,
    conversation_id: str,
    max_tokens: int,
    used_tokens: int,
    remaining_tokens: int,
    ratio: float,
    ceiling_reached: bool,
    recommended_next_action: str = "none",
    compression_candidate: bool = False,
    compression_pass_count: Optional[int] = None,
    compression_tokens_before: Optional[int] = None,
    compression_tokens_after: Optional[int] = None,
    compression_degraded: Optional[bool] = None,
    turn_sequence: Optional[int] = None,
    revision: Optional[int] = None,
    snapshot_kind: Optional[str] = None,
) -> None:
    """Emit additive `status/context_window` event with non-blocking handoff hints."""
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "conversation_id": conversation_id,
        "conversationId": conversation_id,
        "max_tokens": int(max_tokens),
        "used_tokens": int(used_tokens),
        "remaining_tokens": int(remaining_tokens),
        "ratio": float(ratio),
        "ceiling_reached": bool(ceiling_reached),
        "recommended_next_action": recommended_next_action
        if recommended_next_action in {"none", "compress"}
        else "none",
        "compression_candidate": bool(compression_candidate),
    }
    if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
        metadata["turn_sequence"] = turn_sequence
    if isinstance(revision, int) and not isinstance(revision, bool):
        metadata["revision"] = revision
    if snapshot_kind in {"measured", "bootstrap_estimate"}:
        metadata["snapshot_kind"] = snapshot_kind
    if isinstance(compression_pass_count, int):
        metadata["compression_pass_count"] = compression_pass_count
    if isinstance(compression_tokens_before, int):
        metadata["compression_tokens_before"] = compression_tokens_before
    if isinstance(compression_tokens_after, int):
        metadata["compression_tokens_after"] = compression_tokens_after
    if isinstance(compression_degraded, bool):
        metadata["compression_degraded"] = compression_degraded
    _publish_task_status(
        task_id,
        {
            "type": "status",
            "content": "context_window",
            "metadata": metadata,
        },
    )


def build_context_window_lifecycle_event(
    *,
    task_id: int,
    conversation_id: str,
    state: str,
    turn_id: str,
    epoch_id: str,
) -> Optional[dict[str, Any]]:
    """Build one ordered context-compaction lifecycle status event."""
    normalized_state = state.strip().lower() if isinstance(state, str) else ""
    if normalized_state not in _ALLOWED_CONTEXT_WINDOW_LIFECYCLE_STATES:
        return None
    normalized_conversation_id = (
        conversation_id.strip() if isinstance(conversation_id, str) else ""
    )
    normalized_turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
    normalized_epoch_id = epoch_id.strip() if isinstance(epoch_id, str) else ""
    if not normalized_conversation_id or not normalized_turn_id or not normalized_epoch_id:
        return None
    return {
        "type": "status",
        "content": "context_window",
        "metadata": {
            "task_id": task_id,
            "conversation_id": normalized_conversation_id,
            "conversationId": normalized_conversation_id,
            "state": normalized_state,
            "turn_id": normalized_turn_id,
            "epoch_id": normalized_epoch_id,
            "timestamp": _iso_utcnow(),
        },
    }


async def publish_context_window_lifecycle_event(
    *,
    task_id: int,
    conversation_id: str,
    state: str,
    turn_id: str,
    epoch_id: str,
) -> bool:
    """Publish context-compaction lifecycle and await stream sequencing."""
    event = build_context_window_lifecycle_event(
        task_id=task_id,
        conversation_id=conversation_id,
        state=state,
        turn_id=turn_id,
        epoch_id=epoch_id,
    )
    if event is None:
        return False
    return await _publish_task_status_awaited(task_id, event)


def _normalize_retry_lifecycle_state(state: str) -> Optional[str]:
    if not isinstance(state, str):
        return None
    normalized = state.strip().lower()
    if normalized not in _ALLOWED_RETRY_LIFECYCLE_STATES:
        return None
    return normalized


def _normalize_checkpoint_rewind_operation_kind(operation_kind: str) -> Optional[str]:
    if not isinstance(operation_kind, str):
        return None
    normalized = operation_kind.strip().lower()
    if normalized not in _ALLOWED_CHECKPOINT_REWIND_OPERATION_KINDS:
        return None
    return normalized


def _build_checkpoint_operation_metadata(
    *,
    task_id: int,
    retry_identity: Optional[Mapping[str, Any]],
    turn_id: Optional[str],
    workflow_id: Optional[int],
    graph_name: Optional[str],
    checkpoint_id: Optional[str],
    retry_mode: Optional[str],
    retry_attempt: Optional[int],
    retry_max_attempts: Optional[int],
    already_in_flight: Optional[bool],
) -> dict[str, Any]:
    """Project checkpoint operation identity fields through a whitelist."""
    metadata: dict[str, Any] = {"task_id": task_id}

    if isinstance(retry_identity, Mapping):
        for key in _ALLOWED_RETRY_IDENTITY_KEYS:
            if key in retry_identity:
                value = retry_identity[key]
                if value is not None:
                    metadata[key] = value

    if turn_id is not None:
        metadata["turn_id"] = turn_id
    if workflow_id is not None:
        metadata["workflow_id"] = workflow_id
    if graph_name is not None:
        metadata["graph_name"] = graph_name
    if checkpoint_id is not None:
        metadata["checkpoint_id"] = checkpoint_id
    if retry_mode is not None:
        metadata["retry_mode"] = retry_mode
    if isinstance(retry_attempt, int):
        metadata["retry_attempt"] = retry_attempt
    if isinstance(retry_max_attempts, int):
        metadata["retry_max_attempts"] = retry_max_attempts
    if isinstance(already_in_flight, bool):
        metadata["already_in_flight"] = already_in_flight

    return metadata


def build_retry_state_event(
    *,
    task_id: int,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Build the canonical ``status/retry_state`` stream event."""
    normalized_state = _normalize_retry_lifecycle_state(state)
    if normalized_state is None:
        logger.debug("Skipping retry_state event with invalid state=%r", state)
        return None

    metadata = _build_checkpoint_operation_metadata(
        task_id=task_id,
        retry_identity=retry_identity,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name=graph_name,
        checkpoint_id=checkpoint_id,
        retry_mode=retry_mode,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        already_in_flight=already_in_flight,
    )

    metadata["state"] = normalized_state
    metadata["timestamp"] = _iso_utcnow()
    if transcript_resync_required:
        metadata["transcript_resync_required"] = True
    if isinstance(failure_stage, str) and failure_stage.strip():
        metadata["failure_stage"] = failure_stage.strip()
    if isinstance(error_code, str) and error_code.strip():
        metadata["error_code"] = error_code.strip()

    return {
        "type": "status",
        "content": "retry_state",
        "metadata": metadata,
    }


def emit_retry_state_event(
    *,
    task_id: int,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    """Emit additive ``status/retry_state`` event into the shared stream hub.

    The metadata shape is the canonical retry identity contract built by
    ``turn_workflow_service.build_checkpoint_retry_identity`` plus the
    lifecycle ``state`` and (optional) failure annotations. Callers may
    pass either the identity mapping (preferred — single source of truth
    from ``build_checkpoint_retry_identity``) or the explicit per-field
    kwargs; explicit kwargs override identity values when both are given.

    The event is fire-and-forget via ``InMemoryStreamHub.publish`` so the
    hub assigns the next monotonic ``sequence`` and ``StreamEventStore``
    appends a new row — no rewrites, no manual sequence assignment.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(
            publish_retry_state_event(
                task_id=task_id,
                state=state,
                retry_identity=retry_identity,
                turn_id=turn_id,
                workflow_id=workflow_id,
                graph_name=graph_name,
                checkpoint_id=checkpoint_id,
                retry_mode=retry_mode,
                retry_attempt=retry_attempt,
                retry_max_attempts=retry_max_attempts,
                already_in_flight=already_in_flight,
                transcript_resync_required=transcript_resync_required,
                failure_stage=failure_stage,
                error_code=error_code,
            )
        )
    except Exception:
        logger.debug(
            "Failed to schedule retry_state event task_id=%s state=%s",
            task_id,
            state,
            exc_info=True,
        )


async def publish_retry_state_event(
    *,
    task_id: int,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> bool:
    """Publish ``status/retry_state`` and await stream persistence/fanout."""
    event = build_retry_state_event(
        task_id=task_id,
        state=state,
        retry_identity=retry_identity,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name=graph_name,
        checkpoint_id=checkpoint_id,
        retry_mode=retry_mode,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        already_in_flight=already_in_flight,
        transcript_resync_required=transcript_resync_required,
        failure_stage=failure_stage,
        error_code=error_code,
    )
    if event is None:
        return False
    return await _publish_task_status_awaited(task_id, event)


def build_checkpoint_rewind_state_event(
    *,
    task_id: int,
    operation_kind: str,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Build the generic checkpoint rewind lifecycle stream event."""
    normalized_operation_kind = _normalize_checkpoint_rewind_operation_kind(
        operation_kind
    )
    if normalized_operation_kind is None:
        logger.debug(
            "Skipping checkpoint_rewind_state event with invalid operation_kind=%r",
            operation_kind,
        )
        return None

    normalized_state = _normalize_retry_lifecycle_state(state)
    if normalized_state is None:
        logger.debug(
            "Skipping checkpoint_rewind_state event with invalid state=%r", state
        )
        return None

    metadata = _build_checkpoint_operation_metadata(
        task_id=task_id,
        retry_identity=retry_identity,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name=graph_name,
        checkpoint_id=checkpoint_id,
        retry_mode=retry_mode,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        already_in_flight=already_in_flight,
    )
    metadata["operation_kind"] = normalized_operation_kind
    metadata["state"] = normalized_state
    metadata["timestamp"] = _iso_utcnow()
    if transcript_resync_required:
        metadata["transcript_resync_required"] = True
    if isinstance(failure_stage, str) and failure_stage.strip():
        metadata["failure_stage"] = failure_stage.strip()
    if isinstance(error_code, str) and error_code.strip():
        metadata["error_code"] = error_code.strip()

    return {
        "type": "status",
        "content": "checkpoint_rewind_state",
        "metadata": metadata,
    }


def emit_checkpoint_rewind_state_event(
    *,
    task_id: int,
    operation_kind: str,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    """Emit generic ``status/checkpoint_rewind_state`` lifecycle event.

    This event is operation-neutral checkpoint rewind telemetry. Retry
    continues to publish ``retry_state`` for compatibility, while newer
    consumers can watch this generic status packet for retry, stop, and
    future rewind operations without depending on retry-specific names.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(
            publish_checkpoint_rewind_state_event(
                task_id=task_id,
                operation_kind=operation_kind,
                state=state,
                retry_identity=retry_identity,
                turn_id=turn_id,
                workflow_id=workflow_id,
                graph_name=graph_name,
                checkpoint_id=checkpoint_id,
                retry_mode=retry_mode,
                retry_attempt=retry_attempt,
                retry_max_attempts=retry_max_attempts,
                already_in_flight=already_in_flight,
                transcript_resync_required=transcript_resync_required,
                failure_stage=failure_stage,
                error_code=error_code,
            )
        )
    except Exception:
        logger.debug(
            "Failed to schedule checkpoint_rewind_state event task_id=%s state=%s",
            task_id,
            state,
            exc_info=True,
        )


async def publish_checkpoint_rewind_state_event(
    *,
    task_id: int,
    operation_kind: str,
    state: str,
    retry_identity: Optional[Mapping[str, Any]] = None,
    turn_id: Optional[str] = None,
    workflow_id: Optional[int] = None,
    graph_name: Optional[str] = None,
    checkpoint_id: Optional[str] = None,
    retry_mode: Optional[str] = None,
    retry_attempt: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    already_in_flight: Optional[bool] = None,
    transcript_resync_required: bool = False,
    failure_stage: Optional[str] = None,
    error_code: Optional[str] = None,
) -> bool:
    """Publish generic checkpoint rewind lifecycle and await stream ordering."""
    event = build_checkpoint_rewind_state_event(
        task_id=task_id,
        operation_kind=operation_kind,
        state=state,
        retry_identity=retry_identity,
        turn_id=turn_id,
        workflow_id=workflow_id,
        graph_name=graph_name,
        checkpoint_id=checkpoint_id,
        retry_mode=retry_mode,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        already_in_flight=already_in_flight,
        transcript_resync_required=transcript_resync_required,
        failure_stage=failure_stage,
        error_code=error_code,
    )
    if event is None:
        return False
    return await _publish_task_status_awaited(task_id, event)
