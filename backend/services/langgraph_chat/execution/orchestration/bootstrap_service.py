"""Bootstrap helpers for turn identity and workflow initialization.

This module owns start-flow bootstrap concerns: conversation defaulting,
chat-turn reservation, and turn identity resolution.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

from agent.chat import ConversationManager
from backend.database import SessionLocal
from backend.services.chat.message_service import ChatMessageService
from backend.services.chat.turn_orchestrator import ChatTurnOrchestrator
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    format_graph_thread_id,
)
from backend.services.task.graph_thread_lookup import load_task_graph_thread_id

logger = logging.getLogger(__name__)


class TurnExecutionBootstrapService:
    """Service that resolves start-flow turn bootstrap values."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], object] = SessionLocal,
        chat_message_service_factory: Callable[[object], ChatMessageService] = ChatMessageService,
        chat_turn_orchestrator_factory: Callable[[object], ChatTurnOrchestrator] = ChatTurnOrchestrator,
        conversation_manager_factory: Callable[[int], ConversationManager] = ConversationManager,
    ) -> None:
        self._session_factory = session_factory
        self._chat_message_service_factory = chat_message_service_factory
        self._chat_turn_orchestrator_factory = chat_turn_orchestrator_factory
        self._conversation_manager_factory = conversation_manager_factory

    def reserve_start_turn_if_needed(
        self,
        *,
        task_id: int,
        conversation_id: Optional[str],
        message: str,
        anchor_sequence: Optional[int],
        turn_id: Optional[str],
        turn_number: Optional[int],
        reserved_message_id: Optional[int],
        reserve_chat_turn: Optional[Callable[..., tuple[int, int, str, int]]] = None,
    ) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str], Optional[int]]:
        """Reserve the chat turn pair when no assistant placeholder exists yet."""
        resolved_conversation_id = conversation_id
        resolved_reserved_message_id = reserved_message_id
        resolved_anchor_sequence = anchor_sequence
        resolved_turn_id = turn_id
        resolved_turn_number = turn_number

        if resolved_reserved_message_id is not None:
            return (
                resolved_conversation_id,
                resolved_reserved_message_id,
                resolved_anchor_sequence,
                resolved_turn_id,
                resolved_turn_number,
            )

        db_session = self._session_factory()
        try:
            if not resolved_conversation_id:
                resolved_conversation_id = self._conversation_manager_factory(task_id).ensure_default_conversation()
            if reserve_chat_turn is not None:
                user_message_id, assistant_message_id, generated_turn_id, generated_turn_number = reserve_chat_turn(
                    db_session,
                    task_id=task_id,
                    conversation_id=resolved_conversation_id,
                    user_message=message,
                )
            else:
                chat_turn_orchestrator = self._chat_turn_orchestrator_factory(db_session)
                user_message_id, assistant_message_id, generated_turn_id, generated_turn_number = (
                    chat_turn_orchestrator.reserve_chat_turn_pair(
                        task_id=task_id,
                        conversation_id=resolved_conversation_id,
                        user_message=message,
                    )
                )
            resolved_reserved_message_id = assistant_message_id
            if resolved_anchor_sequence is None:
                resolved_anchor_sequence = assistant_message_id
            if resolved_turn_id is None:
                resolved_turn_id = generated_turn_id
            if resolved_turn_number is None:
                resolved_turn_number = generated_turn_number
            logger.warning(
                "[CHAT] Reserved ChatMessage inside start_turn_generation "
                "(task=%s, user_message_id=%s, assistant_message_id=%s)",
                task_id,
                user_message_id,
                assistant_message_id,
            )
            return (
                resolved_conversation_id,
                resolved_reserved_message_id,
                resolved_anchor_sequence,
                resolved_turn_id,
                resolved_turn_number,
            )
        finally:
            try:
                db_session.close()
            except Exception:
                pass

    def resolve_start_turn_identity(
        self,
        *,
        task_id: int,
        conversation_id: Optional[str],
        anchor_sequence: Optional[int],
        turn_id: Optional[str],
        turn_number: Optional[int],
        reserved_message_id: Optional[int],
    ) -> Tuple[dict, Optional[int], Optional[str], Optional[int]]:
        """Resolve thread config, turn number, turn id, and turn sequence for start flow."""
        graph_thread_id = self._load_graph_thread_id(task_id=task_id)
        thread_config = {
            "configurable": {
                "thread_id": format_graph_thread_id(
                    graph_thread_id,
                    task_id=task_id,
                )
            }
        }
        thread_id = thread_config["configurable"]["thread_id"]
        resolved_turn_number = turn_number

        if resolved_turn_number is None and reserved_message_id is not None:
            db_lookup = self._session_factory()
            try:
                chat_svc = self._chat_message_service_factory(db_lookup)
                resolved_turn_number = chat_svc.get_turn_number(reserved_message_id)
            except Exception:
                logger.debug(
                    "Failed to resolve turn_number for message %s (task=%s)",
                    reserved_message_id,
                    task_id,
                    exc_info=True,
                )
            finally:
                try:
                    db_lookup.close()
                except Exception:
                    pass

        if resolved_turn_number is None:
            from backend.services.chat.turn_number_service import get_turn_number_service

            resolved_turn_number = get_turn_number_service().get_next_turn_number(
                task_id=task_id,
                conversation_id=conversation_id,
            )

        turn_sequence = resolved_turn_number if resolved_turn_number is not None else anchor_sequence
        resolved_turn_id = turn_id
        if resolved_turn_id is None:
            if resolved_turn_number is not None:
                resolved_turn_id = f"task-{task_id}-turn-{resolved_turn_number}"
            else:
                resolved_turn_id = thread_id

        return thread_config, resolved_turn_number, resolved_turn_id, turn_sequence

    def _load_graph_thread_id(self, *, task_id: int) -> str:
        """Load the immutable graph identity for a SaaS task."""
        db_lookup = self._session_factory()
        try:
            graph_thread_id = load_task_graph_thread_id(db_lookup, task_id=task_id)
        finally:
            try:
                db_lookup.close()
            except Exception:
                pass
        return graph_thread_id
