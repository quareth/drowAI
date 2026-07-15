"""ChatMessage row CRUD; non-CRUD chat concerns live in dedicated modules.

Optional tool calls passed through ``update_message`` use ``tool_call_repository``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.chat import ChatMessage
from .observation_sections import merge_observation_tokens
from .tool_call_repository import ToolCallRepository

ToolCallInfo = Dict[str, Any]
_UNSET = object()


class ChatMessageService:
    """Reserve and update ChatMessage rows."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def reserve_message(
        self,
        task_id: int,
        conversation_id: str,
        parent_message_id: Optional[int],
        message_type: str,
        *,
        turn_number: Optional[int] = None,
    ) -> ChatMessage:
        """Reserve a message row before streaming (placeholder)."""
        tenant_id = self._resolve_task_tenant_id(task_id)
        msg = ChatMessage(
            task_id=task_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            latest_child_message_id=None,
            message_type=message_type,
            message="",  # placeholder
            token_count=0,
            turn_number=turn_number,
        )
        self.db.add(msg)
        self.db.flush()
        if parent_message_id:
            parent = self.db.get(ChatMessage, parent_message_id)
            if parent:
                parent.latest_child_message_id = msg.id
        self.db.flush()
        return msg

    def update_message(
        self,
        message_id: int,
        message_text: str,
        reasoning_tokens: Optional[str] | object = _UNSET,
        observation_tokens: Optional[str] | object = _UNSET,
        tool_calls: Optional[List[ToolCallInfo]] | object = _UNSET,
        citations: Optional[Dict[int, Any]] | object = _UNSET,
        error: Optional[str] | object = _UNSET,
        token_count: int = 0,
    ) -> ChatMessage:
        """Update a reserved message with final content and optional tool calls."""
        msg = self.db.get(ChatMessage, message_id)
        if not msg:
            raise ValueError(f"ChatMessage id={message_id} not found")
        msg.message = message_text
        if reasoning_tokens is not _UNSET:
            msg.reasoning_tokens = reasoning_tokens
        if observation_tokens is not _UNSET:
            msg.observation_tokens = merge_observation_tokens(
                msg.observation_tokens,
                cast(Optional[str], observation_tokens),
            )
        if citations is not _UNSET:
            msg.citations = citations
        if error is not _UNSET:
            msg.error = error
        msg.token_count = token_count
        self.db.flush()
        if tool_calls is not _UNSET and tool_calls:
            ToolCallRepository(self.db).create_tool_calls(
                message_id,
                cast(List[ToolCallInfo], tool_calls),
            )
        self.db.refresh(msg)
        return msg

    def get_or_create_root_message(
        self,
        task_id: int,
        conversation_id: str,
        message_type: str = "SYSTEM",
        message: str = "",
    ) -> ChatMessage:
        """Ensure a root message exists for the conversation; create if missing."""
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.parent_message_id.is_(None),
            )
            .limit(1)
        )
        existing = self.db.execute(stmt).scalar_one_or_none()
        if existing:
            return existing
        return self.reserve_message(task_id, conversation_id, None, message_type)

    def get_turn_number(self, message_id: int) -> Optional[int]:
        """Return turn_number for a ChatMessage id (or None if missing)."""
        msg = self.db.get(ChatMessage, message_id)
        if not msg:
            return None
        return getattr(msg, "turn_number", None)

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        """Resolve tenant ownership from the canonical task row."""

        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(
                f"Cannot resolve tenant for chat message write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)
