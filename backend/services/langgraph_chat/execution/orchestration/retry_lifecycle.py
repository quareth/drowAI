"""Retry lifecycle stream and terminal metadata helpers.

This module keeps checkpoint-retry stream publication and retry terminal
workflow metadata in one place so direct retry and retry-resume paths do not
drift on packet shape or cleanup semantics.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional

from backend.services.langgraph_chat.streaming.status_events import (
    publish_checkpoint_rewind_state_event,
    publish_retry_state_event,
)

PublishCheckpointRewind = Callable[..., Awaitable[bool]]
PublishRetryState = Callable[..., Awaitable[bool]]


def _normalize_checkpoint_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)):
        cleaned = str(value).strip()
        return cleaned or None
    return None


def _normalize_retry_mode(value: Any) -> Optional[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def retry_mode_from_identity(
    retry_identity: Optional[Mapping[str, Any]],
    *,
    default: str = "checkpoint",
) -> str:
    """Read retry mode from the canonical identity with a conservative fallback."""
    if isinstance(retry_identity, Mapping):
        value = retry_identity.get("retry_mode")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def build_retry_terminal_metadata(
    *,
    failure_source: str,
    error: str,
    retry_state: str,
) -> dict[str, Any]:
    """Build terminal retry workflow metadata shared by retry and retry-resume."""
    normalized_retry_state = retry_state.strip().lower()
    metadata: dict[str, Any] = {
        "failure_source": failure_source,
        "error": error,
        "active_retry": None,
        "retry_state": normalized_retry_state,
    }
    if normalized_retry_state == "cancelled":
        metadata["terminal_status"] = "cancelled"
        metadata["cancel_requested"] = True
        metadata.setdefault("cancel_reason", "explicit_cancel")
    return metadata


class RetryLifecyclePublisher:
    """Publish the paired retry compatibility and checkpoint-rewind packets."""

    def __init__(
        self,
        *,
        task_id: int,
        retry_identity: Optional[Mapping[str, Any]],
        turn_id: Optional[str],
        workflow_id: Optional[int],
        graph_name: Optional[str],
        checkpoint_id: Optional[int | str],
        retry_mode: Optional[str] = None,
        retry_attempt: Optional[int] = None,
        retry_max_attempts: Optional[int] = None,
        publish_checkpoint_rewind: PublishCheckpointRewind = publish_checkpoint_rewind_state_event,
        publish_retry_state: PublishRetryState = publish_retry_state_event,
    ) -> None:
        self._task_id = task_id
        self._retry_identity = retry_identity
        self._turn_id = turn_id
        self._workflow_id = workflow_id
        self._graph_name = graph_name
        self._checkpoint_id = _normalize_checkpoint_id(checkpoint_id)
        self._retry_mode = _normalize_retry_mode(retry_mode)
        self._retry_attempt = retry_attempt
        self._retry_max_attempts = retry_max_attempts
        self._publish_checkpoint_rewind = publish_checkpoint_rewind
        self._publish_retry_state = publish_retry_state

    async def publish(
        self,
        state: str,
        *,
        transcript_resync_required: bool = False,
        failure_stage: Optional[str] = None,
        error_code: Optional[str] = None,
        turn_id: Optional[str] = None,
        graph_name: Optional[str] = None,
        checkpoint_id: Optional[int | str] = None,
        retry_mode: Optional[str] = None,
    ) -> None:
        """Publish both retry lifecycle packet shapes in deterministic order."""
        resolved_turn_id = turn_id if turn_id is not None else self._turn_id
        resolved_graph_name = (
            graph_name if graph_name is not None else self._graph_name
        )
        resolved_checkpoint_id = (
            _normalize_checkpoint_id(checkpoint_id)
            if checkpoint_id is not None
            else self._checkpoint_id
        )
        resolved_retry_mode = (
            _normalize_retry_mode(retry_mode)
            if retry_mode is not None
            else self._retry_mode
        )
        await self._publish_checkpoint_rewind(
            task_id=self._task_id,
            operation_kind="retry",
            state=state,
            retry_identity=self._retry_identity,
            turn_id=resolved_turn_id,
            workflow_id=self._workflow_id,
            graph_name=resolved_graph_name,
            checkpoint_id=resolved_checkpoint_id,
            retry_mode=resolved_retry_mode,
            retry_attempt=self._retry_attempt,
            retry_max_attempts=self._retry_max_attempts,
            transcript_resync_required=transcript_resync_required,
            failure_stage=failure_stage,
            error_code=error_code,
        )
        await self._publish_retry_state(
            task_id=self._task_id,
            state=state,
            retry_identity=self._retry_identity,
            turn_id=resolved_turn_id,
            workflow_id=self._workflow_id,
            graph_name=resolved_graph_name,
            checkpoint_id=resolved_checkpoint_id,
            retry_mode=resolved_retry_mode,
            retry_attempt=self._retry_attempt,
            retry_max_attempts=self._retry_max_attempts,
            transcript_resync_required=transcript_resync_required,
            failure_stage=failure_stage,
            error_code=error_code,
        )
