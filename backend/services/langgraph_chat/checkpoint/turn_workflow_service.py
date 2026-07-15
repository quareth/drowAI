"""Durable HITL turn workflow state transitions.

Also exposes the shared checkpoint-retry identity builder
(``build_checkpoint_retry_identity``) and the atomic compare-and-set
checkpoint-retry claim primitive (``CheckpointRetryClaimResult`` +
``TurnWorkflowService.claim_checkpoint_retry``) used by the retry route to
guarantee idempotent retry semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging
from typing import Any, Callable, Dict, Literal, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.hitl import TurnWorkflow
from backend.core.time_utils import utc_now
from backend.services.langgraph_chat.compression.window_models import (
    canonical_measured_snapshot_revision,
)

logger = logging.getLogger(__name__)


# Backend-owned default checkpoint retry ceiling. Workflow rows may override
# this via ``workflow_metadata['retry_max_attempts']``; the route never
# enforces a separate frontend budget.
DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS = 2

# Sanitized previous-failure projection contract (see Retry Continuation
# Context Contract in checkpoint-retry-solid-foundation guide §322). Only
# these keys may be forwarded into the retry worker carrier.
_SANITIZED_PREVIOUS_FAILURE_KEYS: tuple[str, ...] = (
    "error_code",
    "failure_stage",
    "graph_name",
    "tool_name",
    "tool_call_id",
    "summary",
)

CheckpointRetryClaimStatus = Literal[
    "claimed",
    "already_retrying",
    "missing",
    "not_retryable",
    "retry_exhausted",
    "invalid_state",
]


class TurnWorkflowState(str, Enum):
    """Persistent workflow states for a turn lifecycle."""

    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    RESUMED = "RESUMED"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CheckpointRetryClaimResult:
    """Outcome of an atomic checkpoint-retry claim attempt.

    ``status`` enumerates every branch the route may need to distinguish:
    ``claimed`` (route should schedule one worker), ``already_retrying``
    (route should return ``already_in_flight`` identity without scheduling),
    ``missing`` / ``not_retryable`` / ``retry_exhausted`` / ``invalid_state``
    (route should surface a typed terminal error).
    """

    status: CheckpointRetryClaimStatus
    workflow: Optional[TurnWorkflow] = None
    identity: Optional[Dict[str, Any]] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class _CheckpointRetryClaimInputs:
    metadata: Dict[str, Any]
    checkpoint_id: str
    retry_attempt_count: int
    retry_max_attempts: int
    graph_name: Optional[str]


def _utcnow() -> datetime:
    return utc_now()


def _normalize_str(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _merge_metadata(
    existing: Optional[Dict[str, Any]],
    updates: Optional[Dict[str, Any]],
    *,
    replace: bool = False,
    conversation_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not updates and not replace:
        return existing
    existing_metadata = dict(existing or {})
    update_metadata = dict(updates or {})
    merged = {} if replace else dict(existing_metadata)
    merged.update(update_metadata)

    existing_context = existing_metadata.get("context_window")
    incoming_context = update_metadata.get("context_window")
    existing_revision = canonical_measured_snapshot_revision(
        existing_context,
        expected_conversation_id=conversation_id,
    )
    incoming_revision = canonical_measured_snapshot_revision(
        incoming_context,
        expected_conversation_id=conversation_id,
    )
    if existing_revision is not None and (
        incoming_revision is None or incoming_revision <= existing_revision
    ):
        preserved_context = dict(existing_context)
        merged["context_window"] = preserved_context
        merged["ceiling_reached"] = bool(
            preserved_context.get("ceiling_reached", False)
        )
        merged["recommended_next_action"] = preserved_context.get(
            "recommended_next_action", "none"
        )
        merged["compression_candidate"] = bool(
            preserved_context.get("compression_candidate", False)
        )
    return merged


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_retry_max_attempts(metadata: Dict[str, Any]) -> int:
    raw = metadata.get("retry_max_attempts")
    if raw is None:
        return DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS
    coerced = _coerce_int(raw, default=DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS)
    if coerced <= 0:
        return DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS
    return coerced


def sanitize_previous_failure(
    raw_failure: Any,
) -> Optional[Dict[str, Any]]:
    """Project a workflow ``last_failure`` blob to its sanitized whitelist.

    Returns only the contract-approved keys (``error_code``,
    ``failure_stage``, ``graph_name``, ``tool_name``, ``tool_call_id``,
    ``summary``) and drops anything that could carry secrets such as raw
    request/response bodies, headers, cookies, JWTs, or API keys. Returns
    ``None`` when no whitelisted fields are present.
    """
    if not isinstance(raw_failure, dict):
        return None
    sanitized: Dict[str, Any] = {}
    for key in _SANITIZED_PREVIOUS_FAILURE_KEYS:
        value = raw_failure.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                sanitized[key] = cleaned
    return sanitized or None


def build_checkpoint_retry_identity(
    workflow: TurnWorkflow,
    *,
    task_id: int,
    already_in_flight: bool = False,
) -> Dict[str, Any]:
    """Return the canonical identity payload for one checkpoint retry attempt.

    The output shape is the single source of truth shared across the retry
    route response, the durable workflow metadata, and the streamed
    ``status/retry_state`` packet so backend, stream, and frontend never
    drift on per-layer naming variants. See Retry Identity Contract in the
    Checkpoint Retry Solid Foundation guide.
    """
    metadata = (
        workflow.workflow_metadata
        if isinstance(workflow.workflow_metadata, dict)
        else {}
    )
    retry_attempt = _coerce_int(metadata.get("retry_attempt_count"), default=0)
    retry_max_attempts = _resolve_retry_max_attempts(metadata)

    raw_state = workflow.state if isinstance(workflow.state, str) else ""
    state_value = raw_state.strip().lower() if raw_state else ""

    raw_checkpoint = getattr(workflow, "checkpoint_id", None)
    normalized_checkpoint = _normalize_str(raw_checkpoint)

    raw_graph_name = getattr(workflow, "graph_name", None)
    normalized_graph_name = _normalize_str(raw_graph_name)

    raw_turn_id = getattr(workflow, "turn_id", None)
    normalized_turn_id = _normalize_str(raw_turn_id)

    retry_mode = metadata.get("retry_mode")
    normalized_retry_mode = _normalize_str(retry_mode) or "checkpoint"

    identity: Dict[str, Any] = {
        "task_id": task_id,
        "turn_id": normalized_turn_id,
        "workflow_id": getattr(workflow, "id", None),
        "graph_name": normalized_graph_name,
        "checkpoint_id": normalized_checkpoint,
        "retry_mode": normalized_retry_mode,
        "retry_attempt": retry_attempt,
        "retry_max_attempts": retry_max_attempts,
        "state": state_value,
        "already_in_flight": bool(already_in_flight),
    }
    return identity


def _checkpoint_retry_already_in_flight_result(
    workflow: TurnWorkflow,
    *,
    task_id: int,
) -> CheckpointRetryClaimResult:
    identity = build_checkpoint_retry_identity(
        workflow,
        task_id=task_id,
        already_in_flight=True,
    )
    return CheckpointRetryClaimResult(
        status="already_retrying",
        workflow=workflow,
        identity=identity,
        detail="Retry already in flight for this turn.",
    )


def _evaluate_checkpoint_retry_claim_inputs(
    workflow: TurnWorkflow,
    *,
    requested_graph_name: Optional[str],
) -> tuple[Optional[_CheckpointRetryClaimInputs], Optional[CheckpointRetryClaimResult]]:
    existing_metadata = (
        workflow.workflow_metadata
        if isinstance(workflow.workflow_metadata, dict)
        else {}
    )
    if not bool(existing_metadata.get("retryable")):
        return None, CheckpointRetryClaimResult(
            status="not_retryable",
            workflow=workflow,
            detail="Failed workflow is not marked retryable.",
        )

    normalized_checkpoint = _normalize_str(getattr(workflow, "checkpoint_id", None))
    if not normalized_checkpoint:
        return None, CheckpointRetryClaimResult(
            status="not_retryable",
            workflow=workflow,
            detail="Failed workflow has no stored checkpoint_id to retry from.",
        )

    retry_attempt_count = _coerce_int(
        existing_metadata.get("retry_attempt_count"), default=0
    )
    retry_max_attempts = _resolve_retry_max_attempts(existing_metadata)
    if retry_attempt_count >= retry_max_attempts:
        return None, CheckpointRetryClaimResult(
            status="retry_exhausted",
            workflow=workflow,
            detail=(
                "Checkpoint retry budget is exhausted "
                f"(retry_attempt_count={retry_attempt_count}, "
                f"retry_max_attempts={retry_max_attempts})."
            ),
        )

    normalized_graph_name = _normalize_str(requested_graph_name) or _normalize_str(
        getattr(workflow, "graph_name", None)
    )
    return (
        _CheckpointRetryClaimInputs(
            metadata=dict(existing_metadata),
            checkpoint_id=normalized_checkpoint,
            retry_attempt_count=retry_attempt_count,
            retry_max_attempts=retry_max_attempts,
            graph_name=normalized_graph_name,
        ),
        None,
    )


def _resolve_checkpoint_retry_cas_miss(
    db: Session,
    *,
    workflow_id: int,
    task_id: int,
) -> CheckpointRetryClaimResult:
    refreshed = db.get(TurnWorkflow, workflow_id)
    if refreshed is None:
        return CheckpointRetryClaimResult(
            status="missing",
            detail="Workflow disappeared during retry claim.",
        )
    if refreshed.state == TurnWorkflowState.RETRYING.value:
        return _checkpoint_retry_already_in_flight_result(
            refreshed,
            task_id=task_id,
        )
    return CheckpointRetryClaimResult(
        status="invalid_state",
        workflow=refreshed,
        detail=(
            f"Workflow state changed during retry claim to {refreshed.state!r}."
        ),
    )


def _build_active_checkpoint_retry_block(
    workflow: TurnWorkflow,
    *,
    retry_attempt: int,
    retry_max_attempts: int,
    graph_name: Optional[str],
    previous_failure: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    active_retry_block: Dict[str, Any] = {
        "attempt": retry_attempt,
        "max_attempts": retry_max_attempts,
        "state": "retrying",
        "checkpoint_id": _normalize_str(getattr(workflow, "checkpoint_id", None)),
        "graph_name": graph_name or workflow.graph_name,
    }
    if isinstance(previous_failure, dict):
        previous_error_code = previous_failure.get("error_code")
        if previous_error_code:
            active_retry_block["previous_error_code"] = previous_error_code
        previous_failure_stage = previous_failure.get("failure_stage")
        if previous_failure_stage:
            active_retry_block["previous_failure_stage"] = previous_failure_stage
    return {
        key: value for key, value in active_retry_block.items() if value is not None
    }


def _build_claimed_checkpoint_retry_metadata(
    workflow: TurnWorkflow,
    *,
    metadata: Optional[Dict[str, Any]],
    retry_attempt: int,
    retry_max_attempts: int,
    graph_name: Optional[str],
) -> Dict[str, Any]:
    merged_metadata = _merge_metadata(
        workflow.workflow_metadata,
        metadata,
        conversation_id=workflow.conversation_id,
    )
    merged_metadata = dict(merged_metadata or {})
    merged_metadata["retry_attempt_count"] = retry_attempt
    merged_metadata.setdefault("retry_mode", "checkpoint")
    merged_metadata["retry_max_attempts"] = retry_max_attempts
    merged_metadata["last_retry"] = {
        "retry_mode": merged_metadata.get("retry_mode") or "checkpoint",
        "graph_name": graph_name or workflow.graph_name,
        "attempt": retry_attempt,
    }
    previous_failure = sanitize_previous_failure(merged_metadata.get("last_failure"))
    merged_metadata["active_retry"] = _build_active_checkpoint_retry_block(
        workflow,
        retry_attempt=retry_attempt,
        retry_max_attempts=retry_max_attempts,
        graph_name=graph_name,
        previous_failure=previous_failure,
    )
    return merged_metadata


class TurnWorkflowService:
    """Transactional state transitions for durable HITL workflows."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(
                f"Cannot resolve tenant for turn workflow write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)

    def get_workflow(self, workflow_id: int) -> Optional[TurnWorkflow]:
        return self.db.get(TurnWorkflow, workflow_id)

    def get_latest_turn_workflow(
        self,
        *,
        task_id: int,
        turn_id: str,
    ) -> Optional[TurnWorkflow]:
        """Return the latest workflow row for one logical turn."""
        return (
            self.db.query(TurnWorkflow)
            .filter(
                TurnWorkflow.task_id == task_id,
                TurnWorkflow.turn_id == turn_id,
            )
            .order_by(TurnWorkflow.updated_at.desc(), TurnWorkflow.id.desc())
            .first()
        )

    def get_latest_retryable_failed_workflow(
        self,
        *,
        task_id: int,
        turn_id: str,
        graph_name: Optional[str] = None,
    ) -> Optional[TurnWorkflow]:
        """Return the latest failed workflow marked retryable for one turn."""
        row = self.get_latest_turn_workflow(task_id=task_id, turn_id=turn_id)
        if row is None or row.state != TurnWorkflowState.FAILED.value:
            return None

        metadata = (
            row.workflow_metadata if isinstance(row.workflow_metadata, dict) else {}
        )
        if not bool(metadata.get("retryable")):
            return None

        normalized_graph_name = _normalize_str(graph_name)
        if (
            normalized_graph_name
            and isinstance(row.graph_name, str)
            and row.graph_name.strip()
            and row.graph_name.strip() != normalized_graph_name
        ):
            logger.warning(
                "Ignoring checkpoint retry graph_name mismatch (task=%s turn_id=%s requested=%s canonical=%s)",
                task_id,
                turn_id,
                normalized_graph_name,
                row.graph_name,
            )
        return row

    def get_latest_waiting_workflow(
        self,
        task_id: int,
        graph_name: Optional[str] = None,
    ) -> Optional[TurnWorkflow]:
        query = self.db.query(TurnWorkflow).filter(
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.state == TurnWorkflowState.WAITING_FOR_HUMAN.value,
        )
        if graph_name:
            query = query.filter(
                or_(
                    TurnWorkflow.graph_name == graph_name,
                    TurnWorkflow.graph_name.is_(None),
                )
            )
        return query.order_by(
            TurnWorkflow.updated_at.desc(), TurnWorkflow.id.desc()
        ).first()

    def start_turn(
        self,
        *,
        task_id: int,
        conversation_id: str,
        turn_id: str,
        turn_sequence: Optional[int],
        graph_name: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        interrupt_type: Optional[str] = None,
        reserved_message_id: Optional[int] = None,
        resume_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TurnWorkflow:
        existing = (
            self.db.query(TurnWorkflow)
            .filter(
                TurnWorkflow.task_id == task_id,
                TurnWorkflow.turn_id == turn_id,
            )
            .first()
        )
        if existing is not None:
            changed = False
            if reserved_message_id is not None and existing.reserved_message_id is None:
                existing.reserved_message_id = reserved_message_id
                changed = True
            if turn_sequence is not None and existing.turn_sequence is None:
                existing.turn_sequence = turn_sequence
                changed = True
            normalized_graph_name = _normalize_str(graph_name)
            if normalized_graph_name and not existing.graph_name:
                existing.graph_name = normalized_graph_name
                changed = True
            normalized_checkpoint = _normalize_str(checkpoint_id)
            if normalized_checkpoint and not existing.checkpoint_id:
                existing.checkpoint_id = normalized_checkpoint
                changed = True
            normalized_interrupt = _normalize_str(interrupt_type)
            if normalized_interrupt and not existing.interrupt_type:
                existing.interrupt_type = normalized_interrupt
                changed = True
            normalized_resume = _normalize_str(resume_key)
            if normalized_resume and not existing.resume_key:
                existing.resume_key = normalized_resume
                changed = True
            if metadata:
                merged = _merge_metadata(
                    existing.workflow_metadata,
                    metadata,
                    conversation_id=existing.conversation_id,
                )
                if merged != existing.workflow_metadata:
                    existing.workflow_metadata = merged
                    changed = True
            if changed:
                self.db.commit()
                self.db.refresh(existing)
            return existing

        row = TurnWorkflow(
            task_id=task_id,
            tenant_id=self._resolve_task_tenant_id(task_id),
            conversation_id=conversation_id or "",
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            state=TurnWorkflowState.RUNNING.value,
            graph_name=_normalize_str(graph_name),
            checkpoint_id=_normalize_str(checkpoint_id),
            interrupt_type=_normalize_str(interrupt_type),
            reserved_message_id=reserved_message_id,
            resume_key=_normalize_str(resume_key),
            workflow_metadata=_merge_metadata(
                None,
                metadata,
                conversation_id=conversation_id or "",
            ),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def ensure_waiting_workflow(
        self,
        *,
        task_id: int,
        conversation_id: str,
        turn_id: str,
        turn_sequence: Optional[int],
        graph_name: Optional[str],
        checkpoint_id: Optional[str],
        interrupt_type: Optional[str],
        reserved_message_id: Optional[int],
        resume_key: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TurnWorkflow:
        existing = (
            self.db.query(TurnWorkflow)
            .filter(
                TurnWorkflow.task_id == task_id,
                TurnWorkflow.turn_id == turn_id,
            )
            .first()
        )
        if existing is not None and existing.state in {
            TurnWorkflowState.RESUMED.value,
            TurnWorkflowState.COMPLETED.value,
            TurnWorkflowState.FAILED.value,
        }:
            return existing
        if existing is None:
            existing = TurnWorkflow(
                task_id=task_id,
                tenant_id=self._resolve_task_tenant_id(task_id),
                conversation_id=conversation_id or "",
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                state=TurnWorkflowState.WAITING_FOR_HUMAN.value,
                waiting_at=_utcnow(),
            )
            self.db.add(existing)

        existing.state = TurnWorkflowState.WAITING_FOR_HUMAN.value
        if conversation_id:
            existing.conversation_id = conversation_id
        if turn_sequence is not None:
            existing.turn_sequence = turn_sequence
        if graph_name:
            existing.graph_name = graph_name
        if checkpoint_id:
            existing.checkpoint_id = checkpoint_id
        if interrupt_type:
            existing.interrupt_type = interrupt_type
        if reserved_message_id is not None:
            existing.reserved_message_id = reserved_message_id
        if resume_key:
            existing.resume_key = resume_key
        existing.workflow_metadata = _merge_metadata(
            existing.workflow_metadata,
            metadata,
            conversation_id=existing.conversation_id,
        )
        existing.waiting_at = _utcnow()
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def try_begin_resume(
        self,
        *,
        task_id: int,
        resume_key: str,
        checkpoint_id: Optional[str] = None,
        graph_name: Optional[str] = None,
        reserved_message_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TurnWorkflow]:
        normalized_resume_key = _normalize_str(resume_key)
        if not normalized_resume_key:
            return None

        query = self.db.query(TurnWorkflow).filter(
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.state == TurnWorkflowState.WAITING_FOR_HUMAN.value,
        )
        normalized_checkpoint = _normalize_str(checkpoint_id)
        if normalized_checkpoint:
            query = query.filter(
                or_(
                    TurnWorkflow.checkpoint_id == normalized_checkpoint,
                    TurnWorkflow.resume_key == normalized_resume_key,
                )
            )
        else:
            query = query.filter(TurnWorkflow.resume_key == normalized_resume_key)

        normalized_graph_name = _normalize_str(graph_name)
        if normalized_graph_name:
            query = query.filter(
                or_(
                    TurnWorkflow.graph_name == normalized_graph_name,
                    TurnWorkflow.graph_name.is_(None),
                )
            )

        candidate = query.order_by(
            TurnWorkflow.updated_at.desc(), TurnWorkflow.id.desc()
        ).first()
        if candidate is None:
            return None

        updated_count = (
            self.db.query(TurnWorkflow)
            .filter(
                TurnWorkflow.id == candidate.id,
                TurnWorkflow.state == TurnWorkflowState.WAITING_FOR_HUMAN.value,
            )
            .update(
                {
                    TurnWorkflow.state: TurnWorkflowState.RESUMED.value,
                    TurnWorkflow.resume_key: normalized_resume_key,
                    TurnWorkflow.resumed_at: _utcnow(),
                    TurnWorkflow.updated_at: _utcnow(),
                },
                synchronize_session=False,
            )
        )
        if updated_count != 1:
            self.db.rollback()
            return None

        refreshed = self.db.get(TurnWorkflow, candidate.id)
        if refreshed is None:
            self.db.rollback()
            return None
        if normalized_checkpoint:
            refreshed.checkpoint_id = normalized_checkpoint
        if normalized_graph_name:
            refreshed.graph_name = normalized_graph_name
        if reserved_message_id is not None:
            refreshed.reserved_message_id = reserved_message_id
        refreshed.workflow_metadata = _merge_metadata(
            refreshed.workflow_metadata,
            metadata,
            conversation_id=refreshed.conversation_id,
        )
        self.db.commit()
        self.db.refresh(refreshed)
        return refreshed

    def mark_waiting_for_human(
        self,
        *,
        workflow_id: int,
        checkpoint_id: Optional[str] = None,
        interrupt_type: Optional[str] = None,
        graph_name: Optional[str] = None,
        reserved_message_id: Optional[int] = None,
        resume_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TurnWorkflow]:
        row = self.db.get(TurnWorkflow, workflow_id)
        if row is None:
            return None
        if row.state not in {
            TurnWorkflowState.RUNNING.value,
            TurnWorkflowState.RESUMED.value,
            TurnWorkflowState.WAITING_FOR_HUMAN.value,
        }:
            raise ValueError(
                f"Invalid transition to WAITING_FOR_HUMAN from {row.state}"
            )

        row.state = TurnWorkflowState.WAITING_FOR_HUMAN.value
        row.waiting_at = _utcnow()
        normalized_checkpoint = _normalize_str(checkpoint_id)
        if normalized_checkpoint:
            row.checkpoint_id = normalized_checkpoint
        normalized_interrupt = _normalize_str(interrupt_type)
        if normalized_interrupt:
            row.interrupt_type = normalized_interrupt
        normalized_graph_name = _normalize_str(graph_name)
        if normalized_graph_name:
            row.graph_name = normalized_graph_name
        if reserved_message_id is not None:
            row.reserved_message_id = reserved_message_id
        normalized_resume = _normalize_str(resume_key)
        if normalized_resume:
            row.resume_key = normalized_resume
        row.workflow_metadata = _merge_metadata(
            row.workflow_metadata,
            metadata,
            conversation_id=row.conversation_id,
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_resumed(
        self,
        *,
        workflow_id: int,
        resume_key: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        graph_name: Optional[str] = None,
        reserved_message_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TurnWorkflow]:
        row = self.db.get(TurnWorkflow, workflow_id)
        if row is None:
            return None
        if row.state == TurnWorkflowState.RESUMED.value:
            return row
        if row.state != TurnWorkflowState.WAITING_FOR_HUMAN.value:
            raise ValueError(f"Invalid transition to RESUMED from {row.state}")

        row.state = TurnWorkflowState.RESUMED.value
        row.resumed_at = _utcnow()
        normalized_resume = _normalize_str(resume_key)
        if normalized_resume:
            row.resume_key = normalized_resume
        normalized_checkpoint = _normalize_str(checkpoint_id)
        if normalized_checkpoint:
            row.checkpoint_id = normalized_checkpoint
        normalized_graph_name = _normalize_str(graph_name)
        if normalized_graph_name:
            row.graph_name = normalized_graph_name
        if reserved_message_id is not None:
            row.reserved_message_id = reserved_message_id
        row.workflow_metadata = _merge_metadata(
            row.workflow_metadata,
            metadata,
            conversation_id=row.conversation_id,
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def get_active_checkpoint_retry_workflow(
        self,
        *,
        task_id: int,
        turn_id: str,
        graph_name: Optional[str] = None,
    ) -> Optional[TurnWorkflow]:
        """Return the latest workflow currently in RETRYING for one turn.

        Used when ``get_latest_retryable_failed_workflow`` returns ``None``
        but a duplicate retry post may still be referencing an in-flight
        retry. Filters by ``graph_name`` only when the row carries one.
        """
        normalized_turn_id = _normalize_str(turn_id)
        if not normalized_turn_id:
            return None
        query = self.db.query(TurnWorkflow).filter(
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.turn_id == normalized_turn_id,
            TurnWorkflow.state == TurnWorkflowState.RETRYING.value,
        )
        normalized_graph_name = _normalize_str(graph_name)
        if normalized_graph_name:
            query = query.filter(
                or_(
                    TurnWorkflow.graph_name == normalized_graph_name,
                    TurnWorkflow.graph_name.is_(None),
                )
            )
        return query.order_by(
            TurnWorkflow.updated_at.desc(), TurnWorkflow.id.desc()
        ).first()

    def claim_checkpoint_retry(
        self,
        *,
        task_id: int,
        turn_id: str,
        graph_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckpointRetryClaimResult:
        """Atomically claim a checkpoint retry slot for one turn.

        Compare-and-set semantics: only one concurrent caller can move the
        workflow row from FAILED -> RETRYING. Attempt count is incremented
        exactly once on the ``claimed`` branch and is left untouched on
        every other branch. The result enumerates each terminal/idempotent
        outcome the route needs to distinguish.
        """
        normalized_turn_id = _normalize_str(turn_id)
        if not normalized_turn_id:
            return CheckpointRetryClaimResult(
                status="missing",
                detail="turn_id is required.",
            )

        latest = self.get_latest_turn_workflow(
            task_id=task_id,
            turn_id=normalized_turn_id,
        )
        if latest is None:
            return CheckpointRetryClaimResult(
                status="missing",
                detail="No workflow exists for this turn.",
            )

        # Idempotent: a duplicate retry while one is already in flight.
        if latest.state == TurnWorkflowState.RETRYING.value:
            return _checkpoint_retry_already_in_flight_result(
                latest,
                task_id=task_id,
            )

        # Terminal/non-retry states cannot be claimed.
        if latest.state != TurnWorkflowState.FAILED.value:
            return CheckpointRetryClaimResult(
                status="invalid_state",
                workflow=latest,
                detail=(
                    f"Workflow state {latest.state!r} is not eligible for "
                    "checkpoint retry."
                ),
            )

        claim_inputs, ineligible_result = _evaluate_checkpoint_retry_claim_inputs(
            latest,
            requested_graph_name=graph_name,
        )
        if ineligible_result is not None:
            return ineligible_result
        if claim_inputs is None:
            return CheckpointRetryClaimResult(
                status="missing",
                detail="No workflow exists for this turn.",
            )

        # CAS update: only flip the row if it is still FAILED. SQLAlchemy
        # bulk update with a state predicate gives us single-row CAS
        # semantics across concurrent callers.
        new_attempt = claim_inputs.retry_attempt_count + 1
        update_payload: Dict[str, Any] = {
            TurnWorkflow.state: TurnWorkflowState.RETRYING.value,
            TurnWorkflow.failed_at: None,
            TurnWorkflow.completed_at: None,
            TurnWorkflow.resumed_at: _utcnow(),
            TurnWorkflow.updated_at: _utcnow(),
        }
        if claim_inputs.graph_name:
            update_payload[TurnWorkflow.graph_name] = claim_inputs.graph_name

        updated_count = (
            self.db.query(TurnWorkflow)
            .filter(
                TurnWorkflow.id == latest.id,
                TurnWorkflow.state == TurnWorkflowState.FAILED.value,
            )
            .update(update_payload, synchronize_session=False)
        )

        if updated_count != 1:
            # Lost the CAS race. Re-read and answer based on the new state.
            self.db.rollback()
            return _resolve_checkpoint_retry_cas_miss(
                self.db,
                workflow_id=latest.id,
                task_id=task_id,
            )

        refreshed = self.db.get(TurnWorkflow, latest.id)
        if refreshed is None:
            self.db.rollback()
            return CheckpointRetryClaimResult(
                status="missing",
                detail="Workflow disappeared after retry claim.",
            )

        # Bump attempt count and merge canonical retry metadata exactly once.
        refreshed.workflow_metadata = _build_claimed_checkpoint_retry_metadata(
            refreshed,
            metadata=metadata,
            retry_attempt=new_attempt,
            retry_max_attempts=claim_inputs.retry_max_attempts,
            graph_name=claim_inputs.graph_name,
        )
        self.db.commit()
        self.db.refresh(refreshed)

        identity = build_checkpoint_retry_identity(
            refreshed,
            task_id=task_id,
            already_in_flight=False,
        )
        return CheckpointRetryClaimResult(
            status="claimed",
            workflow=refreshed,
            identity=identity,
        )

    def mark_completed(
        self,
        *,
        workflow_id: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[TurnWorkflow]:
        row = self.db.get(TurnWorkflow, workflow_id)
        if row is None:
            return None
        if row.state == TurnWorkflowState.COMPLETED.value:
            return row
        if row.state not in {
            TurnWorkflowState.RUNNING.value,
            TurnWorkflowState.RESUMED.value,
            TurnWorkflowState.RETRYING.value,
        }:
            raise ValueError(f"Invalid transition to COMPLETED from {row.state}")

        row.state = TurnWorkflowState.COMPLETED.value
        row.completed_at = _utcnow()
        row.workflow_metadata = _merge_metadata(
            row.workflow_metadata,
            metadata,
            conversation_id=row.conversation_id,
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def mark_failed(
        self,
        *,
        workflow_id: int,
        checkpoint_id: Optional[str] = None,
        graph_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        replace_metadata: bool = False,
    ) -> Optional[TurnWorkflow]:
        row = self.db.get(TurnWorkflow, workflow_id)
        if row is None:
            return None
        if row.state == TurnWorkflowState.FAILED.value:
            return row
        if row.state not in {
            TurnWorkflowState.RUNNING.value,
            TurnWorkflowState.RESUMED.value,
            TurnWorkflowState.RETRYING.value,
            TurnWorkflowState.WAITING_FOR_HUMAN.value,
        }:
            raise ValueError(f"Invalid transition to FAILED from {row.state}")

        row.state = TurnWorkflowState.FAILED.value
        row.failed_at = _utcnow()
        normalized_checkpoint = _normalize_str(checkpoint_id)
        if normalized_checkpoint:
            row.checkpoint_id = normalized_checkpoint
        normalized_graph_name = _normalize_str(graph_name)
        if normalized_graph_name:
            row.graph_name = normalized_graph_name
        row.workflow_metadata = _merge_metadata(
            row.workflow_metadata,
            metadata,
            replace=replace_metadata,
            conversation_id=row.conversation_id,
        )
        self.db.commit()
        self.db.refresh(row)
        return row


def start_turn_workflow_best_effort(
    *,
    task_id: int,
    conversation_id: str,
    turn_id: str,
    turn_sequence: Optional[int],
    graph_name: Optional[str],
    reserved_message_id: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[int]:
    """Best-effort wrapper to persist workflow start state."""
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        row = workflow_service.start_turn(
            task_id=task_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            graph_name=graph_name,
            reserved_message_id=reserved_message_id,
            metadata=metadata,
        )
        return row.id
    except Exception:
        logger.warning(
            "Failed to start turn workflow (task=%s turn_id=%s)",
            task_id,
            turn_id,
            exc_info=True,
        )
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_turn_workflow_waiting_best_effort(
    *,
    workflow_id: Optional[int],
    checkpoint_id: Optional[str],
    interrupt_type: Optional[str],
    graph_name: Optional[str],
    reserved_message_id: Optional[int],
    resume_key: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort wrapper to transition workflow to WAITING_FOR_HUMAN."""
    if workflow_id is None:
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        workflow_service.mark_waiting_for_human(
            workflow_id=workflow_id,
            checkpoint_id=checkpoint_id,
            interrupt_type=interrupt_type,
            graph_name=graph_name,
            reserved_message_id=reserved_message_id,
            resume_key=resume_key,
            metadata=metadata,
        )
    except Exception:
        logger.warning(
            "Failed to transition workflow %s to WAITING_FOR_HUMAN",
            workflow_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_turn_workflow_completed_best_effort(
    *,
    workflow_id: Optional[int],
    metadata: Optional[Dict[str, Any]] = None,
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort wrapper to transition workflow to COMPLETED."""
    if workflow_id is None:
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        workflow_service.mark_completed(workflow_id=workflow_id, metadata=metadata)
    except Exception:
        logger.warning(
            "Failed to transition workflow %s to COMPLETED",
            workflow_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_turn_workflow_failed_best_effort(
    *,
    workflow_id: Optional[int],
    checkpoint_id: Optional[str] = None,
    graph_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    replace_metadata: bool = False,
    session_factory: Optional[Callable[[], Session]] = None,
) -> None:
    """Best-effort wrapper to transition workflow to FAILED."""
    if workflow_id is None:
        return
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        workflow_service.mark_failed(
            workflow_id=workflow_id,
            checkpoint_id=checkpoint_id,
            graph_name=graph_name,
            metadata=metadata,
            replace_metadata=replace_metadata,
        )
    except Exception:
        logger.warning(
            "Failed to transition workflow %s to FAILED",
            workflow_id,
            exc_info=True,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def resolve_reserved_message_id_from_workflow_best_effort(
    workflow_id: Optional[int],
    *,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[int]:
    """Best-effort lookup of reserved message id for a workflow row."""
    if workflow_id is None:
        return None
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        row = workflow_service.get_workflow(workflow_id)
        if row is None:
            return None
        return (
            row.reserved_message_id
            if isinstance(row.reserved_message_id, int)
            else None
        )
    except Exception:
        logger.debug(
            "Failed to resolve reserved_message_id for workflow %s",
            workflow_id,
            exc_info=True,
        )
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def resolve_turn_id_from_workflow_best_effort(
    workflow_id: Optional[int],
    *,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[str]:
    """Best-effort lookup of turn id for a workflow row."""
    if workflow_id is None:
        return None
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        row = workflow_service.get_workflow(workflow_id)
        if row is None:
            return None
        turn_id = getattr(row, "turn_id", None)
        if isinstance(turn_id, str) and turn_id.strip():
            return turn_id.strip()
        return None
    except Exception:
        logger.debug(
            "Failed to resolve turn_id for workflow %s",
            workflow_id,
            exc_info=True,
        )
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


def resolve_checkpoint_retry_identity_best_effort(
    *,
    workflow_id: Optional[int],
    task_id: int,
    session_factory: Optional[Callable[[], Session]] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort canonical retry identity for a workflow row.

    Loads the workflow and projects it through ``build_checkpoint_retry_identity``
    so the retry orchestrator can emit lifecycle stream events that match
    the route response identity exactly. Returns ``None`` when the workflow
    row is missing or the lookup fails — the caller is expected to fall back
    to a partial identity from its in-scope kwargs.
    """
    if workflow_id is None:
        return None
    if session_factory is None:
        from backend.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        workflow_service = TurnWorkflowService(db)
        row = workflow_service.get_workflow(workflow_id)
        if row is None:
            return None
        return build_checkpoint_retry_identity(row, task_id=task_id)
    except Exception:
        logger.debug(
            "Failed to resolve checkpoint retry identity for workflow %s",
            workflow_id,
            exc_info=True,
        )
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass


__all__ = [
    "TurnWorkflowService",
    "TurnWorkflowState",
    "CheckpointRetryClaimResult",
    "CheckpointRetryClaimStatus",
    "DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS",
    "build_checkpoint_retry_identity",
    "sanitize_previous_failure",
    "start_turn_workflow_best_effort",
    "mark_turn_workflow_waiting_best_effort",
    "mark_turn_workflow_completed_best_effort",
    "mark_turn_workflow_failed_best_effort",
    "resolve_reserved_message_id_from_workflow_best_effort",
    "resolve_turn_id_from_workflow_best_effort",
    "resolve_checkpoint_retry_identity_best_effort",
]
