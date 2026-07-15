"""Tests for per-connection ConnectionSessionState mutable defaults."""

from __future__ import annotations

import queue

from drowai_runner.control_channel.session.state import ConnectionSessionState


def test_connection_session_state_instances_have_independent_containers() -> None:
    first = ConnectionSessionState()
    second = ConnectionSessionState()

    first.ack_decisions_by_message_id["msg-1"] = ("accepted", None)
    first.processed_runtime_messages.add("msg-1")
    first.assigned_runtime_jobs["job-1"] = 42
    first.cached_tool_command_results[("job-1", "cmd-1")] = object()  # type: ignore[assignment]
    first.inflight_tool_commands[("job-1", "cmd-1")] = object()  # type: ignore[assignment]
    first.pending_upload_contexts[("job-1", "cmd-1")] = object()  # type: ignore[assignment]
    first.tool_command_dispatch_events.put(object())

    assert second.ack_decisions_by_message_id == {}
    assert second.processed_runtime_messages == set()
    assert second.assigned_runtime_jobs == {}
    assert second.cached_tool_command_results == {}
    assert second.inflight_tool_commands == {}
    assert second.pending_upload_contexts == {}
    try:
        second.tool_command_dispatch_events.get_nowait()
        raise AssertionError("second queue should be empty")
    except queue.Empty:
        pass


def test_connection_session_state_field_types_match_contract() -> None:
    state = ConnectionSessionState()

    assert isinstance(state.ack_decisions_by_message_id, dict)
    assert isinstance(state.processed_runtime_messages, set)
    assert isinstance(state.assigned_runtime_jobs, dict)
    assert isinstance(state.cached_tool_command_results, dict)
    assert isinstance(state.inflight_tool_commands, dict)
    assert isinstance(state.pending_upload_contexts, dict)
    assert isinstance(state.tool_command_dispatch_events, queue.SimpleQueue)
