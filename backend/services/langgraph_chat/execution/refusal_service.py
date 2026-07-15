"""Sanitize and persist provider refusals as non-error terminal chat outcomes."""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Callable, Dict, Optional

from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from backend.database import SessionLocal
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.chat.message_service import ChatMessageService
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    mark_interrupt_ticket_failed_best_effort,
)
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    mark_turn_workflow_failed_best_effort,
)
from backend.services.langgraph_chat.execution.error_service import (
    PublishBoundaryCompletionEvents,
    TurnExecutionErrorService,
)

logger = logging.getLogger(__name__)

_FIELD_LIMITS = {
    "provider": 80,
    "model": 160,
    "category": 80,
    "explanation": 2000,
    "response_id": 200,
}


def _sanitize_field(value: Any, *, limit: int) -> Optional[str]:
    """Return bounded single-line text without Unicode control characters."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    visible = "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(visible.split()).strip()
    if not normalized:
        return None
    return normalized[:limit].rstrip() or None


def _summary_category(category: Optional[str]) -> Optional[str]:
    """Return a safe category label suitable for drowAI-authored prose."""
    if not category:
        return None
    label = re.sub(r"[^A-Za-z0-9 _-]+", "", category)
    label = " ".join(label.replace("_", " ").replace("-", " ").split())
    return label.lower() or None


def sanitize_refusal_outcome(outcome: LLMRefusalOutcome) -> Dict[str, Any]:
    """Project a provider outcome onto the durable, safe public refusal shape."""
    provider = _sanitize_field(outcome.provider, limit=_FIELD_LIMITS["provider"]) or "Provider"
    model = _sanitize_field(outcome.model, limit=_FIELD_LIMITS["model"]) or "Unknown model"
    category = _sanitize_field(outcome.category, limit=_FIELD_LIMITS["category"])
    explanation = _sanitize_field(
        outcome.explanation,
        limit=_FIELD_LIMITS["explanation"],
    )
    response_id = _sanitize_field(
        outcome.response_id,
        limit=_FIELD_LIMITS["response_id"],
    )
    category_label = _summary_category(category)
    summary = (
        f"The provider declined this request under its {category_label} safety policy."
        if category_label
        else "The provider declined this request under its safety policy."
    )
    return {
        "provider": provider,
        "model": model,
        "category": category,
        "summary": summary,
        "explanation": explanation,
        "response_id": response_id,
        "partial": bool(
            isinstance(outcome.partial_content, str)
            and outcome.partial_content.strip()
        ),
    }


class TurnExecutionRefusalService:
    """Own provider-refusal extraction and terminal chat side effects."""

    def __init__(self, *, error_service: Optional[TurnExecutionErrorService] = None) -> None:
        self._error_service = error_service or TurnExecutionErrorService()

    def extract_refusal_outcome(
        self,
        exc: BaseException,
    ) -> Optional[LLMRefusalOutcome]:
        """Find a neutral refusal outcome anywhere in an exception chain."""
        for candidate in TurnExecutionErrorService.iter_exception_chain(exc):
            if isinstance(candidate, LLMRefusalError):
                return candidate.outcome
        return None

    @staticmethod
    def _persist_assistant_refusal(
        *,
        reserved_message_id: Optional[int],
        content: str,
    ) -> None:
        """Persist refusal content while explicitly clearing any stale row error."""
        if reserved_message_id is None:
            return
        db_session = SessionLocal()
        try:
            ChatMessageService(db_session).update_message(
                reserved_message_id,
                content,
                error=None,
            )
            db_session.commit()
        except Exception:
            db_session.rollback()
            logger.debug(
                "Failed to persist assistant refusal (reserved_message_id=%s)",
                reserved_message_id,
                exc_info=True,
            )
        finally:
            db_session.close()

    async def handle_terminal_turn_refusal(
        self,
        *,
        outcome: LLMRefusalOutcome,
        task_id: int,
        hub: Any,
        workflow_id: Optional[int],
        reserved_message_id: Optional[int],
        publish_boundary_completion_events: PublishBoundaryCompletionEvents,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        graph_name: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        mark_turn_workflow_failed: Optional[Callable[..., None]] = None,
        interrupt_id: Optional[str] = None,
        mark_interrupt_ticket_failed: Optional[Callable[..., None]] = None,
    ) -> None:
        """Persist and publish one refusal without generic error fields or retry UX."""
        resolved = self._error_service.resolve_failure_context(
            task_id=task_id,
            workflow_id=workflow_id,
            reserved_message_id=reserved_message_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
        )
        refusal = sanitize_refusal_outcome(outcome)
        partial_content = (
            outcome.partial_content
            if isinstance(outcome.partial_content, str)
            and outcome.partial_content.strip()
            else None
        )
        content = partial_content or str(refusal["summary"])
        workflow_metadata = {
            "outcome_type": "provider_refusal",
            "retryable": False,
            "active_retry": None,
            "retry_state": "declined",
            "refusal": refusal,
        }
        failed_fn = mark_turn_workflow_failed or mark_turn_workflow_failed_best_effort
        failed_fn(
            workflow_id=workflow_id,
            checkpoint_id=checkpoint_id or resolved.get("checkpoint_id"),
            graph_name=graph_name or resolved.get("graph_name"),
            metadata=workflow_metadata,
            replace_metadata=True,
        )
        if interrupt_id is not None:
            interrupt_failed_fn = (
                mark_interrupt_ticket_failed
                or mark_interrupt_ticket_failed_best_effort
            )
            interrupt_failed_fn(task_id=task_id, interrupt_id=interrupt_id)

        resolved_reserved_message_id = resolved.get("reserved_message_id")
        self._persist_assistant_refusal(
            reserved_message_id=(
                resolved_reserved_message_id
                if isinstance(resolved_reserved_message_id, int)
                else None
            ),
            content=content,
        )
        resolved_conversation_id = str(resolved.get("conversation_id") or "")
        boundary_metadata = attach_conversation_ids(
            {
                "role": "assistant",
                "status": "declined",
                "stop_reason": "refusal",
                "streaming": False,
                "outcome_type": "provider_refusal",
                "retryable": False,
                "refusal": refusal,
            },
            resolved_conversation_id,
        )
        resolved_turn_id = resolved.get("turn_id")
        resolved_turn_sequence = resolved.get("turn_sequence")
        if isinstance(resolved_turn_id, str):
            boundary_metadata["id"] = resolved_turn_id
            boundary_metadata["turn_id"] = resolved_turn_id
        if isinstance(resolved_turn_sequence, int):
            boundary_metadata["turn_sequence"] = resolved_turn_sequence

        if hub is None:
            return
        try:
            await publish_boundary_completion_events(
                task_id=task_id,
                hub=hub,
                content=content,
                conversation_id=resolved_conversation_id,
                turn_id=boundary_metadata.get("id"),
                turn_sequence=boundary_metadata.get("turn_sequence"),
                base_metadata=boundary_metadata,
            )
        except Exception:
            logger.debug(
                "Failed to publish terminal refusal boundary for task %s",
                task_id,
                exc_info=True,
            )


__all__ = [
    "TurnExecutionRefusalService",
    "sanitize_refusal_outcome",
]
