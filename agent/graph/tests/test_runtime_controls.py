"""Tests for transient-safe current-turn runtime execution controls."""

from __future__ import annotations

from agent.graph.runtime_controls import (
    read_active_execution_control,
    set_active_execution_control,
)


def _active(session_id: str = "shs_control_123") -> dict[str, object]:
    return {
        "originating_tool_id": "shell.utility",
        "continuation_tool_id": "shell.write_stdin",
        "process_status": "running",
        "session_id": session_id,
        "stdin_available": True,
    }


def test_active_execution_control_round_trips_without_output_fields() -> None:
    metadata: dict[str, object] = {"turn_sequence": 3}

    set_active_execution_control(
        metadata,
        turn_sequence=3,
        active_execution=_active(),
    )

    active = read_active_execution_control(metadata)
    assert active == _active()
    assert set(active or {}).isdisjoint({"stdout", "stderr", "command", "parameters"})


def test_active_execution_control_is_hidden_from_a_different_turn() -> None:
    metadata: dict[str, object] = {"turn_sequence": 3}
    set_active_execution_control(
        metadata,
        turn_sequence=3,
        active_execution=_active(),
    )

    metadata["turn_sequence"] = 4

    assert read_active_execution_control(metadata) is None


def test_active_execution_control_clears_at_terminal_state() -> None:
    metadata: dict[str, object] = {"turn_sequence": 3}
    set_active_execution_control(
        metadata,
        turn_sequence=3,
        active_execution=_active(),
    )

    set_active_execution_control(
        metadata,
        turn_sequence=3,
        active_execution=None,
    )

    assert read_active_execution_control(metadata) is None
    controls = metadata["current_turn_runtime_controls"]
    assert isinstance(controls, dict)
    assert "active_execution" not in controls
