"""Task interrupt and resume orchestration service.

Responsibilities:
- Fetch pending interrupt state for a task.
- Coordinate durable resume workflow transitions and enqueue resume generation.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.hitl import InterruptTicket, InterruptTicketState
from ..runtime_provider import RuntimeProviderContextResolver
from ..langgraph_chat.checkpoint import interrupt_state_service
from ..langgraph_chat.checkpoint.interrupt_ticket_service import (
    InterruptTicketClaimConflictError,
    InterruptTicketNotFoundError,
    InterruptTicketService,
)
from ..langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowService
from .access_service import get_owned_task_or_404

logger = logging.getLogger(__name__)


class TaskInterruptService:
    """Service for interrupt retrieval and authoritative resume orchestration.

    Lifecycle authority contract:
    - Observed interrupt registration is performed by
      `InterruptTicketService.create_or_update_pending`.
    - Resume claim is performed by `InterruptTicketService.claim_for_resume` and
      must return HTTP 409 when the interrupt is no longer pending.
    - Post-claim lifecycle completion (`mark_resumed`, `mark_completed`,
      `mark_failed`) remains delegated to the ticket lifecycle service.
    - Requeue to `PENDING` is only allowed in explicit resume-enqueue failure
      handling, never during normal resume flow.
    """

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _raise_claim_http_error(exc: Exception) -> None:
        if isinstance(exc, InterruptTicketNotFoundError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Interrupt ticket not found for this task.",
            ) from exc
        if isinstance(exc, InterruptTicketClaimConflictError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Interrupt cannot be resumed because it is no longer pending. "
                    f"{exc}"
                ),
            ) from exc
        raise exc

    def _claim_pending_ticket_or_raise(
        self,
        *,
        ticket_service: InterruptTicketService,
        interrupt_id: str,
        task_id: int,
        user_id: int,
    ) -> Any:
        try:
            return ticket_service.claim_for_resume(
                interrupt_id=interrupt_id,
                task_id=task_id,
                user_id=user_id,
            )
        except (InterruptTicketNotFoundError, InterruptTicketClaimConflictError) as exc:
            self._raise_claim_http_error(exc)

    @staticmethod
    def _requeue_after_enqueue_failure(
        *,
        workflow_service: TurnWorkflowService,
        workflow: Any,
        ticket_service: InterruptTicketService,
        interrupt_id: str,
        task_id: int,
        checkpoint_id: Optional[str],
        interrupt_type: str,
        graph_name: str,
        reserved_message_id: Optional[int],
        resume_key: str,
    ) -> None:
        """Best-effort recovery branch used only when enqueueing resume fails."""
        try:
            workflow_service.mark_waiting_for_human(
                workflow_id=workflow.id,
                checkpoint_id=checkpoint_id,
                interrupt_type=interrupt_type,
                graph_name=graph_name,
                reserved_message_id=reserved_message_id,
                resume_key=resume_key,
                metadata={"resume_enqueue_failed": True},
            )
        except Exception:
            logger.warning(
                "Failed to revert workflow to WAITING_FOR_HUMAN after resume enqueue failure (task=%s)",
                task_id,
                exc_info=True,
            )
        try:
            ticket_service.mark_pending(
                interrupt_id=interrupt_id,
                task_id=task_id,
            )
        except Exception:
            logger.warning(
                "Failed to revert interrupt ticket to PENDING after resume enqueue failure "
                "(task=%s, interrupt_id=%s)",
                task_id,
                interrupt_id,
                exc_info=True,
            )

    def list_pending_interrupts_for_user(
        self,
        user_id: int,
        tenant_id: int,
    ) -> list[dict[str, Any]]:
        """Return pending interrupt summaries scoped to authorized tenant/user context."""
        query = (
            self.db.query(InterruptTicket, Task)
            .join(Task, Task.id == InterruptTicket.task_id)
            .filter(InterruptTicket.state == InterruptTicketState.PENDING)
        )
        query = query.filter(InterruptTicket.tenant_id == int(tenant_id))
        query = query.filter(Task.user_id == int(user_id))
        rows = query.order_by(InterruptTicket.updated_at.desc(), InterruptTicket.id.desc()).all()
        results: list[dict[str, Any]] = []
        for ticket, task in rows:
            results.append(
                {
                    "task_id": ticket.task_id,
                    "task_name": task.name,
                    "interrupt_id": ticket.interrupt_id,
                    "interrupt_type": ticket.interrupt_type,
                    "graph_name": ticket.graph_name,
                    "thread_id": ticket.thread_id,
                    "turn_id": ticket.turn_id,
                    "turn_sequence": ticket.turn_sequence,
                    "checkpoint_id": ticket.checkpoint_id,
                    "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
                    "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
                }
            )
        return results

    def _get_authoritative_pending_ticket(self, *, task_id: int) -> Optional[InterruptTicket]:
        """Return the latest authoritative pending ticket for this task."""
        return (
            self.db.query(InterruptTicket)
            .filter(
                InterruptTicket.task_id == task_id,
                InterruptTicket.state == InterruptTicketState.PENDING,
            )
            .order_by(InterruptTicket.updated_at.desc(), InterruptTicket.id.desc())
            .first()
        )

    async def get_task_interrupt(
        self,
        task_id: int,
        user_id: int,
        interrupt_service: Any,
        tenant_id: int,
    ) -> dict[str, Any]:
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
        ticket = self._get_authoritative_pending_ticket(task_id=task_id)
        if ticket is None:
            return {"has_interrupt": False, "task_id": task_id}

        response: dict[str, Any] = {
            "has_interrupt": True,
            "task_id": task_id,
            "thread_id": ticket.thread_id,
            "graph_name": ticket.graph_name,
            "interrupt_id": ticket.interrupt_id,
            "checkpoint_id": ticket.checkpoint_id,
            "interrupt_type": ticket.interrupt_type,
            "payload": ticket.payload_snapshot if isinstance(ticket.payload_snapshot, dict) else None,
            "resumable": True,
        }

        hydrated_interrupt = await interrupt_service.get_pending_interrupt(
            task_id,
            graph_name=ticket.graph_name,
            thread_id=ticket.thread_id,
            graph_thread_id=runtime_context.graph_thread_id,
        )
        if not isinstance(hydrated_interrupt, dict):
            return response

        hydrated_id = hydrated_interrupt.get("interrupt_id")
        if isinstance(hydrated_id, str) and hydrated_id.strip() and hydrated_id.strip() != ticket.interrupt_id:
            return response

        for field in (
            "thread_id",
            "graph_name",
            "checkpoint_id",
            "interrupt_type",
            "payload",
            "resumable",
        ):
            if field in hydrated_interrupt:
                response[field] = hydrated_interrupt.get(field)
        return response

    async def resume_graph_execution(
        self,
        *,
        task_id: int,
        user_id: int,
        interrupt_id: Optional[str],
        graph_name: Optional[str],
        response_payload: dict[str, Any],
        create_task_fn: Callable[[Any], Any],
        run_resume_generation: Callable[..., Any],
        approval_received_at: Optional[float] = None,
        tenant_id: int,
    ) -> dict[str, Any]:
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

        requested_interrupt_id = interrupt_id.strip() if isinstance(interrupt_id, str) else None
        if not requested_interrupt_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="interrupt_id is required. Submit the canonical interrupt_id from the interrupt snapshot.",
            )

        ticket_service = InterruptTicketService(self.db)
        canonical_interrupt_id = requested_interrupt_id

        claimed_ticket = self._claim_pending_ticket_or_raise(
            ticket_service=ticket_service,
            interrupt_id=canonical_interrupt_id,
            task_id=task_id,
            user_id=user_id,
        )

        if claimed_ticket.interrupt_type == "clarify_request":
            action = response_payload.get("action")
            answers = response_payload.get("answers")
            has_valid_answer = isinstance(answers, dict) and any(
                str(key).strip() and str(value).strip()
                for key, value in answers.items()
            )
            if action != "answer" or not has_valid_answer:
                # Validation failure occurs after claim; revert claim so retries
                # do not get stranded in RESUMING for invalid client payloads.
                try:
                    ticket_service.mark_pending(
                        interrupt_id=canonical_interrupt_id,
                        task_id=task_id,
                    )
                except Exception:
                    logger.warning(
                        "Failed to revert claimed clarify interrupt after validation error "
                        "(task=%s, interrupt_id=%s)",
                        task_id,
                        canonical_interrupt_id,
                        exc_info=True,
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "clarify_request interrupts require action='answer' "
                        "with at least one non-empty answer."
                    ),
                )

        checkpoint_id = claimed_ticket.checkpoint_id
        resolved_graph_name = claimed_ticket.graph_name
        if isinstance(graph_name, str) and graph_name.strip():
            requested_graph_name = graph_name.strip()
            if requested_graph_name != resolved_graph_name:
                logger.warning(
                    "Ignoring client graph_name mismatch during resume (task=%s, interrupt_id=%s, requested=%s, canonical=%s)",
                    task_id,
                    canonical_interrupt_id,
                    requested_graph_name,
                    resolved_graph_name,
                )
        payload_data = (
            claimed_ticket.payload_snapshot
            if isinstance(claimed_ticket.payload_snapshot, dict)
            else {}
        )
        # Root invariant: checkpoint_id must come from canonical LangGraph interrupt snapshot,
        # not from payload metadata like run_id and not from stale ticket cache.
        try:
            pending_interrupt = await interrupt_state_service.get_interrupt_state_service().get_pending_interrupt(
                task_id=task_id,
                graph_name=resolved_graph_name,
                thread_id=getattr(claimed_ticket, "thread_id", None),
                graph_thread_id=runtime_context.graph_thread_id,
            )
        except Exception:
            pending_interrupt = None
            logger.warning(
                "Failed to hydrate pending interrupt snapshot (task=%s, interrupt_id=%s)",
                task_id,
                canonical_interrupt_id,
                exc_info=True,
            )

        if isinstance(pending_interrupt, dict):
            snapshot_interrupt_id = pending_interrupt.get("interrupt_id")
            if (
                isinstance(snapshot_interrupt_id, str)
                and snapshot_interrupt_id.strip() == canonical_interrupt_id
            ):
                snapshot_checkpoint_id = pending_interrupt.get("checkpoint_id")
                normalized_snapshot_checkpoint: Optional[str] = None
                if isinstance(snapshot_checkpoint_id, (int, str)):
                    normalized_snapshot_checkpoint = str(snapshot_checkpoint_id).strip() or None

                if normalized_snapshot_checkpoint != checkpoint_id:
                    checkpoint_id = normalized_snapshot_checkpoint
                    try:
                        claimed_ticket.checkpoint_id = checkpoint_id
                        self.db.commit()
                    except Exception:
                        self.db.rollback()
                        logger.warning(
                            "Failed to persist reconciled checkpoint_id for interrupt ticket "
                            "(task=%s, interrupt_id=%s, checkpoint_id=%s)",
                            task_id,
                            canonical_interrupt_id,
                            checkpoint_id,
                            exc_info=True,
                        )
            elif isinstance(snapshot_interrupt_id, str) and snapshot_interrupt_id.strip():
                logger.warning(
                    "Pending interrupt identity mismatch while resuming (task=%s, claimed=%s, snapshot=%s)",
                    task_id,
                    canonical_interrupt_id,
                    snapshot_interrupt_id.strip(),
                )
        payload_turn_id = payload_data.get("turn_id") if isinstance(payload_data.get("turn_id"), str) else None
        payload_turn_sequence = (
            payload_data.get("turn_sequence")
            if isinstance(payload_data.get("turn_sequence"), int)
            else None
        )
        payload_reserved_message_id = (
            payload_data.get("reserved_message_id")
            if isinstance(payload_data.get("reserved_message_id"), int)
            else None
        )
        payload_conversation_id = ""
        for conv_key in ("conversation_id", "conversationId"):
            conv_value = payload_data.get(conv_key)
            if isinstance(conv_value, str) and conv_value.strip():
                payload_conversation_id = conv_value.strip()
                break

        workflow_service = TurnWorkflowService(self.db)
        normalized_checkpoint = str(checkpoint_id) if checkpoint_id is not None else None
        if normalized_checkpoint:
            resume_key = normalized_checkpoint
        else:
            resume_key = canonical_interrupt_id

        effective_reserved_message_id = payload_reserved_message_id

        workflow = workflow_service.try_begin_resume(
            task_id=task_id,
            resume_key=resume_key,
            checkpoint_id=normalized_checkpoint,
            graph_name=resolved_graph_name,
            reserved_message_id=effective_reserved_message_id,
            metadata={
                "resume_source": "tasks_router",
                "interrupt_id": canonical_interrupt_id,
            },
        )
        if workflow is None:
            fallback_turn_id = payload_turn_id or (
                f"task-{task_id}-checkpoint-{normalized_checkpoint}"
                if normalized_checkpoint
                else f"task-{task_id}-resume-{resume_key}"
            )
            workflow_service.ensure_waiting_workflow(
                task_id=task_id,
                conversation_id=payload_conversation_id,
                turn_id=fallback_turn_id,
                turn_sequence=payload_turn_sequence,
                graph_name=resolved_graph_name,
                checkpoint_id=normalized_checkpoint,
                interrupt_type=claimed_ticket.interrupt_type,
                reserved_message_id=effective_reserved_message_id,
                resume_key=resume_key,
                metadata={"backfilled": True},
            )
            workflow = workflow_service.try_begin_resume(
                task_id=task_id,
                resume_key=resume_key,
                checkpoint_id=normalized_checkpoint,
                graph_name=resolved_graph_name,
                reserved_message_id=effective_reserved_message_id,
                metadata={
                    "resume_source": "tasks_router_backfill",
                    "interrupt_id": canonical_interrupt_id,
                },
            )

        if workflow is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Resume already in flight for this interrupt.",
            )

        try:
            create_task_fn(
                run_resume_generation(
                    task_id=task_id,
                    user_id=user_id,
                    graph_thread_id=runtime_context.graph_thread_id,
                    tenant_id=runtime_context.tenant_id,
                    runtime_placement_mode=runtime_context.runtime_placement_mode,
                    workspace_id=runtime_context.workspace_id,
                    actor_type=runtime_context.actor_type.value,
                    actor_id=runtime_context.actor_id,
                    runner_id=runtime_context.runner_id,
                    execution_site_id=runtime_context.execution_site_id,
                    response=response_payload,
                    graph_name=resolved_graph_name,
                    checkpoint_id=checkpoint_id,
                    reserved_message_id=(
                        effective_reserved_message_id
                        if isinstance(effective_reserved_message_id, int)
                        else None
                    ),
                    resume_key=resume_key,
                    workflow_id=workflow.id,
                    interrupt_id=canonical_interrupt_id,
                    approval_received_at=approval_received_at,
                )
            )
        except Exception:
            self._requeue_after_enqueue_failure(
                workflow_service=workflow_service,
                workflow=workflow,
                ticket_service=ticket_service,
                interrupt_id=canonical_interrupt_id,
                task_id=task_id,
                checkpoint_id=normalized_checkpoint,
                interrupt_type=claimed_ticket.interrupt_type,
                graph_name=resolved_graph_name,
                reserved_message_id=effective_reserved_message_id,
                resume_key=resume_key,
            )
            raise

        return {
            "status": "resumed",
            "task_id": task_id,
            "interrupt_id": canonical_interrupt_id,
        }
