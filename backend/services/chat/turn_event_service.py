"""Persist canonical per-turn detail events for transcript ordering.

This service owns write-path persistence for `chat_turn_events` rows generated
from assistant turn completion state (tool calls + observations). It supports
full replacement and merge/append semantics for resume flows where only the
new segment is available in-memory.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.hitl import TurnWorkflow

ToolCallInfo = Dict[str, Any]
ObservationInfo = Dict[str, Any]
ReasoningInfo = Dict[str, Any]
TurnEventInfo = Dict[str, Any]
EVENT_ATTRIBUTION_KEYS = (
    "producer_type",
    "agent_run_id",
    "agent_id",
    "agent_kind",
    "agent_display_name",
    "agent_icon_key",
    "parent_turn_id",
    "parent_run_id",
    "parent_graph_thread_id",
    "graph_thread_id",
    "internal_only",
    "lifecycle_version",
)
_TERMINAL_LIFECYCLE_METADATA_KEYS = (
    "status",
    "process_status",
    "session_status",
    "interaction_boundary",
    "session_id",
    "close_reason",
    "lifecycle_event",
    "shell_lifecycle_event",
)
_TERMINAL_LIFECYCLE_COMPACT_KEYS = (
    "process_status",
    "session_status",
    "interaction_boundary",
    "session_id",
    "close_reason",
    "lifecycle_event",
    "error",
    "failure_category",
)


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

    def append_terminal_tool_lifecycle_event(
        self,
        *,
        task_id: int,
        turn_id: str,
        tool_call_id: str | None,
        tool_name: str,
        content: str,
        status: str,
        process_status: str,
        session_status: str,
        interaction_boundary: str,
        session_id: str | None,
        close_reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatTurnEvent | None:
        """Append one canonical terminal tool lifecycle row for a turn.

        Returns ``None`` when the turn cannot be correlated to its reserved
        assistant message; cleanup callers should still release runtime
        resources in that case.
        """
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return None
        workflow = self._load_workflow_for_turn(
            task_id=int(task_id),
            turn_id=normalized_turn_id,
        )
        if workflow is None or not isinstance(workflow.reserved_message_id, int):
            return None
        message = self.db.get(ChatMessage, int(workflow.reserved_message_id))
        if message is None or int(getattr(message, "task_id", 0) or 0) != int(task_id):
            return None

        normalized_tool_call_id = str(tool_call_id or "").strip() or None
        normalized_session_id = str(session_id or "").strip() or None
        normalized_close_reason = str(close_reason or "").strip() or "unknown"
        session_tool_row = self._find_existing_shell_session_tool_lifecycle_row(
            chat_message_id=int(message.id),
            session_id=normalized_session_id,
        )
        if session_tool_row is not None:
            session_tool_call_id = (
                str(getattr(session_tool_row, "tool_call_id", "") or "").strip() or None
            )
            normalized_tool_call_id = session_tool_call_id or normalized_tool_call_id
        existing = self._find_existing_terminal_lifecycle_row(
            chat_message_id=int(message.id),
            tool_call_id=normalized_tool_call_id,
            session_id=normalized_session_id,
            close_reason=normalized_close_reason,
        )
        if existing is not None:
            return existing
        existing_tool_row = session_tool_row or self._find_existing_tool_lifecycle_row(
            chat_message_id=int(message.id),
            tool_call_id=normalized_tool_call_id,
        )
        resolved_tool_name = self._row_tool_name(existing_tool_row) or (
            str(tool_name or "shell.session").strip() or "shell.session"
        )

        event_metadata: Dict[str, Any] = {
            "tool_name": resolved_tool_name,
            "status": status,
            "process_status": process_status,
            "session_status": session_status,
            "interaction_boundary": interaction_boundary,
            "session_id": normalized_session_id,
            "close_reason": normalized_close_reason,
            "lifecycle_event": "shell_session_terminal",
            "turn_id": normalized_turn_id,
            "id": normalized_turn_id,
            "conversation_id": message.conversation_id,
            "turn_sequence": workflow.turn_sequence,
            "output_persistence": "transient",
        }
        if metadata:
            event_metadata.update(metadata)
        if normalized_tool_call_id:
            event_metadata["tool_call_id"] = normalized_tool_call_id
        event_metadata["tool_name"] = resolved_tool_name
        if existing_tool_row is not None:
            existing_metadata = getattr(existing_tool_row, "event_metadata", None)
            existing_metadata = existing_metadata if isinstance(existing_metadata, dict) else {}
            existing_sequence = existing_metadata.get("sequence")
            if (
                isinstance(existing_sequence, int)
                and not isinstance(existing_sequence, bool)
                and existing_sequence >= 0
            ):
                event_metadata.setdefault("sequence", existing_sequence)
        stable_metadata = _stable_metadata(
            {key: value for key, value in event_metadata.items() if value is not None}
        )
        if existing_tool_row is not None:
            existing_metadata = getattr(existing_tool_row, "event_metadata", None)
            existing_metadata = (
                existing_metadata if isinstance(existing_metadata, dict) else {}
            )
            if existing_metadata.get("output_persistence") == "durable":
                terminal_metadata = (
                    stable_metadata if isinstance(stable_metadata, dict) else {}
                )
                merged_metadata = dict(existing_metadata)
                for key in _TERMINAL_LIFECYCLE_METADATA_KEYS:
                    if terminal_metadata.get(key) is not None:
                        merged_metadata[key] = terminal_metadata[key]

                existing_compact = existing_metadata.get("compact_tool_result")
                terminal_compact = terminal_metadata.get("compact_tool_result")
                if isinstance(existing_compact, dict):
                    merged_compact = dict(existing_compact)
                    lifecycle_compact = (
                        terminal_compact if isinstance(terminal_compact, dict) else {}
                    )
                    for key in _TERMINAL_LIFECYCLE_COMPACT_KEYS:
                        value = lifecycle_compact.get(key, terminal_metadata.get(key))
                        if value is not None:
                            merged_compact[key] = value
                    merged_metadata["compact_tool_result"] = merged_compact
                existing_tool_row.event_metadata = _stable_metadata(merged_metadata)
            else:
                existing_tool_row.content = content
                existing_tool_row.event_metadata = stable_metadata
            self.db.flush()
            return existing_tool_row

        next_phase_sequence = self._next_phase_sequence(int(message.id))
        row = self._build_row(
            task_id=int(task_id),
            conversation_id=str(message.conversation_id or ""),
            chat_message_id=int(message.id),
            turn_number=int(message.turn_number or workflow.turn_sequence or message.id),
            phase_sequence=next_phase_sequence,
            event={
                "kind": "tool",
                "sub_turn_index": None,
                "tool_call_id": normalized_tool_call_id,
                "content": content,
                "event_metadata": stable_metadata,
            },
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _load_workflow_for_turn(
        self,
        *,
        task_id: int,
        turn_id: str,
    ) -> TurnWorkflow | None:
        return self.db.execute(
            select(TurnWorkflow)
            .where(
                TurnWorkflow.task_id == int(task_id),
                TurnWorkflow.turn_id == turn_id,
            )
            .order_by(TurnWorkflow.updated_at.desc(), TurnWorkflow.id.desc())
        ).scalars().first()

    def _next_phase_sequence(self, chat_message_id: int) -> int:
        current = self.db.execute(
            select(func.max(ChatTurnEvent.phase_sequence)).where(
                ChatTurnEvent.chat_message_id == int(chat_message_id)
            )
        ).scalar_one_or_none()
        return 0 if current is None else int(current) + 1

    def _find_existing_terminal_lifecycle_row(
        self,
        *,
        chat_message_id: int,
        tool_call_id: str | None,
        session_id: str | None,
        close_reason: str,
    ) -> ChatTurnEvent | None:
        rows = list(
            self.db.execute(
                select(ChatTurnEvent).where(
                    ChatTurnEvent.chat_message_id == int(chat_message_id),
                    ChatTurnEvent.kind == "tool",
                )
            ).scalars().all()
        )
        for row in rows:
            metadata = getattr(row, "event_metadata", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            if metadata.get("lifecycle_event") != "shell_session_terminal":
                continue
            if str(metadata.get("close_reason") or "") != close_reason:
                continue
            row_tool_call_id = str(getattr(row, "tool_call_id", "") or "").strip() or None
            row_session_id = str(metadata.get("session_id") or "").strip() or None
            if row_tool_call_id == tool_call_id and row_session_id == session_id:
                return row
        return None

    def _find_existing_tool_lifecycle_row(
        self,
        *,
        chat_message_id: int,
        tool_call_id: str | None,
    ) -> ChatTurnEvent | None:
        if not tool_call_id:
            return None
        return self.db.execute(
            select(ChatTurnEvent)
            .where(
                ChatTurnEvent.chat_message_id == int(chat_message_id),
                ChatTurnEvent.kind == "tool",
                ChatTurnEvent.tool_call_id == tool_call_id,
            )
            .order_by(ChatTurnEvent.phase_sequence.asc(), ChatTurnEvent.id.asc())
            .limit(1)
        ).scalars().first()

    def _find_existing_shell_session_tool_lifecycle_row(
        self,
        *,
        chat_message_id: int,
        session_id: str | None,
    ) -> ChatTurnEvent | None:
        if not session_id:
            return None
        rows = list(
            self.db.execute(
                select(ChatTurnEvent)
                .where(
                    ChatTurnEvent.chat_message_id == int(chat_message_id),
                    ChatTurnEvent.kind == "tool",
                )
                .order_by(ChatTurnEvent.phase_sequence.asc(), ChatTurnEvent.id.asc())
            ).scalars().all()
        )
        fallback: ChatTurnEvent | None = None
        for row in rows:
            metadata = getattr(row, "event_metadata", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            row_session_id = str(metadata.get("session_id") or "").strip() or None
            if row_session_id != session_id:
                continue
            if fallback is None:
                fallback = row
            lifecycle_event = str(metadata.get("lifecycle_event") or "").strip()
            session_status = str(metadata.get("session_status") or "").strip().lower()
            interaction_boundary = (
                str(metadata.get("interaction_boundary") or "").strip().lower()
            )
            if (
                lifecycle_event != "shell_session_terminal"
                or session_status != "closed"
                or interaction_boundary != "terminal"
            ):
                return row
        return fallback

    @staticmethod
    def _row_tool_name(row: ChatTurnEvent | None) -> str | None:
        if row is None:
            return None
        metadata = getattr(row, "event_metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        for key in ("tool_name", "tool", "command"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        return None

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
            reasoning_section_id = _to_text(metadata.get("reasoning_section_id"))
            if reasoning_section_id:
                return ("reasoning", reasoning_section_id)
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
        metadata = _event_attribution(tool_call)
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
            "status",
            "process_status",
            "session_status",
            "interaction_boundary",
            "session_id",
            "output_persistence",
            "compact_tool_result",
            "sequence",
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

        metadata = _event_attribution(observation)
        stream_sequence = observation.get("sequence")
        if (
            isinstance(stream_sequence, int)
            and not isinstance(stream_sequence, bool)
            and stream_sequence >= 0
        ):
            metadata["sequence"] = stream_sequence
        return {
            "phase_sequence": observation.get("phase_sequence"),
            "kind": "observation",
            "sub_turn_index": _coerce_optional_int(observation.get("sub_turn_index")),
            "tool_call_id": None,
            "content": _to_text(observation.get("content")),
            "event_metadata": _stable_metadata(metadata),
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

        metadata = _event_attribution(reasoning)
        for key in (
            "section_name",
            "reasoning_section_id",
            "source",
            "started_at",
            "ended_at",
            "sequence",
        ):
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


def _event_attribution(event: Dict[str, Any]) -> Dict[str, Any]:
    """Copy only stable agent-run ownership fields into canonical metadata."""
    return {
        key: event[key]
        for key in EVENT_ATTRIBUTION_KEYS
        if event.get(key) is not None
    }
