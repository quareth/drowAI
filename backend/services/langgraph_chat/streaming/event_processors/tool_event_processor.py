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

from agent.graph.compression.schema import CompactToolOutput
from agent.graph.contracts.streaming_constants import (
    STEP_TOOL_END,
    STEP_TOOL_START,
    TOOL_PHASE_INDEX,
)
from backend.services.metrics.utils import safe_inc

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

        processed = {
            "type": "tool_start",
            "content": f"Executing {tool}...",
            "metadata": {
                "subtype": "tool_start",
                "tool": tool,
                "parameters": parameters,
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

    def process_tool_delta(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Process tool delta event (raw output chunk)."""
        _ = event
        return None

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
        error = event.get("error")
        ind = event.get("ind")
        tool_batch_id = event.get("tool_batch_id")
        sub_turn_index = event.get("sub_turn_index")
        if sub_turn_index is None:
            raw_metadata = event.get("metadata")
            if isinstance(raw_metadata, Mapping):
                sub_turn_index = raw_metadata.get("sub_turn_index")
        parameters = event.get("parameters", {})

        normalized_compact_tool_result = self._normalize_compact_tool_result(
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

        if state_container is not None:
            if not parameters and tool_call_id:
                cached_params = state_container.get_tool_call_parameters(tool_call_id)
                if cached_params:
                    parameters = cached_params
            tool_call_info: dict[str, Any] = {
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "tool_id": None,
                "tool_name": str(tool),
                "tool_arguments": parameters if isinstance(parameters, dict) else {},
                "tool_result": normalized_compact_tool_result,
            }
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

        processed = {
            "type": "tool_end",
            "content": f"Tool {tool} completed ({status})",
            "metadata": {
                "subtype": "tool_end",
                "tool": tool,
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "status": status,
                "duration": duration,
                "exit_code": exit_code,
                "summary": summary,
                "compact_tool_result": normalized_compact_tool_result,
                "error": error,
                "conversation_id": conversation_id,
                "conversationId": conversation_id,
                "id": turn_id,
                "streaming": False,
                "source": "langgraph_stream",
                "timestamp": time.time(),
            },
        }
        processed["metadata"]["step_type"] = STEP_TOOL_END
        processed["metadata"]["ind"] = ind if ind is not None else TOOL_PHASE_INDEX

        self._metric_inc("langgraph_tool_ends_processed")
        return processed

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
    def _normalize_compact_tool_result(
        *,
        tool: str,
        status: str,
        exit_code: Any,
        summary: Any,
        error: Any,
        compact_tool_result: Any,
    ) -> dict[str, Any]:
        """Return a compact-tool-result payload that matches schema shape."""
        source_payload = compact_tool_result if isinstance(compact_tool_result, Mapping) else {}
        summary_payload = summary if isinstance(summary, Mapping) else {}

        normalized_exit_code: Optional[int]
        if exit_code is None:
            normalized_exit_code = None
        else:
            try:
                normalized_exit_code = int(exit_code)
            except (TypeError, ValueError):
                normalized_exit_code = None

        merged_payload: dict[str, Any] = {
            "schema_version": source_payload.get("schema_version", "2.0"),
            "tool": source_payload.get("tool", tool),
            "status": source_payload.get("status", status),
            "success": source_payload.get(
                "success",
                str(status).lower() in {"success", "ok"},
            ),
            "exit_code": normalized_exit_code,
            "summary": source_payload.get("summary")
            or summary_payload.get("summary")
            or summary
            or "",
            "key_findings": source_payload.get(
                "key_findings",
                summary_payload.get("key_findings"),
            ),
            "errors": source_payload.get("errors", summary_payload.get("errors")),
            "report_recommendations": source_payload.get(
                "report_recommendations",
                summary_payload.get("report_recommendations"),
            ),
            "structured_signals": source_payload.get(
                "structured_signals",
                summary_payload.get("structured_signals"),
            ),
            "decision_evidence": source_payload.get(
                "decision_evidence",
                summary_payload.get("decision_evidence"),
            ),
            "lossiness_risk": source_payload.get(
                "lossiness_risk",
                summary_payload.get("lossiness_risk"),
            ),
            "artifact_refs": source_payload.get("artifact_refs") or [],
            "compression": source_payload.get("compression"),
        }
        if error and not merged_payload.get("errors"):
            merged_payload["errors"] = [str(error)]

        return CompactToolOutput.from_dict(merged_payload).to_dict()

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
