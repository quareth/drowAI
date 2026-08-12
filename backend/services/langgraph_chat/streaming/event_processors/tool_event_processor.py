"""Translate tool execution events and manage tool-specific side effects.

Responsibilities:
- build ``tool_start`` and ``tool_end`` payloads for the live stream
- suppress raw ``tool_delta`` chunks in compact-only mode
- normalize compact tool-result payloads into the schema expected by persistence
  and downstream consumers
- update ``ChatStateContainer`` with tool call lifecycle data
- trigger immediate tool snapshot persistence when the required identifiers exist
- preserve the current diagnostic logging and metrics behavior for tool events

This module is the only event-family processor that owns a persistence-related
side effect, but it still delegates the database write itself to
``ToolCallSnapshotService``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Callable, Optional, TYPE_CHECKING

from agent.graph.contracts.streaming_constants import (
    STEP_TOOL_DELTA,
    STEP_TOOL_END,
    STEP_TOOL_START,
    TOOL_PHASE_INDEX,
)
from agent.tools.utils import resolve_command_text_for_execution
from backend.services.chat.compact_tool_result import normalize_compact_tool_result
from backend.services.metrics.utils import safe_inc
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.shell_capabilities import (
    SHELL_SESSION_START_TOOL_IDS,
    SHELL_SESSION_TOOL_IDS,
    SHELL_WRITE_STDIN_TOOL_ID,
)

from backend.services.langgraph_chat.streaming.event_processors.snapshot_service import (
    ToolCallSnapshotService,
)

if TYPE_CHECKING:
    from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer

logger = logging.getLogger("backend.services.langgraph_chat.streaming_adapter")

try:
    from backend.services.langgraph_chat.diagnostic_logger import get_diagnostic_logger

    _diag_logger = get_diagnostic_logger()
except Exception:  # pragma: no cover - diagnostics unavailable
    _diag_logger = None


def _diag_info(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.info(message, *args)


def _diag_warning(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.warning(message, *args)


class ToolEventProcessor:
    """Own tool event construction, normalization, and snapshot timing."""

    def __init__(
        self,
        snapshot_service: ToolCallSnapshotService,
        *,
        metric_inc: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._snapshot_service = snapshot_service
        self._metric_inc = metric_inc or safe_inc

    def process_tool_start(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process tool start event."""
        tool = event.get("tool", "unknown")
        tool_call_id = event.get("tool_call_id")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        parameters = event.get("parameters", {})
        ind = event.get("ind")
        tool_batch_id = event.get("tool_batch_id")
        normalized_tool = str(tool or "unknown").strip()
        stream_parameters = self._redact_persisted_shell_stdin_arguments(
            tool=normalized_tool,
            parameters=dict(parameters) if isinstance(parameters, dict) else {},
        )
        command_display = (
            resolve_command_text_for_execution(normalized_tool, parameters)
            if normalized_tool in SHELL_SESSION_START_TOOL_IDS
            and isinstance(parameters, dict)
            else None
        )

        if not tool_call_id:
            logger.warning(
                "[STREAM_ADAPTER] tool_start missing tool_call_id (tool=%s conv=%s turn_id=%s turn_seq=%s keys=%s)",
                tool,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                list(event.keys()),
            )
            _diag_warning(
                "STREAM_ADAPTER | tool_start missing tool_call_id | tool=%s conv=%s turn_id=%s turn_seq=%s keys=%s",
                tool,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                list(event.keys()),
            )
        else:
            logger.info(
                "[STREAM_ADAPTER] tool_start (tool=%s tool_call_id=%s conv=%s turn_id=%s turn_seq=%s)",
                tool,
                tool_call_id,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
            )
            _diag_info(
                "STREAM_ADAPTER | tool_start | tool=%s tool_call_id=%s conv=%s turn_id=%s turn_seq=%s",
                tool,
                tool_call_id,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
            )

        if state_container is not None and tool_call_id:
            state_container.record_tool_call_start(tool_call_id, parameters)

        processed: dict[str, Any] = {
            "type": "tool_start",
            "content": f"Executing {tool}...",
            "metadata": {
                "subtype": "tool_start",
                "tool": tool,
                "parameters": stream_parameters,
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": True,
                "source": "langgraph_stream",
                "timestamp": time.time(),
            },
        }
        if command_display:
            processed["metadata"]["command_display"] = command_display
        processed["metadata"]["step_type"] = STEP_TOOL_START
        processed["metadata"]["ind"] = ind if ind is not None else TOOL_PHASE_INDEX

        self._metric_inc("langgraph_tool_starts_processed")
        return processed

    def process_tool_batch_start(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a tool batch lifecycle start event."""
        metadata = self._batch_metadata(event)
        metadata["subtype"] = "tool_batch_start"
        metadata["step_type"] = "tool_batch_start"
        metadata["streaming"] = True
        metadata["source"] = "langgraph_stream"
        metadata["timestamp"] = time.time()
        metadata["ind"] = event.get("ind", TOOL_PHASE_INDEX)
        self._metric_inc("langgraph_tool_batch_starts_processed")
        return {
            "type": "tool_batch_start",
            "content": "Tool batch started",
            "metadata": metadata,
        }

    def process_tool_delta(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> Optional[dict[str, Any]]:
        """Process shell lifecycle progress while suppressing ordinary raw chunks."""
        tool = str(event.get("tool") or "unknown").strip()
        if not self._is_shell_lifecycle_delta(event, tool=tool):
            return None

        tool_call_id = event.get("tool_call_id")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        compact_tool_result = event.get("compact_tool_result")
        output_persistence = event.get("output_persistence") or "transient"
        process_status = event.get("process_status")
        if process_status is None and isinstance(compact_tool_result, Mapping):
            process_status = compact_tool_result.get("process_status")
        session_status = event.get("session_status")
        if session_status is None and isinstance(compact_tool_result, Mapping):
            session_status = compact_tool_result.get("session_status")
        interaction_boundary = event.get("interaction_boundary")
        if interaction_boundary is None and isinstance(compact_tool_result, Mapping):
            interaction_boundary = compact_tool_result.get("interaction_boundary")
        session_id = event.get("session_id")
        if session_id is None and isinstance(compact_tool_result, Mapping):
            session_id = compact_tool_result.get("session_id")
        status = event.get("status", "success")
        content = str(event.get("content") or "")
        summary = event.get("summary", {})
        normalized_compact_tool_result = normalize_compact_tool_result(
            tool=tool,
            status=str(status),
            exit_code=event.get("exit_code"),
            summary=summary if isinstance(summary, Mapping) else {"summary": content},
            error=event.get("error"),
            compact_tool_result=compact_tool_result,
        )

        if state_container is not None and tool_call_id:
            tool_call_info: dict[str, Any] = {
                "tool_call_id": tool_call_id,
                "tool_batch_id": event.get("tool_batch_id"),
                "tool_id": None,
                "tool_name": tool,
                "tool_arguments": {},
                "tool_result": self._transient_lifecycle_result(
                    normalized_compact_tool_result
                ),
                "status": status,
                "process_status": process_status,
                "session_status": session_status,
                "interaction_boundary": interaction_boundary,
                "session_id": session_id,
                "output_persistence": output_persistence,
                "compact_tool_result": self._transient_lifecycle_result(
                    normalized_compact_tool_result
                ),
                "replace_existing_lifecycle": True,
            }
            sub_turn_index = self._coerce_non_negative_int(event.get("sub_turn_index"))
            if sub_turn_index is not None:
                tool_call_info["turn_index"] = sub_turn_index
            state_container.add_tool_call(tool_call_info)

        processed = {
            "type": "tool_delta",
            "content": content,
            "metadata": {
                "subtype": "tool_delta",
                "tool": tool,
                "tool_call_id": tool_call_id,
                "tool_batch_id": event.get("tool_batch_id"),
                "status": status,
                "process_status": process_status,
                "session_status": session_status,
                "interaction_boundary": interaction_boundary,
                "session_id": session_id,
                "compact_tool_result": normalized_compact_tool_result,
                "output_persistence": output_persistence,
                "shell_lifecycle_event": True,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": True,
                "source": "langgraph_stream",
                "timestamp": time.time(),
            },
        }
        processed["metadata"]["step_type"] = STEP_TOOL_DELTA
        processed["metadata"]["ind"] = event.get("ind", TOOL_PHASE_INDEX)
        self._metric_inc("langgraph_shell_lifecycle_deltas_processed")
        return processed

    def process_tool_end(
        self,
        event: dict[str, Any],
        state_container: Optional["ChatStateContainer"] = None,
    ) -> dict[str, Any]:
        """Process tool end event."""
        tool = event.get("tool", "unknown")
        tool_call_id = event.get("tool_call_id")
        conversation_id = event.get("conversation_id", "")
        turn_id = event.get("turn_id", "")
        status = event.get("status", "unknown")
        duration = event.get("duration", 0)
        exit_code = event.get("exit_code")
        summary = event.get("summary", {})
        compact_tool_result = event.get("compact_tool_result")
        output_persistence = event.get("output_persistence")
        process_status = event.get("process_status")
        if process_status is None and isinstance(compact_tool_result, Mapping):
            process_status = compact_tool_result.get("process_status")
        session_status = event.get("session_status")
        if session_status is None and isinstance(compact_tool_result, Mapping):
            session_status = compact_tool_result.get("session_status")
        interaction_boundary = event.get("interaction_boundary")
        if interaction_boundary is None and isinstance(compact_tool_result, Mapping):
            interaction_boundary = compact_tool_result.get("interaction_boundary")
        session_id = event.get("session_id")
        if session_id is None and isinstance(compact_tool_result, Mapping):
            session_id = compact_tool_result.get("session_id")
        error = event.get("error")
        ind = event.get("ind")
        tool_batch_id = event.get("tool_batch_id")
        sub_turn_index = event.get("sub_turn_index")
        if sub_turn_index is None:
            raw_metadata = event.get("metadata")
            if isinstance(raw_metadata, Mapping):
                sub_turn_index = raw_metadata.get("sub_turn_index")
        parameters = event.get("parameters", {})

        normalized_compact_tool_result = normalize_compact_tool_result(
            tool=str(tool),
            status=str(status),
            exit_code=exit_code,
            summary=summary,
            error=error,
            compact_tool_result=compact_tool_result,
        )

        if not tool_call_id:
            logger.warning(
                "[STREAM_ADAPTER] tool_end missing tool_call_id (tool=%s conv=%s turn_id=%s turn_seq=%s status=%s keys=%s)",
                tool,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                status,
                list(event.keys()),
            )
            _diag_warning(
                "STREAM_ADAPTER | tool_end missing tool_call_id | tool=%s conv=%s turn_id=%s turn_seq=%s status=%s keys=%s",
                tool,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                status,
                list(event.keys()),
            )
        else:
            logger.info(
                "[STREAM_ADAPTER] tool_end (tool=%s tool_call_id=%s conv=%s turn_id=%s turn_seq=%s status=%s)",
                tool,
                tool_call_id,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                status,
            )
            _diag_info(
                "STREAM_ADAPTER | tool_end | tool=%s tool_call_id=%s conv=%s turn_id=%s turn_seq=%s status=%s",
                tool,
                tool_call_id,
                conversation_id,
                turn_id,
                event.get("turn_sequence") or event.get("sequence"),
                status,
            )

        is_shell_lifecycle_event = bool(event.get("shell_lifecycle_event")) or (
            self._is_shell_lifecycle_tool_end(
                tool=str(tool),
                process_status=process_status,
                session_status=session_status,
                interaction_boundary=interaction_boundary,
            )
        )

        if state_container is not None:
            if not parameters and tool_call_id:
                cached_params = state_container.get_tool_call_parameters(tool_call_id)
                if cached_params:
                    parameters = cached_params
            durable_parameters = self._redact_persisted_shell_stdin_arguments(
                tool=str(tool),
                parameters=dict(parameters) if isinstance(parameters, dict) else {},
            )
            durable_parameters = mask_durable_secrets(
                durable_parameters,
                source="tool_call_arguments",
            )
            tool_call_info: dict[str, Any] = {
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "tool_id": None,
                "tool_name": str(tool),
                "tool_arguments": durable_parameters,
                "tool_result": (
                    self._transient_lifecycle_result(normalized_compact_tool_result)
                    if output_persistence == "transient"
                    else normalized_compact_tool_result
                ),
                "status": status,
                "process_status": process_status,
                "session_status": session_status,
                "interaction_boundary": interaction_boundary,
                "session_id": session_id,
                "output_persistence": output_persistence,
            }
            if is_shell_lifecycle_event:
                tool_call_info["replace_existing_lifecycle"] = True
            if output_persistence == "transient":
                tool_call_info["compact_tool_result"] = tool_call_info["tool_result"]
            normalized_sub_turn_index = self._coerce_non_negative_int(sub_turn_index)
            if normalized_sub_turn_index is not None:
                tool_call_info["turn_index"] = normalized_sub_turn_index
            stored_tool_call = state_container.add_tool_call(tool_call_info)
            reserved_message_id = state_container.reserved_message_id
            if isinstance(reserved_message_id, int) and tool_call_id:
                self._snapshot_service.persist_snapshot(
                    reserved_message_id=reserved_message_id,
                    tool_call_info=stored_tool_call,
                )

        processed: dict[str, Any] = {
            "type": "tool_end",
            "content": f"Tool {tool} completed ({status})",
            "metadata": {
                "subtype": "tool_end",
                "tool": tool,
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "status": status,
                "process_status": process_status,
                "session_status": session_status,
                "interaction_boundary": interaction_boundary,
                "session_id": session_id,
                "duration": duration,
                "exit_code": exit_code,
                "summary": summary,
                "compact_tool_result": normalized_compact_tool_result,
                "output_persistence": output_persistence,
                "error": error,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": False,
                "source": "langgraph_stream",
                "timestamp": time.time(),
            },
        }
        if is_shell_lifecycle_event:
            processed["metadata"]["shell_lifecycle_event"] = True
        processed["metadata"]["step_type"] = STEP_TOOL_END
        processed["metadata"]["ind"] = ind if ind is not None else TOOL_PHASE_INDEX

        self._metric_inc("langgraph_tool_ends_processed")
        return processed

    @staticmethod
    def _is_shell_lifecycle_delta(event: Mapping[str, Any], *, tool: str) -> bool:
        """Return whether a delta is an interactive shell lifecycle projection."""
        if tool not in SHELL_SESSION_TOOL_IDS:
            return False
        if bool(event.get("shell_lifecycle_event")):
            return True
        for key in ("process_status", "session_status", "interaction_boundary", "session_id"):
            if event.get(key) is not None:
                return True
        compact = event.get("compact_tool_result")
        if isinstance(compact, Mapping):
            return any(
                compact.get(key) is not None
                for key in (
                    "process_status",
                    "session_status",
                    "interaction_boundary",
                    "session_id",
                )
            )
        return False

    @staticmethod
    def _is_shell_lifecycle_tool_end(
        *,
        tool: str,
        process_status: Any,
        session_status: Any,
        interaction_boundary: Any,
    ) -> bool:
        """Return whether a tool_end should replace earlier shell lifecycle state."""
        if str(tool or "").strip() not in SHELL_SESSION_TOOL_IDS:
            return False
        return any(
            value is not None
            for value in (process_status, session_status, interaction_boundary)
        )

    def process_tool_batch_end(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a tool batch lifecycle end event."""
        metadata = self._batch_metadata(event)
        metadata["subtype"] = "tool_batch_end"
        metadata["step_type"] = "tool_batch_end"
        metadata["streaming"] = False
        metadata["source"] = "langgraph_stream"
        metadata["timestamp"] = time.time()
        metadata["ind"] = event.get("ind", TOOL_PHASE_INDEX)
        self._metric_inc("langgraph_tool_batch_ends_processed")
        return {
            "type": "tool_batch_end",
            "content": f"Tool batch completed ({metadata.get('status', 'unknown')})",
            "metadata": metadata,
        }

    @staticmethod
    def _batch_metadata(event: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize batch lifecycle metadata for frontend grouping."""
        calls = event.get("tool_calls")
        if not isinstance(calls, list):
            calls = event.get("calls")
        metadata: dict[str, Any] = {
            "tool_batch_id": event.get("tool_batch_id"),
            "execution_strategy": event.get("execution_strategy")
            or event.get("effective_execution_strategy"),
            "requested_execution_strategy": event.get("requested_execution_strategy"),
            "tool_batch_total": event.get("tool_batch_total"),
            "tool_calls": calls if isinstance(calls, list) else [],
            "calls": calls if isinstance(calls, list) else [],
            "status": event.get("status"),
            "success": event.get("success"),
            "completed": event.get("completed"),
            "failed": event.get("failed"),
            "results": event.get("results") if isinstance(event.get("results"), list) else [],
        }
        for key in ("conversation_id", "turn_id", "id"):
            if event.get(key) is not None:
                metadata[key] = event.get(key)
        if event.get("conversation_id") is not None:
            metadata["conversationId"] = event.get("conversation_id")
        return metadata

    @staticmethod
    def _transient_lifecycle_result(
        compact_tool_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Keep terminal status for transcript replay without retaining output."""
        return {
            "schema_version": compact_tool_result.get("schema_version", "2.0"),
            "tool": compact_tool_result.get("tool", "unknown"),
            "status": compact_tool_result.get("status", "unknown"),
            "success": bool(compact_tool_result.get("success")),
            "exit_code": compact_tool_result.get("exit_code"),
            "process_status": compact_tool_result.get("process_status"),
            "session_status": compact_tool_result.get("session_status"),
            "interaction_boundary": compact_tool_result.get("interaction_boundary"),
            "session_id": compact_tool_result.get("session_id"),
            "summary": "",
            "key_findings": [],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "high",
            "artifact_refs": [],
            "compression": None,
        }

    @staticmethod
    def _redact_persisted_shell_stdin_arguments(
        *,
        tool: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove raw interactive input before canonical history persistence."""
        normalized_tool = str(tool or "").strip()
        if normalized_tool != SHELL_WRITE_STDIN_TOOL_ID:
            return parameters
        redacted = dict(parameters)
        chars = redacted.pop("chars", None)
        redacted.pop("input", None)
        redacted["chars_redacted"] = True
        if isinstance(chars, str):
            redacted["chars_length"] = len(chars)
        return redacted

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> Optional[int]:
        """Return a non-negative integer when the raw value normalizes cleanly."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, float):
            if value.is_integer() and value >= 0:
                return int(value)
            return None
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            try:
                parsed = int(candidate)
            except ValueError:
                return None
            return parsed if parsed >= 0 else None
        return None


__all__ = ["ToolEventProcessor"]
