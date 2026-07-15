"""Task-scoped checkpoint retry orchestration for failed graph turns.

Responsibilities:
- Validate task ownership for graph retry requests.
- Resolve the canonical retry decision (claimed, already-in-flight, or
  terminal) for a turn via the workflow-layer compare-and-set semantics
  in ``TurnWorkflowService``.
- Schedule exactly one checkpoint retry worker on a successful claim and
  return a typed retry-identity payload for both newly accepted and
  already-active retries.

This module is HTTP-orchestration-only: every durable state transition
and budget check lives in ``TurnWorkflowService``. Worker payloads are
threaded with the canonical retry identity (checkpoint_id, retry_attempt,
retry_max_attempts) plus a sanitized ``previous_failure`` projection so
graph continuation can pick a corrected path without leaking secrets.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..runtime_provider import RuntimeProviderContextResolver
from ..langgraph_chat.checkpoint.turn_workflow_service import (
    TurnWorkflowService,
    build_checkpoint_retry_identity,
    sanitize_previous_failure,
)
from ..langgraph_chat.execution.orchestration.retry_lifecycle import (
    RetryLifecyclePublisher,
    retry_mode_from_identity,
)
from .access_service import get_owned_task_or_404
# Mapping from CheckpointRetryClaimResult.status to the surfaced HTTP detail
# fragment so each typed terminal branch is distinguishable to the frontend
# without leaking the underlying enum values into untyped errors.
_TYPED_CLAIM_FAILURE_HINTS: dict[str, str] = {
    "missing": "missing",
    "not_retryable": "not_retryable",
    "retry_exhausted": "retry_exhausted",
    "invalid_state": "invalid_state",
}

logger = logging.getLogger(__name__)


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class TaskGraphRetryService:
    """Service for checkpoint retry orchestration of failed LangGraph turns."""

    def __init__(self, db: Session):
        self.db = db

    async def retry_graph_execution(
        self,
        *,
        task_id: int,
        user_id: int,
        turn_id: str,
        retry_mode: str,
        graph_name: Optional[str],
        create_task_fn: Callable[[Any], Any],
        run_checkpoint_retry_generation: Callable[..., Any],
        tenant_id: int,
    ) -> dict[str, Any]:
        """Validate and enqueue a checkpoint retry for a failed retryable turn."""
        authorized_task = get_owned_task_or_404(
            db=self.db,
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        runtime_context = RuntimeProviderContextResolver.context_from_task(
            task=authorized_task,
            user_id=user_id,
        )

        normalized_turn_id = turn_id.strip() if isinstance(turn_id, str) else ""
        if not normalized_turn_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="turn_id is required.",
            )

        normalized_retry_mode = retry_mode.strip() if isinstance(retry_mode, str) else ""
        if normalized_retry_mode != "checkpoint":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="retry_mode must be 'checkpoint'.",
            )

        workflow_service = TurnWorkflowService(self.db)

        # Atomic CAS-style claim — the single source of truth for FAILED ->
        # RETRYING transitions. Every terminal/idempotent branch the route
        # must distinguish (claimed / already_retrying / missing /
        # not_retryable / retry_exhausted / invalid_state) is enumerated on
        # the result so the route never has to re-derive eligibility from
        # raw metadata.
        claim_result = workflow_service.claim_checkpoint_retry(
            task_id=task_id,
            turn_id=normalized_turn_id,
            graph_name=graph_name,
            metadata={
                "retry_source": "tasks_router",
                "retry_mode": "checkpoint",
            },
        )
        claim_status = claim_result.status

        if claim_status == "already_retrying":
            identity = claim_result.identity or {}
            if not identity and claim_result.workflow is not None:
                identity = build_checkpoint_retry_identity(
                    claim_result.workflow,
                    task_id=task_id,
                    already_in_flight=True,
                )
            return self._build_identity_response(
                task_id=task_id,
                turn_id=normalized_turn_id,
                identity=identity,
                already_in_flight=True,
            )

        if claim_status != "claimed":
            # Typed terminal — surface a 409 with a detail string that
            # mentions the specific reason code so the frontend / logs can
            # branch on it without parsing free-form text.
            reason_hint = _TYPED_CLAIM_FAILURE_HINTS.get(claim_status, claim_status)
            base_detail = (claim_result.detail or "").strip()
            if base_detail:
                detail = f"{base_detail} ({reason_hint})"
            else:
                detail = f"checkpoint retry rejected: {reason_hint}"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )

        workflow = claim_result.workflow
        if workflow is None:
            # Defensive: claim returned ``claimed`` but didn't surface the
            # workflow row. Treat as a 409 to avoid a broken worker carrier.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Retry claim succeeded but workflow row is missing.",
            )

        identity = claim_result.identity or build_checkpoint_retry_identity(
            workflow,
            task_id=task_id,
            already_in_flight=False,
        )
        retry_lifecycle = RetryLifecyclePublisher(
            task_id=task_id,
            retry_identity=identity,
            turn_id=workflow.turn_id,
            workflow_id=workflow.id,
            graph_name=identity.get("graph_name")
            if isinstance(identity.get("graph_name"), str)
            else graph_name,
            checkpoint_id=identity.get("checkpoint_id"),
            retry_mode=retry_mode_from_identity(identity),
            retry_attempt=_coerce_int_or_none(identity.get("retry_attempt")),
            retry_max_attempts=_coerce_int_or_none(identity.get("retry_max_attempts")),
        )
        await retry_lifecycle.publish("accepted")

        post_claim_metadata = (
            workflow.workflow_metadata
            if isinstance(getattr(workflow, "workflow_metadata", None), dict)
            else {}
        )

        canonical_graph_name = identity.get("graph_name") or workflow.graph_name
        if not canonical_graph_name and isinstance(graph_name, str) and graph_name.strip():
            canonical_graph_name = graph_name.strip()
        if not canonical_graph_name:
            # The CAS guard already requires graph_name to be present, but
            # belt-and-braces: never schedule a worker without one.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Retryable workflow is missing graph_name and cannot be retried.",
            )

        # Sanitized previous-failure projection — drops every secret-bearing
        # field per the Retry Continuation Context Contract. Read from the
        # post-claim row so we capture the failure that triggered this
        # specific attempt.
        sanitized_previous_failure = sanitize_previous_failure(
            post_claim_metadata.get("last_failure")
        )

        reserved_message_id = workflow.reserved_message_id if isinstance(
            workflow.reserved_message_id, int
        ) else None
        turn_sequence = workflow.turn_sequence if isinstance(workflow.turn_sequence, int) else None

        retry_attempt_for_worker = _coerce_int_or_none(identity.get("retry_attempt"))
        retry_max_for_worker = _coerce_int_or_none(identity.get("retry_max_attempts"))

        stored_checkpoint_id: Optional[str] = None
        identity_checkpoint = identity.get("checkpoint_id")
        if isinstance(identity_checkpoint, str) and identity_checkpoint.strip():
            stored_checkpoint_id = identity_checkpoint.strip()
        else:
            raw_post_claim_checkpoint = getattr(workflow, "checkpoint_id", None)
            if isinstance(raw_post_claim_checkpoint, str) and raw_post_claim_checkpoint.strip():
                stored_checkpoint_id = raw_post_claim_checkpoint.strip()

        worker_kwargs: dict[str, Any] = {
            "task_id": task_id,
            "user_id": user_id,
            "graph_thread_id": runtime_context.graph_thread_id,
            "tenant_id": runtime_context.tenant_id,
            "runtime_placement_mode": runtime_context.runtime_placement_mode,
            "workspace_id": runtime_context.workspace_id,
            "actor_type": runtime_context.actor_type.value,
            "actor_id": runtime_context.actor_id,
            "runner_id": runtime_context.runner_id,
            "execution_site_id": runtime_context.execution_site_id,
            "workflow_id": workflow.id,
            "turn_id": workflow.turn_id,
            "turn_sequence": turn_sequence,
            "graph_name": canonical_graph_name,
            "reserved_message_id": reserved_message_id,
        }
        if stored_checkpoint_id is not None:
            worker_kwargs["checkpoint_id"] = stored_checkpoint_id
        if retry_attempt_for_worker is not None:
            worker_kwargs["retry_attempt"] = retry_attempt_for_worker
        if retry_max_for_worker is not None:
            worker_kwargs["retry_max_attempts"] = retry_max_for_worker
        if sanitized_previous_failure:
            worker_kwargs["previous_failure"] = sanitized_previous_failure

        try:
            create_task_fn(
                run_checkpoint_retry_generation(**worker_kwargs)
            )
        except Exception:
            logger.warning(
                "Failed to enqueue checkpoint retry (task=%s turn_id=%s workflow_id=%s)",
                task_id,
                normalized_turn_id,
                workflow.id,
                exc_info=True,
            )
            workflow_service.mark_failed(
                workflow_id=workflow.id,
                metadata={
                    "retryable": True,
                    "retry_mode": "checkpoint",
                    "retry_enqueue_failed": True,
                    "error": "retry_enqueue_failed",
                },
            )
            await retry_lifecycle.publish(
                "failed",
                transcript_resync_required=True,
                failure_stage="enqueue",
                error_code="retry_enqueue_failed",
            )
            raise

        return self._build_identity_response(
            task_id=task_id,
            turn_id=normalized_turn_id,
            identity=identity,
            already_in_flight=False,
        )

    @staticmethod
    def _build_identity_response(
        *,
        task_id: int,
        turn_id: str,
        identity: dict[str, Any],
        already_in_flight: bool,
    ) -> dict[str, Any]:
        """Build a canonical retry response carrying the full retry identity.

        The idempotent ``already_in_flight`` payload and fresh ``claimed``
        payload share the same shape so the frontend never has to fall back
        to placeholder values when the route accepts a retry.
        """
        identity = dict(identity or {})
        return {
            "status": "retrying",
            "task_id": task_id,
            "turn_id": turn_id,
            "retry_mode": identity.get("retry_mode") or "checkpoint",
            "already_in_flight": bool(already_in_flight),
            "workflow_id": identity.get("workflow_id"),
            "checkpoint_id": identity.get("checkpoint_id"),
            "retry_attempt": identity.get("retry_attempt"),
            "retry_max_attempts": identity.get("retry_max_attempts"),
            "graph_name": identity.get("graph_name"),
            "state": identity.get("state") or "retrying",
            "identity": identity,
        }


__all__ = ["TaskGraphRetryService"]
