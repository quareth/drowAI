"""Live stream projection for tool executions stopped by chat cancellation.

This module bridges durable chat-stop provenance to the live stream. It emits
the terminal tool events the frontend already uses so cancellation during tool
execution is visible immediately without waiting for a refresh.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.graph.contracts.streaming_constants import STEP_TOOL_END, TOOL_PHASE_INDEX
from backend.models.provenance import ToolExecution
from backend.models.streaming import StreamEvent
from backend.services.langgraph_chat.runtime.tool_cancel_service import ToolCancelProjectionResult

logger = logging.getLogger(__name__)

_TOOL_STREAM_EVENT_TYPES = frozenset(
    {
        "tool_batch_start",
        "tool_start",
        "tool_end",
        "tool_batch_end",
    }
)


@dataclass(frozen=True)
class ToolCancelStreamProjectionResult:
    """Summary of cancellation events projected to the live stream."""

    tool_end_count: int
    tool_batch_end_count: int
    streaming_state_updated: bool


@dataclass(frozen=True)
class StreamToolIdentity:
    """Live-stream grouping identity for one tool call."""

    tool_call_id: str
    tool_batch_id: str | None = None
    tool_name: str | None = None
    conversation_id: str | None = None
    turn_sequence: int | None = None


@dataclass(frozen=True)
class StreamBatchIdentity:
    """Live-stream grouping identity for one tool batch."""

    tool_batch_id: str
    tool_calls: tuple[StreamToolIdentity, ...]
    conversation_id: str | None = None
    turn_sequence: int | None = None


@dataclass(frozen=True)
class StreamToolHistory:
    """Relevant prior stream state for one cancelled turn."""

    tools: dict[str, StreamToolIdentity]
    batches: dict[str, StreamBatchIdentity]
    terminal_tool_statuses: dict[str, str]
    terminal_batches: set[str]


class ChatToolCancelStreamProjectionService:
    """Publish terminal live-stream events for cancelled tool executions."""

    def __init__(self, db: Session) -> None:
        self._db = db

    async def publish_cancelled_turn(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str | None,
        tool_cancellation: ToolCancelProjectionResult,
    ) -> ToolCancelStreamProjectionResult:
        """Publish cancellation terminal events for a stopped turn."""
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return ToolCancelStreamProjectionResult(
                tool_end_count=0,
                tool_batch_end_count=0,
                streaming_state_updated=False,
            )

        rows = self._load_cancelled_rows(
            tenant_id=int(tenant_id),
            task_id=int(task_id),
            turn_id=normalized_turn_id,
            tool_cancellation=tool_cancellation,
        )
        history = self._load_stream_tool_history(
            tenant_id=int(tenant_id),
            task_id=int(task_id),
            turn_id=normalized_turn_id,
        )
        try:
            from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

            hub = get_in_memory_stream_hub()
            hub.set_streaming_state(int(task_id), False)
            streaming_state_updated = True
        except Exception:
            logger.exception(
                "Failed to mark chat stream inactive after cancellation task_id=%s turn_id=%s",
                task_id,
                normalized_turn_id,
            )
            hub = None
            streaming_state_updated = False

        if hub is None:
            return ToolCancelStreamProjectionResult(
                tool_end_count=0,
                tool_batch_end_count=0,
                streaming_state_updated=streaming_state_updated,
            )

        tool_end_count = 0
        batch_rows: dict[str, list[tuple[ToolExecution, StreamToolIdentity]]] = defaultdict(list)
        standalone_rows: list[tuple[ToolExecution, StreamToolIdentity | None]] = []
        for row in rows:
            tool_call_id = str(row.tool_call_id or "").strip()
            identity = history.tools.get(tool_call_id)
            if identity and identity.tool_batch_id:
                batch_rows[identity.tool_batch_id].append((row, identity))
            else:
                standalone_rows.append((row, identity))

        for row, identity in standalone_rows:
            tool_call_id = str(row.tool_call_id or "").strip()
            if tool_call_id and tool_call_id in history.terminal_tool_statuses:
                continue
            event = self._tool_end_event(
                row=row,
                fallback_turn_id=normalized_turn_id,
                identity=identity,
            )
            try:
                await hub.publish(int(task_id), event)
                tool_end_count += 1
            except Exception:
                logger.exception(
                    "Failed to publish cancelled tool_end task_id=%s turn_id=%s tool_call_id=%s",
                    task_id,
                    normalized_turn_id,
                    row.tool_call_id,
                )

        tool_batch_end_count = 0
        for batch_id, grouped_rows in batch_rows.items():
            for row, identity in grouped_rows:
                tool_call_id = str(row.tool_call_id or "").strip()
                if tool_call_id and tool_call_id in history.terminal_tool_statuses:
                    continue
                event = self._tool_end_event(
                    row=row,
                    fallback_turn_id=normalized_turn_id,
                    identity=identity,
                )
                try:
                    await hub.publish(int(task_id), event)
                    tool_end_count += 1
                except Exception:
                    logger.exception(
                        "Failed to publish cancelled batch tool_end task_id=%s turn_id=%s tool_call_id=%s",
                        task_id,
                        normalized_turn_id,
                        row.tool_call_id,
                    )

            if batch_id in history.terminal_batches:
                continue
            batch_identity = history.batches.get(batch_id)
            event = self._tool_batch_end_event(
                batch_id=batch_id,
                grouped_rows=grouped_rows,
                batch_identity=batch_identity,
                terminal_tool_statuses=history.terminal_tool_statuses,
                fallback_turn_id=normalized_turn_id,
            )
            try:
                await hub.publish(int(task_id), event)
                tool_batch_end_count += 1
            except Exception:
                logger.exception(
                    "Failed to publish cancelled tool_batch_end task_id=%s turn_id=%s tool_batch_id=%s",
                    task_id,
                    normalized_turn_id,
                    batch_id,
                )

        return ToolCancelStreamProjectionResult(
            tool_end_count=tool_end_count,
            tool_batch_end_count=tool_batch_end_count,
            streaming_state_updated=streaming_state_updated,
        )

    def _load_cancelled_rows(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str,
        tool_cancellation: ToolCancelProjectionResult,
    ) -> list[ToolExecution]:
        rows = list(
            self._db.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.tenant_id == tenant_id,
                    ToolExecution.task_id == task_id,
                    ToolExecution.turn_id == turn_id,
                )
                .order_by(ToolExecution.created_at.asc(), ToolExecution.id.asc())
            )
            .scalars()
            .all()
        )
        projected_ids = {str(value).strip() for value in tool_cancellation.execution_ids if str(value).strip()}
        projected_call_ids = {
            str(value).strip() for value in tool_cancellation.tool_call_ids if str(value).strip()
        }
        candidates: list[ToolExecution] = []
        for row in rows:
            row_id = str(row.id or "").strip()
            call_id = str(row.tool_call_id or "").strip()
            if projected_ids and row_id in projected_ids:
                candidates.append(row)
                continue
            if projected_call_ids and call_id in projected_call_ids:
                candidates.append(row)
                continue
            if not projected_ids and not projected_call_ids and self._is_cancelled_row(row):
                candidates.append(row)
        return candidates

    @staticmethod
    def _tool_end_event(
        *,
        row: ToolExecution,
        fallback_turn_id: str,
        identity: StreamToolIdentity | None = None,
    ) -> dict[str, Any]:
        metadata = row.execution_metadata if isinstance(row.execution_metadata, dict) else {}
        cancellation = metadata.get("cancellation") if isinstance(metadata.get("cancellation"), dict) else {}
        turn_id = str(row.turn_id or fallback_turn_id)
        tool_name = str(identity.tool_name if identity and identity.tool_name else row.tool_name or "unknown")
        conversation_id = (
            identity.conversation_id
            if identity and identity.conversation_id
            else row.conversation_id
        )
        turn_sequence = (
            identity.turn_sequence
            if identity and identity.turn_sequence is not None
            else row.turn_sequence
        )
        duration = None
        if row.duration_ms is not None:
            duration = max(float(row.duration_ms) / 1000.0, 0.0)
        event_metadata: dict[str, Any] = {
            "subtype": "tool_end",
            "tool": tool_name,
            "tool_name": tool_name,
            "tool_call_id": row.tool_call_id,
            "status": "cancelled",
            "duration": duration if duration is not None else 0,
            "exit_code": row.exit_code,
            "summary": {},
            "compact_tool_result": {
                "schema_version": "2.0",
                "tool": tool_name,
                "status": "cancelled",
                "exit_code": row.exit_code,
                "summary": {},
                "error": "user_cancelled",
            },
            "error": "user_cancelled",
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "id": turn_id,
            "turn_id": turn_id,
            "turn_sequence": turn_sequence,
            "streaming": False,
            "is_streaming": False,
            "in_progress": False,
            "source": "chat_stop",
            "timestamp": time.time(),
            "step_type": STEP_TOOL_END,
            "ind": TOOL_PHASE_INDEX,
            "failure_category": "user_cancelled",
            "cancellation_source": "chat_stop",
            "process_state": cancellation.get("process_state") or "orphaned_until_terminal",
            "runtime_kill_attempted": bool(cancellation.get("runtime_kill_attempted")),
            "runtime_kill_supported": bool(cancellation.get("runtime_kill_supported")),
        }
        tool_batch_id = identity.tool_batch_id if identity and identity.tool_batch_id else metadata.get("tool_batch_id")
        if isinstance(tool_batch_id, str) and tool_batch_id.strip():
            event_metadata["tool_batch_id"] = tool_batch_id.strip()
        return {
            "type": "tool_end",
            "content": "Tool stopped",
            "metadata": {key: value for key, value in event_metadata.items() if value is not None},
        }

    @staticmethod
    def _tool_batch_end_event(
        *,
        batch_id: str,
        grouped_rows: Iterable[tuple[ToolExecution, StreamToolIdentity]],
        batch_identity: StreamBatchIdentity | None,
        terminal_tool_statuses: Mapping[str, str],
        fallback_turn_id: str,
    ) -> dict[str, Any]:
        row_pairs = list(grouped_rows)
        first_row, first_identity = row_pairs[0]
        turn_id = str(first_row.turn_id or fallback_turn_id)
        row_by_call_id = {str(row.tool_call_id or "").strip(): row for row, _identity in row_pairs}
        if batch_identity is not None and batch_identity.tool_calls:
            tool_calls = list(batch_identity.tool_calls)
        else:
            tool_calls = [identity for _row, identity in row_pairs]
        results: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        completed = 0
        for identity in tool_calls:
            tool_call_id = identity.tool_call_id
            status = terminal_tool_statuses.get(tool_call_id)
            if not status:
                status = "cancelled"
            normalized_status = status.lower()
            if normalized_status in {"success", "ok", "completed"}:
                completed += 1
            row = row_by_call_id.get(tool_call_id)
            tool_name = identity.tool_name or (str(row.tool_name) if row is not None else "unknown")
            results.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool": tool_name,
                    "tool_name": tool_name,
                    "status": status,
                    "failure_category": None
                    if normalized_status in {"success", "ok", "completed"}
                    else "user_cancelled",
                    "error": None
                    if normalized_status in {"success", "ok", "completed"}
                    else "user_cancelled",
                }
            )
            calls.append(
                {
                    "tool_call_id": tool_call_id,
                    "tool": tool_name,
                    "tool_name": tool_name,
                    "status": status,
                }
            )
        conversation_id = (
            batch_identity.conversation_id
            if batch_identity and batch_identity.conversation_id
            else first_identity.conversation_id or first_row.conversation_id
        )
        turn_sequence = (
            batch_identity.turn_sequence
            if batch_identity and batch_identity.turn_sequence is not None
            else first_identity.turn_sequence
        )
        metadata: dict[str, Any] = {
            "subtype": "tool_batch_end",
            "step_type": "tool_batch_end",
            "tool_batch_id": batch_id,
            "execution_strategy": None,
            "requested_execution_strategy": None,
            "tool_batch_total": len(tool_calls),
            "tool_calls": calls,
            "calls": calls,
            "status": "cancelled",
            "success": False,
            "completed": completed,
            "failed": max(len(tool_calls) - completed, 0),
            "results": results,
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "turn_id": turn_id,
            "id": turn_id,
            "turn_sequence": turn_sequence,
            "streaming": False,
            "is_streaming": False,
            "in_progress": False,
            "source": "chat_stop",
            "timestamp": time.time(),
            "ind": TOOL_PHASE_INDEX,
            "failure_category": "user_cancelled",
            "cancellation_source": "chat_stop",
        }
        return {
            "type": "tool_batch_end",
            "content": "Tool batch stopped",
            "metadata": {key: value for key, value in metadata.items() if value is not None},
        }

    def _load_stream_tool_history(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str,
    ) -> StreamToolHistory:
        rows = list(
            self._db.execute(
                select(StreamEvent)
                .where(
                    StreamEvent.tenant_id == tenant_id,
                    StreamEvent.task_id == task_id,
                    StreamEvent.turn_id == turn_id,
                    StreamEvent.event_type.in_(_TOOL_STREAM_EVENT_TYPES),
                )
                .order_by(StreamEvent.sequence.asc(), StreamEvent.id.asc())
            )
            .scalars()
            .all()
        )
        tools: dict[str, StreamToolIdentity] = {}
        batches: dict[str, StreamBatchIdentity] = {}
        terminal_tool_statuses: dict[str, str] = {}
        terminal_batches: set[str] = set()
        for row in rows:
            metadata = self._stream_event_metadata(row)
            event_type = str(row.event_type or metadata.get("step_type") or "").strip()
            if event_type == "tool_batch_start":
                batch_id = self._read_string(metadata.get("tool_batch_id"))
                if not batch_id:
                    continue
                call_identities = tuple(
                    self._identity_from_batch_call(
                        entry=entry,
                        batch_id=batch_id,
                        metadata=metadata,
                    )
                    for entry in self._stream_tool_calls(metadata)
                )
                call_identities = tuple(identity for identity in call_identities if identity is not None)
                batches[batch_id] = StreamBatchIdentity(
                    tool_batch_id=batch_id,
                    tool_calls=call_identities,
                    conversation_id=self._conversation_id(metadata),
                    turn_sequence=self._read_int(metadata.get("turn_sequence")),
                )
                for identity in call_identities:
                    tools.setdefault(identity.tool_call_id, identity)
            elif event_type == "tool_start":
                identity = self._identity_from_tool_metadata(metadata)
                if identity is not None:
                    tools[identity.tool_call_id] = self._merge_identity(
                        tools.get(identity.tool_call_id),
                        identity,
                    )
            elif event_type == "tool_end":
                tool_call_id = self._read_string(metadata.get("tool_call_id"))
                if tool_call_id:
                    terminal_tool_statuses[tool_call_id] = self._read_string(metadata.get("status")) or "completed"
            elif event_type == "tool_batch_end":
                batch_id = self._read_string(metadata.get("tool_batch_id"))
                if batch_id:
                    terminal_batches.add(batch_id)
                for result in self._stream_results(metadata):
                    tool_call_id = self._read_string(result.get("tool_call_id"))
                    if tool_call_id:
                        terminal_tool_statuses[tool_call_id] = (
                            self._read_string(result.get("status")) or "completed"
                        )
        return StreamToolHistory(
            tools=tools,
            batches=batches,
            terminal_tool_statuses=terminal_tool_statuses,
            terminal_batches=terminal_batches,
        )

    @classmethod
    def _identity_from_tool_metadata(cls, metadata: Mapping[str, Any]) -> StreamToolIdentity | None:
        tool_call_id = cls._read_string(metadata.get("tool_call_id"))
        if not tool_call_id:
            return None
        return StreamToolIdentity(
            tool_call_id=tool_call_id,
            tool_batch_id=cls._read_string(metadata.get("tool_batch_id")),
            tool_name=cls._read_string(metadata.get("tool_name"))
            or cls._read_string(metadata.get("tool"))
            or cls._read_string(metadata.get("command")),
            conversation_id=cls._conversation_id(metadata),
            turn_sequence=cls._read_int(metadata.get("turn_sequence")),
        )

    @classmethod
    def _identity_from_batch_call(
        cls,
        *,
        entry: Mapping[str, Any],
        batch_id: str,
        metadata: Mapping[str, Any],
    ) -> StreamToolIdentity | None:
        tool_call_id = cls._read_string(entry.get("tool_call_id"))
        if not tool_call_id:
            return None
        return StreamToolIdentity(
            tool_call_id=tool_call_id,
            tool_batch_id=batch_id,
            tool_name=cls._read_string(entry.get("tool_name"))
            or cls._read_string(entry.get("tool"))
            or cls._read_string(entry.get("tool_id")),
            conversation_id=cls._conversation_id(metadata),
            turn_sequence=cls._read_int(metadata.get("turn_sequence")),
        )

    @staticmethod
    def _merge_identity(
        existing: StreamToolIdentity | None,
        incoming: StreamToolIdentity,
    ) -> StreamToolIdentity:
        if existing is None:
            return incoming
        return StreamToolIdentity(
            tool_call_id=incoming.tool_call_id,
            tool_batch_id=incoming.tool_batch_id or existing.tool_batch_id,
            tool_name=incoming.tool_name or existing.tool_name,
            conversation_id=incoming.conversation_id or existing.conversation_id,
            turn_sequence=incoming.turn_sequence
            if incoming.turn_sequence is not None
            else existing.turn_sequence,
        )

    @staticmethod
    def _stream_event_metadata(row: StreamEvent) -> dict[str, Any]:
        payload = row.payload if isinstance(row.payload, dict) else {}
        obj = payload.get("obj") if isinstance(payload.get("obj"), dict) else {}
        metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        return dict(metadata)

    @staticmethod
    def _stream_tool_calls(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        raw = metadata.get("tool_calls")
        if not isinstance(raw, list):
            raw = metadata.get("calls")
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw if isinstance(entry, Mapping)]

    @staticmethod
    def _stream_results(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        raw = metadata.get("results")
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw if isinstance(entry, Mapping)]

    @staticmethod
    def _conversation_id(metadata: Mapping[str, Any]) -> str | None:
        return ChatToolCancelStreamProjectionService._read_string(metadata.get("conversation_id")) or (
            ChatToolCancelStreamProjectionService._read_string(metadata.get("conversationId"))
        )

    @staticmethod
    def _read_string(value: Any) -> str | None:
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _read_int(value: Any) -> int | None:
        if isinstance(value, int):
            return value
        return None

    @staticmethod
    def _is_cancelled_row(row: ToolExecution) -> bool:
        status = str(row.status or "").strip().lower()
        if status in {"cancel_requested", "cancelled", "canceled", "stopped"}:
            return True
        metadata = row.execution_metadata if isinstance(row.execution_metadata, Mapping) else {}
        cancellation = metadata.get("cancellation")
        return isinstance(cancellation, Mapping) and bool(cancellation.get("cancel_requested"))


__all__ = ["ChatToolCancelStreamProjectionService", "ToolCancelStreamProjectionResult"]
