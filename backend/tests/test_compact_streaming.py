"""
Compact streaming and replay contract tests.

These tests validate compact-mode behavior across streaming adapter persistence
and replay reconstruction so tool outputs remain compact-only end to end.
"""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import Mock

import pytest

# Required before importing backend modules that initialize DB dependencies.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


def _compact_payload() -> Dict[str, Any]:
    return {
        "schema_version": "2.0",
        "tool": "nmap",
        "status": "success",
        "success": True,
        "exit_code": 0,
        "summary": "Scan finished with one open port.",
        "key_findings": ["80/tcp open"],
        "errors": [],
        "report_recommendations": ["Investigate exposed service"],
        "structured_signals": [{"type": "service", "port": 80, "service": "http"}],
        "decision_evidence": ["80/tcp open http"],
        "lossiness_risk": "low",
        "artifact_refs": [{"path": "/workspace/artifacts/tool_outputs/run-1.txt"}],
        "compression": {"source": "llm"},
    }


def test_streaming_adapter_persists_compact_tool_summary() -> None:
    """tool_end persistence writes compact payload to tool_result in compact mode."""
    adapter = LangGraphStreamingAdapter()
    state_container = Mock()
    state_container.get_tool_call_parameters.return_value = {"target": "127.0.0.1"}
    state_container.add_tool_call.side_effect = lambda payload: payload
    state_container.reserved_message_id = None
    compact_payload = _compact_payload()
    event = {
        "type": "tool_end",
        "tool": "nmap",
        "tool_call_id": "call-1",
        "conversation_id": "conv-1",
        "turn_id": "turn-1",
        "status": "success",
        "duration": 1.1,
        "exit_code": 0,
        "summary": {"summary": "Scan finished with one open port."},
        "compact_tool_result": compact_payload,
    }

    result = adapter.process_streaming_event(event, state_container=state_container)

    assert result is not None
    persisted_payload = state_container.add_tool_call.call_args[0][0]["tool_result"]
    assert persisted_payload["schema_version"] == "2.0"
    assert persisted_payload["summary"] == "Scan finished with one open port."
    assert persisted_payload["key_findings"] == ["80/tcp open"]
    assert persisted_payload["structured_signals"] == [{"type": "service", "port": 80, "service": "http"}]


def test_compact_mode_does_not_emit_tool_delta_events() -> None:
    """Compact mode suppresses raw tool_delta in live adapter output."""
    adapter = LangGraphStreamingAdapter()
    raw_delta_event = {
        "type": "tool_delta",
        "tool": "nmap",
        "tool_call_id": "call-1",
        "content": "Starting nmap...",
        "conversation_id": "conv-1",
        "turn_id": "turn-1",
    }

    assert adapter.process_streaming_event(raw_delta_event) is None


def test_frontend_receives_compact_tool_result_on_tool_end() -> None:
    """Streaming adapter forwards compact payload in tool_end metadata."""
    adapter = LangGraphStreamingAdapter()
    compact_payload = _compact_payload()
    event = {
        "type": "tool_end",
        "tool": "nmap",
        "tool_call_id": "call-1",
        "conversation_id": "conv-1",
        "turn_id": "turn-1",
        "status": "success",
        "duration": 1.1,
        "exit_code": 0,
        "summary": {"summary": "Scan finished with one open port."},
        "compact_tool_result": compact_payload,
    }

    result = adapter.process_streaming_event(event)

    assert result is not None
    compact = result["metadata"]["compact_tool_result"]
    assert isinstance(compact, dict)
    assert compact["schema_version"] == "2.0"
    assert compact["summary"] == "Scan finished with one open port."
