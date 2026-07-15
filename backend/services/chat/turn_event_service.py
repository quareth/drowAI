"""Persist canonical per-turn detail events for transcript ordering.

This service owns write-path persistence for `chat_turn_events` rows generated
from assistant turn completion state (tool calls + observations). It supports
full replacement and merge/append semantics for resume flows where only the
new segment is available in-memory.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.chat import ChatTurnEvent

ToolCallInfo = Dict[str, Any]
ObservationInfo = Dict[str, Any]
ReasoningInfo = Dict[str, Any]
TurnEventInfo = Dict[str, Any]


class ChatTurnEventService:
    """Service for canonical turn-event write persistence."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_events_for_message(
        self,
        *,
        task_id: int,
        conversation_id: str,
        chat_message_id: int,
        turn_number: int,
        reasoning_sections: Optional[List[ReasoningInfo]] = None,
        tool_calls: Optional[List[ToolCallInfo]] = None,
        observation_sections: Optional[List[ObservationInfo]] = None,
    ) -> List[ChatTurnEvent]:
        """Replace canonical rows for one assistant message.

        Raises:
            ValueError: When phase-sequence invariants are invalid.
        """
        normalized_events = self._build_events(
            tool_calls, observation_sections, reasoning_sections,
        )
        self._validate_phase_sequence(normalized_events)

        self.db.execute(
            delete(ChatTurnEvent).where(ChatTurnEvent.chat_message_id == chat_message_id)
        )

        created_rows: List[ChatTurnEvent] = []
        for event in sorted(normalized_events, key=lambda item: item["phase_sequence"]):
            row = self._build_row(
                task_id=task_id,
                conversation_id=conversation_id,
                chat_message_id=chat_message_id,
                turn_number=turn_number,
                phase_sequence=int(event["phase_sequence"]),
                event=event,
            )
            self.db.add(row)
            created_rows.append(row)

        self.db.flush()
        return created_rows

    def merge_events_for_message(
        self,
        *,
        task_id: int,
        conversation_id: str,
        chat_message_id: int,
        turn_number: int,
        reasoning_sections: Optional[List[ReasoningInfo]] = None,
        tool_calls: Optional[List[ToolCallInfo]] = None,
        observation_sections: Optional[List[ObservationInfo]] = None,
    ) -> List[ChatTurnEvent]:
        """Append new canonical rows while preserving existing rows for a message.

        Intended for resume persistence where the in-memory state container only
        carries the latest segment and previously persisted rows must remain.
        """
        normalized_events = self._build_events(
            tool_calls, observation_sections, reasoning_sections,
        )
        self._validate_phase_sequence(normalized_events)
        if not normalized_events:
            return []

        existing_rows = self._load_rows_for_message(chat_message_id)
        existing_keys = {self._row_identity_key(row) for row in existing_rows}
        used_sequences = {
            int(getattr(row, "phase_sequence", 0) or 0)
            for row in existing_rows
            if getattr(row, "phase_sequence", None) is not None
        }

        has_existing = bool(existing_rows)
        next_phase_sequence = (max(used_sequences) + 1) if used_sequences else 0
        created_rows: List[ChatTurnEvent] = []
        for event in sorted(normalized_events, key=lambda item: item["phase_sequence"]):
            event_key = self._event_identity_key(event)
            if event_key in existing_keys:
                continue

            if has_existing:
                phase_sequence = next_phase_sequence
                next_phase_sequence += 1
            else:
                candidate = int(event["phase_sequence"])
                if candidate not in used_sequences:
                    phase_sequence = candidate
                else:
                    while next_phase_sequence in used_sequences:
                        next_phase_sequence += 1
                    phase_sequence = next_phase_sequence
                    next_phase_sequence += 1

            row = self._build_row(
                task_id=task_id,
                conversation_id=conversation_id,
                chat_message_id=chat_message_id,
                turn_number=turn_number,
                phase_sequence=phase_sequence,
                event=event,
            )
            self.db.add(row)
            created_rows.append(row)
            existing_keys.add(event_key)
            used_sequences.add(phase_sequence)

        self.db.flush()
        return created_rows

    def _load_rows_for_message(self, chat_message_id: int) -> List[ChatTurnEvent]:
        query = (
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id == chat_message_id)
            .order_by(ChatTurnEvent.phase_sequence.asc())
        )
        return list(self.db.execute(query).scalars().all())

    def _build_row(
        self,
        *,
        task_id: int,
        conversation_id: str,
        chat_message_id: int,
        turn_number: int,
        phase_sequence: int,
        event: TurnEventInfo,
    ) -> ChatTurnEvent:
        tenant_id = self._resolve_task_tenant_id(task_id)
        return ChatTurnEvent(
            task_id=task_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            chat_message_id=chat_message_id,
            turn_number=turn_number,
            phase_sequence=phase_sequence,
            kind=event["kind"],
            sub_turn_index=event.get("sub_turn_index"),
            tool_call_id=event.get("tool_call_id"),
            content=event.get("content"),
            event_metadata=event.get("event_metadata"),
        )

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(
                f"Cannot resolve tenant for chat turn event write without task ownership: task_id={task_id}"
            )
        return int(tenant_id)

    @staticmethod
    def _event_identity_key(event: TurnEventInfo) -> tuple[Any, ...]:
        """Build a dedup identity tuple for one canonical turn event.

        Reasoning rows are keyed in a reasoning-specific identity space that
        includes section metadata. This prevents collisions with observation
        rows and preserves distinct reasoning sections during idempotent merge
        writes even when text/sub_turn_index happen to match.
        """
        kind = str(event.get("kind") or "").strip().lower()
        if kind == "tool":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                return ("tool", str(tool_call_id))
            return (
                "tool",
                _coerce_optional_int(event.get("sub_turn_index")),
                _to_text(event.get("content")) or "",
            )
        if kind == "reasoning":
            metadata = event.get("event_metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            return (
                "reasoning",
                _coerce_optional_int(event.get("sub_turn_index")),
                _to_text(event.get("content")) or "",
                _to_text(metadata.get("section_name")) or "",
                _to_text(metadata.get("source")) or "",
            )
        return (
            "observation",
            _coerce_optional_int(event.get("sub_turn_index")),
            _to_text(event.get("content")) or "",
        )

    @classmethod
    def _row_identity_key(cls, row: ChatTurnEvent) -> tuple[Any, ...]:
        return cls._event_identity_key(
            {
                "kind": getattr(row, "kind", None),
                "sub_turn_index": getattr(row, "sub_turn_index", None),
                "tool_call_id": getattr(row, "tool_call_id", None),
                "content": getattr(row, "content", None),
                "event_metadata": getattr(row, "event_metadata", None),
            }
        )

    def _build_events(
        self,
        tool_calls: Optional[List[ToolCallInfo]],
        observation_sections: Optional[List[ObservationInfo]],
        reasoning_sections: Optional[List[ReasoningInfo]] = None,
    ) -> List[TurnEventInfo]:
        events: List[TurnEventInfo] = []
        for reasoning in reasoning_sections or []:
            event = self._event_from_reasoning(reasoning)
            if event is not None:
                events.append(event)
        for tool_call in tool_calls or []:
            event = self._event_from_tool_call(tool_call)
            if event is not None:
                events.append(event)
        for observation in observation_sections or []:
            event = self._event_from_observation(observation)
            if event is not None:
                events.append(event)
        return events

    def _event_from_tool_call(self, tool_call: ToolCallInfo) -> Optional[TurnEventInfo]:
        if not isinstance(tool_call, dict):
            return None
        phase_sequence = tool_call.get("phase_sequence")
        tool_call_id = tool_call.get("tool_call_id")
        metadata: Dict[str, Any] = {}
        for key in (
            "tool_name",
            "tool_id",
            "tool_arguments",
            "tool_batch_id",
            "tab_index",
            "reasoning_tokens",
            "generated_images",
            "tool_call_tokens",
            "turn_index",
        ):
            value = tool_call.get(key)
            if value is not None:
                metadata[key] = value
        return {
            "phase_sequence": phase_sequence,
            "kind": "tool",
            "sub_turn_index": _coerce_optional_int(tool_call.get("turn_index")),
            "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
            "content": _to_text(tool_call.get("tool_result")),
            "event_metadata": _stable_metadata(metadata),
        }

    def _event_from_observation(
        self,
        observation: ObservationInfo,
    ) -> Optional[TurnEventInfo]:
        if not isinstance(observation, dict):
            text = _to_text(observation)
            if text is None:
                return None
            return {
                "phase_sequence": None,
                "kind": "observation",
                "sub_turn_index": None,
                "tool_call_id": None,
                "content": text,
                "event_metadata": None,
            }

        return {
            "phase_sequence": observation.get("phase_sequence"),
            "kind": "observation",
            "sub_turn_index": _coerce_optional_int(observation.get("sub_turn_index")),
            "tool_call_id": None,
            "content": _to_text(observation.get("content")),
            "event_metadata": _stable_metadata({}),
        }

    def _event_from_reasoning(
        self,
        reasoning: ReasoningInfo,
    ) -> Optional[TurnEventInfo]:
        """Build a canonical turn event from a reasoning section dict."""
        if not isinstance(reasoning, dict):
            text = _to_text(reasoning)
            if text is None:
                return None
            return {
                "phase_sequence": None,
                "kind": "reasoning",
                "sub_turn_index": None,
                "tool_call_id": None,
                "content": text,
                "event_metadata": None,
            }

        metadata: Dict[str, Any] = {}
        for key in ("section_name", "reasoning_section_id", "source", "started_at", "ended_at"):
            value = reasoning.get(key)
            if value is not None:
                metadata[key] = value

        return {
            "phase_sequence": reasoning.get("phase_sequence"),
            "kind": "reasoning",
            "sub_turn_index": _coerce_optional_int(reasoning.get("sub_turn_index")),
            "tool_call_id": None,
            "content": _to_text(reasoning.get("content")),
            "event_metadata": _stable_metadata(metadata),
        }

    def _validate_phase_sequence(self, events: List[TurnEventInfo]) -> None:
        seen: set[int] = set()
        for event in events:
            phase_sequence = event.get("phase_sequence")
            if not isinstance(phase_sequence, int) or phase_sequence < 0:
                raise ValueError("chat_turn_events require non-negative integer phase_sequence")
            if phase_sequence in seen:
                raise ValueError(
                    f"duplicate phase_sequence {phase_sequence} for one chat_message_id"
                )
            seen.add(phase_sequence)


def _to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except Exception:
        return str(value)


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_metadata(metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not metadata:
        return None
    try:
        return json.loads(json.dumps(metadata, sort_keys=True, default=str))
    except Exception:
        return metadata
