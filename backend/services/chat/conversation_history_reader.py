"""Prompt-authoritative reader for conversation history and neutral projection.

This module owns tree traversal over ChatMessage rows, prompt-facing cursor
pagination semantics, summary marker handling, and conversion into the shared
role/content history shape consumed by provider adapters. UI transcript
pagination remains in
``backend.services.chat.transcript_query_service``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage

logger = logging.getLogger("backend.services.conversation_history_reader")

SYSTEM_SUMMARY_MESSAGE_TYPE = "SYSTEM_SUMMARY"
_COMPRESSION_EPOCH_METADATA_KEY = "context_compression"
_USER_MESSAGE_TYPES = ("user", "USER", "user_input", "user_message")
_ASSISTANT_MESSAGE_TYPES = ("assistant", "ASSISTANT", "assistant_message")


@dataclass(frozen=True, slots=True)
class AlignedConversationHistory:
    """Prompt messages and backend source IDs aligned by list position."""

    messages: tuple[Dict[str, Any], ...]
    source_message_ids: tuple[int, ...]


def _min_dt() -> datetime:
    """Min datetime for ordering (None-safe sort)."""
    return datetime.min.replace(tzinfo=timezone.utc)


class ConversationHistoryReader:
    """Read-only conversation history loader and provider-neutral projector."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_conversation_history(
        self,
        task_id: int,
        conversation_id: str,
        limit: Optional[int] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        include_summary_markers: bool = False,
    ) -> List[ChatMessage]:
        """Load messages for a conversation (tree traversal: roots then all children by created_at).

        Traverses all children of each node (ordered by created_at), not only latest_child_message_id,
        so sibling branches (e.g. regenerated branches) remain discoverable in history.

        Pagination:
        - after: message id cursor; return messages that come after this id (next page).
        - before: message id cursor; return messages that come before this id (previous page).
        - When neither is set, returns the most recent `limit` messages (oldest of that window first).
        """
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .order_by(ChatMessage.created_at.asc())
        )
        all_msgs = list(self.db.execute(stmt).scalars().unique().all())
        if not all_msgs:
            return []
        by_id = {m.id: m for m in all_msgs}
        roots = [m for m in all_msgs if m.parent_message_id is None]
        roots.sort(key=lambda m: m.created_at or _min_dt())
        ordered: List[ChatMessage] = []
        for r in roots:
            self._append_subtree(r, by_id, ordered)
        ids_to_idx = {m.id: i for i, m in enumerate(ordered)}
        if after is not None:
            idx = ids_to_idx.get(after)
            if idx is not None:
                ordered = ordered[idx + 1 :]
            # else: cursor not found, return from start
        elif before is not None:
            idx = ids_to_idx.get(before)
            if idx is not None:
                ordered = ordered[:idx]
        if not include_summary_markers:
            ordered = [
                msg
                for msg in ordered
                if (getattr(msg, "message_type", None) or "").strip() != SYSTEM_SUMMARY_MESSAGE_TYPE
            ]
        if limit is not None and len(ordered) > limit:
            if before is not None:
                ordered = ordered[-limit:]
            elif after is not None:
                ordered = ordered[:limit]
            else:
                ordered = ordered[-limit:]
        return ordered

    def convert_chat_messages_to_openai(
        self,
        chat_messages: List[ChatMessage],
    ) -> List[Dict[str, Any]]:
        """Convert ChatMessage rows into shared role/content history messages."""
        messages: List[Dict[str, Any]] = []

        for msg in chat_messages:
            mt = (getattr(msg, "message_type", None) or "").strip()
            content = (getattr(msg, "message", None) or "").strip()

            if mt in _USER_MESSAGE_TYPES:
                messages.append({"role": "user", "content": content or " "})
                continue
            if mt in _ASSISTANT_MESSAGE_TYPES:
                out: Dict[str, Any] = {"role": "assistant", "content": content or " "}
                tool_calls_list = getattr(msg, "tool_calls", None)
                if tool_calls_list:
                    openai_tool_calls = []
                    for tc in tool_calls_list:
                        tc_id = getattr(tc, "tool_call_id", None) or getattr(tc, "id")
                        name = getattr(tc, "tool_name", None) or ""
                        args = getattr(tc, "tool_arguments", None) or {}
                        openai_tool_calls.append(
                            {
                                "id": str(tc_id),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args) if isinstance(args, dict) else str(args),
                                },
                            }
                        )
                    if openai_tool_calls:
                        out["tool_calls"] = openai_tool_calls
                messages.append(out)
                continue
            # Skip SYSTEM and other types for prompt history.
        return messages

    def build_openai_conversation_history(
        self,
        *,
        task_id: int,
        conversation_id: Optional[str],
        limit: Optional[int] = None,
        exclude_message_ids: Optional[set[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Load persisted conversation and return shared role/content history."""
        return list(
            self.build_aligned_openai_conversation_history(
                task_id=task_id,
                conversation_id=conversation_id,
                limit=limit,
                exclude_message_ids=exclude_message_ids,
            ).messages
        )

    def build_aligned_openai_conversation_history(
        self,
        *,
        task_id: int,
        conversation_id: Optional[str],
        limit: Optional[int] = None,
        exclude_message_ids: Optional[set[int]] = None,
    ) -> AlignedConversationHistory:
        """Load prompt history and its source IDs in one canonical read."""
        if not conversation_id:
            return AlignedConversationHistory(messages=(), source_message_ids=())
        chat_messages = self.get_conversation_history(
            task_id=task_id,
            conversation_id=conversation_id,
            limit=limit,
            include_summary_markers=True,
        )
        cutoff_window = self._latest_summary_cutoff_window(
            chat_messages,
            task_id=task_id,
            conversation_id=conversation_id,
        )
        if cutoff_window is not None:
            summary_message, retained_messages = cutoff_window
            if exclude_message_ids:
                retained_messages = [
                    message
                    for message in retained_messages
                    if message.id not in exclude_message_ids
                ]
            return self._prepend_summary(summary_message, retained_messages)

        raw_messages = [
            message
            for message in chat_messages
            if (getattr(message, "message_type", None) or "").strip()
            != SYSTEM_SUMMARY_MESSAGE_TYPE
        ]
        if exclude_message_ids:
            raw_messages = [
                message
                for message in raw_messages
                if message.id not in exclude_message_ids
            ]
        return self._convert_chat_messages_to_aligned_history(raw_messages)

    def _latest_summary_cutoff_window(
        self,
        chat_messages: List[ChatMessage],
        *,
        task_id: int,
        conversation_id: str,
    ) -> Optional[tuple[ChatMessage, List[ChatMessage]]]:
        """Resolve the latest summary only when its cutoff is valid."""
        summaries = [
            message
            for message in chat_messages
            if (getattr(message, "message_type", None) or "").strip()
            == SYSTEM_SUMMARY_MESSAGE_TYPE
        ]
        summaries.sort(
            key=lambda message: (
                getattr(message, "created_at", None) is None,
                getattr(message, "created_at", None) or _min_dt(),
                int(message.id),
            ),
            reverse=True,
        )
        raw_messages = [
            message
            for message in chat_messages
            if (getattr(message, "message_type", None) or "").strip()
            != SYSTEM_SUMMARY_MESSAGE_TYPE
        ]
        raw_indexes = {message.id: index for index, message in enumerate(raw_messages)}

        if not summaries:
            return None
        summary = summaries[0]
        if not self._belongs_to_conversation(
            summary,
            task_id=task_id,
            conversation_id=conversation_id,
        ):
            return None
        through_message_id = self._summary_cutoff_id(summary)
        cutoff_index = raw_indexes.get(through_message_id)
        if cutoff_index is None:
            return None
        cutoff_message = raw_messages[cutoff_index]
        if not self._belongs_to_conversation(
            cutoff_message,
            task_id=task_id,
            conversation_id=conversation_id,
        ):
            return None
        return summary, raw_messages[cutoff_index + 1 :]

    @staticmethod
    def _summary_cutoff_id(summary: ChatMessage) -> Optional[int]:
        """Read a positive summarized-through ID from snapshot metadata."""
        citations = getattr(summary, "citations", None)
        if not isinstance(citations, dict):
            return None
        payload = citations.get(_COMPRESSION_EPOCH_METADATA_KEY)
        if not isinstance(payload, dict):
            return None
        through_message_id = payload.get("through_message_id")
        if (
            isinstance(through_message_id, bool)
            or not isinstance(through_message_id, int)
            or through_message_id <= 0
        ):
            return None
        return through_message_id

    @staticmethod
    def _belongs_to_conversation(
        message: ChatMessage,
        *,
        task_id: int,
        conversation_id: str,
    ) -> bool:
        """Confirm a summary or cutoff row belongs to the requested scope."""
        return (
            getattr(message, "task_id", None) == task_id
            and getattr(message, "conversation_id", None) == conversation_id
        )

    def _prepend_summary(
        self,
        summary_message: ChatMessage,
        raw_messages: List[ChatMessage],
    ) -> AlignedConversationHistory:
        """Project one summary followed by prompt-visible canonical raw rows."""
        post_summary_history = self._convert_chat_messages_to_aligned_history(raw_messages)
        return AlignedConversationHistory(
            messages=(
                {
                    "role": "system",
                    "content": (getattr(summary_message, "message", None) or "").strip()
                    or " ",
                },
                *post_summary_history.messages,
            ),
            source_message_ids=(
                int(summary_message.id),
                *post_summary_history.source_message_ids,
            ),
        )

    def _convert_chat_messages_to_aligned_history(
        self,
        chat_messages: List[ChatMessage],
    ) -> AlignedConversationHistory:
        """Project rows and retain IDs only for prompt-visible messages."""
        messages: List[Dict[str, Any]] = []
        source_message_ids: List[int] = []
        for chat_message in chat_messages:
            projected = self.convert_chat_messages_to_openai([chat_message])
            if not projected:
                continue
            messages.extend(projected)
            source_message_ids.extend([int(chat_message.id)] * len(projected))
        return AlignedConversationHistory(
            messages=tuple(messages),
            source_message_ids=tuple(source_message_ids),
        )

    def _append_subtree(
        self,
        node: ChatMessage,
        by_id: Dict[int, ChatMessage],
        out: List[ChatMessage],
    ) -> None:
        """Append node and all its children (siblings ordered by created_at) for full-branch history."""
        out.append(node)
        children = [m for m in by_id.values() if m.parent_message_id == node.id]
        children.sort(key=lambda m: (m.created_at is None, m.created_at or _min_dt()))
        for ch in children:
            self._append_subtree(ch, by_id, out)
