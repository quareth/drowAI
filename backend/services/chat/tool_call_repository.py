"""Persistence repository for ToolCall rows attached to chat messages.

This module owns ToolCall upsert behavior, parent linkage, child traversal,
and payload normalization. ChatMessage row CRUD stays in
``backend.services.chat.message_service``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage, ToolCall

logger = logging.getLogger("backend.services.tool_call_repository")

ToolCallInfo = Dict[str, Any]


class ToolCallRepository:
    """Persist ToolCall rows with parent linkage and partial-update upsert."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_tool_calls(
        self,
        chat_message_id: int,
        tool_calls: List[ToolCallInfo],
        parent_tool_call_id: Optional[int] = None,
    ) -> List[ToolCall]:
        """Create or update ToolCall rows with optional parent references."""
        created: List[ToolCall] = []
        for idx, tc in enumerate(tool_calls):
            row = self._upsert_tool_call(
                chat_message_id=chat_message_id,
                tool_call=tc,
                fallback_index=idx,
                parent_tool_call_id=parent_tool_call_id,
            )
            created.append(row)
            # Recursively create children if present.
            children = tc.get("child_calls") or tc.get("children") or []
            if children:
                created.extend(
                    self.create_tool_calls(chat_message_id, children, parent_tool_call_id=row.id)
                )
        return created

    def _upsert_tool_call(
        self,
        *,
        chat_message_id: int,
        tool_call: ToolCallInfo,
        fallback_index: int,
        parent_tool_call_id: Optional[int],
    ) -> ToolCall:
        tool_call_id = tool_call.get("tool_call_id") or tool_call.get("id") or f"tc-{fallback_index}"
        parent_id = tool_call.get("parent_tool_call_id") or parent_tool_call_id
        tool_call_id_str = str(tool_call_id)
        normalized_arguments = self._normalize_json_field(tool_call.get("tool_arguments")) or {}
        normalized_result = self._normalize_text_field(tool_call.get("tool_result"))
        normalized_generated_images = self._normalize_json_field(tool_call.get("generated_images"))
        resolved_turn_index = self._resolve_turn_index(tool_call.get("turn_index"), fallback_index)
        tenant_id = self._resolve_message_tenant_id(chat_message_id)

        existing = self.db.execute(
            select(ToolCall).where(
                ToolCall.chat_message_id == chat_message_id,
                ToolCall.tool_call_id == tool_call_id_str,
            )
        ).scalar_one_or_none()

        if existing:
            if parent_id is not None:
                existing.parent_tool_call_id = parent_id
            tool_id = tool_call.get("tool_id")
            if tool_id is not None:
                existing.tool_id = tool_id
            tool_name = tool_call.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                existing.tool_name = tool_name
            if "tool_arguments" in tool_call:
                existing.tool_arguments = normalized_arguments
            if "tool_result" in tool_call:
                existing.tool_result = normalized_result
            # Keep ToolCall persistence backward-compatible while canonical turn-event rows
            # become ordering authority. turn_index remains best-effort provenance metadata.
            existing.turn_index = resolved_turn_index
            tab_index = tool_call.get("tab_index")
            if tab_index is not None:
                existing.tab_index = tab_index
            reasoning_tokens = tool_call.get("reasoning_tokens")
            if reasoning_tokens is not None:
                existing.reasoning_tokens = reasoning_tokens
            generated_images = tool_call.get("generated_images")
            if generated_images is not None:
                existing.generated_images = normalized_generated_images
            tool_call_tokens = tool_call.get("tool_call_tokens")
            if tool_call_tokens is not None:
                existing.tool_call_tokens = tool_call_tokens
            self.db.flush()
            return existing

        row = ToolCall(
            chat_message_id=chat_message_id,
            tenant_id=tenant_id,
            parent_tool_call_id=parent_id,
            tool_call_id=tool_call_id_str,
            tool_id=tool_call.get("tool_id"),
            tool_name=tool_call.get("tool_name", ""),
            tool_arguments=normalized_arguments,
            tool_result=normalized_result,
            turn_index=resolved_turn_index,
            tab_index=tool_call.get("tab_index"),
            reasoning_tokens=tool_call.get("reasoning_tokens"),
            generated_images=normalized_generated_images,
            tool_call_tokens=tool_call.get("tool_call_tokens", 0),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _resolve_message_tenant_id(self, chat_message_id: int) -> int:
        tenant_id = self.db.execute(
            select(ChatMessage.tenant_id).where(ChatMessage.id == chat_message_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(
                "Cannot resolve tenant for tool call write without chat_message ownership: "
                f"chat_message_id={chat_message_id}"
            )
        return int(tenant_id)

    @staticmethod
    def _resolve_turn_index(raw_turn_index: Any, fallback_index: int) -> int:
        """Return persisted turn_index as best-effort metadata."""
        if raw_turn_index is None:
            return fallback_index
        try:
            return int(raw_turn_index)
        except (TypeError, ValueError):
            return fallback_index

    def _normalize_json_field(self, value: Any) -> Optional[Dict[str, Any] | List[Any]]:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate or candidate.lower() == "null":
                return None
            try:
                parsed = json.loads(candidate)
            except Exception:
                return None
            if isinstance(parsed, (dict, list)):
                return parsed
        return None

    def _normalize_text_field(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value)
            except Exception:
                return str(value)
        return str(value)
