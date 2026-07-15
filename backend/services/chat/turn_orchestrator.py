"""High-level reservation orchestration for chat turn message rows.

This service composes ChatMessage CRUD, prompt-authoritative tail lookup, and
turn-number allocation. The final ChatMessage row commit remains here; the
TurnNumberService counter allocation still commits internally as before.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from .message_service import ChatMessageService
from .conversation_history_reader import ConversationHistoryReader
from .turn_number_service import get_turn_number_service


class ChatTurnOrchestrator:
    """Reserve ChatMessage rows for single-message and user/assistant turns."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._chat = ChatMessageService(db)
        self._reader = ConversationHistoryReader(db)
        self._turn = get_turn_number_service()

    def reserve_user_message(
        self,
        *,
        task_id: int,
        conversation_id: str,
        user_message: str,
    ) -> tuple[int, int]:
        """Reserve and persist a user message as a single turn node."""
        parent_for_user = self._resolve_parent_for_new_turn(task_id, conversation_id)
        turn_number = self._turn.get_next_turn_number_in_session(
            self.db,
            task_id=task_id,
            conversation_id=conversation_id,
        )
        user_msg = self._chat.reserve_message(
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=parent_for_user,
            message_type="user",
            turn_number=turn_number,
        )
        self._chat.update_message(user_msg.id, user_message, token_count=0)
        self.db.commit()
        return user_msg.id, turn_number

    def reserve_chat_turn_pair(
        self,
        *,
        task_id: int,
        conversation_id: str,
        user_message: str,
    ) -> tuple[int, int, str, int]:
        """Reserve user+assistant rows and return identifiers for one chat turn."""
        parent_for_user = self._resolve_parent_for_new_turn(task_id, conversation_id)
        turn_number = self._turn.get_next_turn_number_in_session(
            self.db,
            task_id=task_id,
            conversation_id=conversation_id,
        )

        user_msg = self._chat.reserve_message(
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=parent_for_user,
            message_type="user",
            turn_number=turn_number,
        )
        self._chat.update_message(user_msg.id, user_message, token_count=0)
        assistant_msg = self._chat.reserve_message(
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=user_msg.id,
            message_type="assistant",
            turn_number=turn_number,
        )
        self.db.commit()
        turn_id = f"task-{task_id}-turn-{turn_number}"
        return user_msg.id, assistant_msg.id, turn_id, turn_number

    def _resolve_parent_for_new_turn(self, task_id: int, conversation_id: str) -> int:
        history = self._reader.get_conversation_history(
            task_id=task_id,
            conversation_id=conversation_id,
            limit=None,
        )
        parent_for_user = history[-1].id if history else None
        if parent_for_user is not None:
            return parent_for_user

        root = self._chat.get_or_create_root_message(
            task_id=task_id,
            conversation_id=conversation_id,
            message_type="SYSTEM",
            message="",
        )
        return root.id
