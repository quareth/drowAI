"""Build bounded task-local transcript packets for memo preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task

TranscriptItemSource = Literal["message", "detail_event"]

_EXCLUDED_MESSAGE_TYPES = {"system", "system_summary"}
_DEFAULT_MAX_MESSAGES = 80
_DEFAULT_MAX_CHARACTERS = 12_000
_DEFAULT_MAX_ITEM_CHARACTERS = 1_200


@dataclass(frozen=True)
class TranscriptContextItem:
    """One compact transcript item available to memo preparation."""

    ref: str
    source: TranscriptItemSource
    role: str
    text: str
    created_at: str | None
    turn_number: int | None
    phase_sequence: int | None = None
    detail_type: str | None = None


@dataclass(frozen=True)
class TranscriptContext:
    """Bounded transcript context for one tenant-owned engagement task."""

    task_id: int
    conversation_id: str | None
    items: tuple[TranscriptContextItem, ...]
    message_count: int
    detail_event_count: int
    total_characters: int
    truncated: bool
    max_messages: int
    max_characters: int


class TranscriptContextBuilder:
    """Read durable chat rows into a bounded memo transcript context."""

    def __init__(
        self,
        db: Session,
        *,
        max_messages: int = _DEFAULT_MAX_MESSAGES,
        max_characters: int = _DEFAULT_MAX_CHARACTERS,
        max_item_characters: int = _DEFAULT_MAX_ITEM_CHARACTERS,
    ) -> None:
        if max_messages <= 0:
            raise ValueError("max_messages must be greater than zero")
        if max_characters <= 0:
            raise ValueError("max_characters must be greater than zero")
        if max_item_characters <= 0:
            raise ValueError("max_item_characters must be greater than zero")
        self._db = db
        self._max_messages = int(max_messages)
        self._max_characters = int(max_characters)
        self._max_item_characters = int(max_item_characters)

    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> TranscriptContext:
        """Return bounded transcript items for the requested task scope."""

        conversation_id = self._select_latest_conversation_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if conversation_id is None:
            return self._empty_context(task_id=task_id)

        messages = self._select_messages(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            conversation_id=conversation_id,
        )
        if not messages:
            return self._empty_context(task_id=task_id, conversation_id=conversation_id)

        message_ids = [
            int(message.id) for message in messages if message.id is not None
        ]
        events_by_message = self._select_events_by_message(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            message_ids=message_ids,
        )
        raw_items = self._map_items(
            messages=messages, events_by_message=events_by_message
        )
        bounded_items, truncated = self._bound_items(raw_items)
        message_count = sum(1 for item in bounded_items if item.source == "message")
        detail_event_count = sum(
            1 for item in bounded_items if item.source == "detail_event"
        )
        return TranscriptContext(
            task_id=int(task_id),
            conversation_id=conversation_id,
            items=tuple(bounded_items),
            message_count=message_count,
            detail_event_count=detail_event_count,
            total_characters=sum(len(item.text) for item in bounded_items),
            truncated=truncated
            or len(message_ids) >= self._max_messages
            and self._has_older_messages(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                conversation_id=conversation_id,
                oldest_message=messages[0],
            ),
            max_messages=self._max_messages,
            max_characters=self._max_characters,
        )

    def _empty_context(
        self,
        *,
        task_id: int,
        conversation_id: str | None = None,
    ) -> TranscriptContext:
        return TranscriptContext(
            task_id=int(task_id),
            conversation_id=conversation_id,
            items=(),
            message_count=0,
            detail_event_count=0,
            total_characters=0,
            truncated=False,
            max_messages=self._max_messages,
            max_characters=self._max_characters,
        )

    def _select_latest_conversation_id(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> str | None:
        turn_key = self._message_turn_key()
        row = (
            self._scoped_message_query(
                ChatMessage.conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .filter(
                ChatMessage.conversation_id.isnot(None),
                func.length(func.trim(ChatMessage.conversation_id)) > 0,
            )
            .order_by(turn_key.desc(), ChatMessage.id.desc())
            .first()
        )
        if row is None:
            return None
        conversation_id = str(row[0] or "").strip()
        return conversation_id or None

    def _select_messages(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        conversation_id: str,
    ) -> list[ChatMessage]:
        lookback_limit = self._max_messages * 3
        turn_key = self._message_turn_key()
        rows = (
            self._scoped_message_query(
                ChatMessage,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .filter(ChatMessage.conversation_id == conversation_id)
            .order_by(turn_key.desc(), ChatMessage.id.desc())
            .limit(lookback_limit)
            .all()
        )
        messages = list(reversed(rows))
        branch_messages = self._latest_branch_messages(messages)
        if branch_messages:
            messages = branch_messages
        return messages[-self._max_messages :]

    def _select_events_by_message(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        message_ids: list[int],
    ) -> dict[int, list[ChatTurnEvent]]:
        if not message_ids:
            return {}
        rows = (
            self._scoped_event_query(
                ChatTurnEvent,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .filter(ChatTurnEvent.chat_message_id.in_(message_ids))
            .order_by(
                ChatTurnEvent.turn_number.asc(),
                ChatTurnEvent.phase_sequence.asc(),
                ChatTurnEvent.id.asc(),
            )
            .all()
        )
        events_by_message: dict[int, list[ChatTurnEvent]] = {}
        for event in rows:
            events_by_message.setdefault(int(event.chat_message_id), []).append(event)
        return events_by_message

    def _map_items(
        self,
        *,
        messages: list[ChatMessage],
        events_by_message: dict[int, list[ChatTurnEvent]],
    ) -> list[TranscriptContextItem]:
        items: list[TranscriptContextItem] = []
        for message in messages:
            message_id = int(message.id)
            message_type = _compact_label(message.message_type, fallback="message")
            turn_number = _int_or_none(message.turn_number) or message_id
            message_text = _compact_text(message.message, self._max_item_characters)
            if message_text:
                items.append(
                    TranscriptContextItem(
                        ref=f"chat_message:{message_id}",
                        source="message",
                        role=message_type,
                        text=message_text,
                        created_at=_datetime_to_json(message.created_at),
                        turn_number=turn_number,
                    )
                )
            for event in events_by_message.get(message_id, []):
                event_text = self._event_text(event)
                if not event_text:
                    continue
                items.append(
                    TranscriptContextItem(
                        ref=f"chat_turn_event:{int(event.id)}",
                        source="detail_event",
                        role=message_type,
                        text=event_text,
                        created_at=_datetime_to_json(event.created_at),
                        turn_number=_int_or_none(event.turn_number),
                        phase_sequence=_int_or_none(event.phase_sequence),
                        detail_type=_compact_label(event.kind, fallback="detail"),
                    )
                )
        return items

    def _event_text(self, event: ChatTurnEvent) -> str:
        content = _compact_text(event.content, self._max_item_characters)
        if content:
            return content
        label = _compact_label(event.kind, fallback="detail")
        parts = [f"{label} event"]
        tool_call_id = _compact_text(event.tool_call_id, 120)
        if tool_call_id:
            parts.append(f"tool_call_id={tool_call_id}")
        metadata_summary = _metadata_summary(event.event_metadata)
        if metadata_summary:
            parts.append(metadata_summary)
        return _compact_text("; ".join(parts), self._max_item_characters)

    def _bound_items(
        self,
        items: list[TranscriptContextItem],
    ) -> tuple[list[TranscriptContextItem], bool]:
        bounded: list[TranscriptContextItem] = []
        total = 0
        truncated = False
        for item in items:
            remaining = self._max_characters - total
            if remaining <= 0:
                truncated = True
                break
            text = item.text
            if len(text) > remaining:
                text = _compact_text(text, remaining)
                truncated = True
            if not text:
                truncated = True
                break
            bounded.append(
                TranscriptContextItem(
                    ref=item.ref,
                    source=item.source,
                    role=item.role,
                    text=text,
                    created_at=item.created_at,
                    turn_number=item.turn_number,
                    phase_sequence=item.phase_sequence,
                    detail_type=item.detail_type,
                )
            )
            total += len(text)
        if len(bounded) < len(items):
            truncated = True
        return bounded, truncated

    def _latest_branch_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        if not any(getattr(message, "parent_message_id", None) for message in messages):
            return []
        messages_by_id = {
            int(message.id): message for message in messages if message.id is not None
        }
        parent_ids = {
            int(message.parent_message_id)
            for message in messages
            if getattr(message, "parent_message_id", None) is not None
        }
        leaf_messages = [
            message for message in messages if int(message.id) not in parent_ids
        ]
        if not leaf_messages:
            return []
        latest_leaf = max(
            leaf_messages,
            key=lambda message: (_message_turn_value(message), int(message.id)),
        )
        lineage: list[ChatMessage] = []
        current: ChatMessage | None = latest_leaf
        visited: set[int] = set()
        while current is not None and int(current.id) not in visited:
            current_id = int(current.id)
            visited.add(current_id)
            lineage.append(current)
            parent_id = getattr(current, "parent_message_id", None)
            current = (
                messages_by_id.get(int(parent_id)) if parent_id is not None else None
            )
        if len(lineage) <= 1:
            return []
        return list(reversed(lineage))

    def _has_older_messages(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        conversation_id: str,
        oldest_message: ChatMessage,
    ) -> bool:
        oldest_turn = _message_turn_value(oldest_message)
        row = (
            self._scoped_message_query(
                ChatMessage.id,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            .filter(ChatMessage.conversation_id == conversation_id)
            .filter(
                (self._message_turn_key() < oldest_turn)
                | (
                    (self._message_turn_key() == oldest_turn)
                    & (ChatMessage.id < int(oldest_message.id))
                )
            )
            .first()
        )
        return row is not None

    def _scoped_message_query(
        self,
        *entities: Any,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Any:
        return (
            self._db.query(*entities)
            .select_from(ChatMessage)
            .join(Task, Task.id == ChatMessage.task_id)
            .filter(
                ChatMessage.tenant_id == int(tenant_id),
                ChatMessage.task_id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                ~func.lower(ChatMessage.message_type).in_(_EXCLUDED_MESSAGE_TYPES),
            )
        )

    def _scoped_event_query(
        self,
        *entities: Any,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Any:
        return (
            self._db.query(*entities)
            .select_from(ChatTurnEvent)
            .join(Task, Task.id == ChatTurnEvent.task_id)
            .join(ChatMessage, ChatMessage.id == ChatTurnEvent.chat_message_id)
            .filter(
                ChatTurnEvent.tenant_id == int(tenant_id),
                ChatTurnEvent.task_id == int(task_id),
                ChatMessage.tenant_id == int(tenant_id),
                ChatMessage.task_id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
        )

    @staticmethod
    def _message_turn_key() -> Any:
        return func.coalesce(ChatMessage.turn_number, ChatMessage.id)


def _message_turn_value(message: ChatMessage) -> int:
    return _int_or_none(message.turn_number) or int(message.id)


def _compact_label(value: Any, *, fallback: str) -> str:
    label = str(value or "").strip().lower()
    return label or fallback


def _compact_text(value: Any, max_characters: int) -> str:
    if value is None or max_characters <= 0:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= max_characters:
        return text
    if max_characters <= 1:
        return text[:max_characters]
    if max_characters <= 3:
        return text[:max_characters]
    return text[: max_characters - 3].rstrip() + "..."


def _metadata_summary(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    parts: list[str] = []
    for key in ("tool_name", "status", "event", "phase", "kind"):
        value = metadata.get(key)
        if isinstance(value, str | int | float | bool):
            compact_value = _compact_text(value, 80)
            if compact_value:
                parts.append(f"{key}={compact_value}")
    return "; ".join(parts)


def _datetime_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


__all__ = [
    "TranscriptContext",
    "TranscriptContextBuilder",
    "TranscriptContextItem",
    "TranscriptItemSource",
]
