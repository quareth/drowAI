"""Tests for Phase 5 PTR history formatter behavior."""

from __future__ import annotations

import copy
from typing import Any, Dict

from agent.graph.utils import iteration_memory as _iteration_memory
from agent.graph.utils.history_formatter import (
    EMPTY_ITERATION_HISTORY_MARKER,
    build_iteration_history,
    build_iteration_history_from_state,
)
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_build_iteration_history_returns_marker_when_ledger_present() -> None:
    metadata: Dict[str, Any] = {}
    _iteration_memory.append(
        metadata,
        turn_sequence=2,
        source="ptr",
        payload={
            "sections": [
                {"heading": "PTR Decision", "body": "PTR summary"},
            ]
        },
    )

    result = build_iteration_history(
        trace_observations=["observation"],
        trace_reasoning=["reasoning"],
        metadata=metadata,
        turn_sequence=2,
    )

    assert result == EMPTY_ITERATION_HISTORY_MARKER


def test_build_iteration_history_returns_marker_when_ledger_absent() -> None:
    result = build_iteration_history(
        trace_observations=["observation"],
        trace_reasoning=["reasoning"],
        metadata={},
        turn_sequence=2,
    )

    assert result == EMPTY_ITERATION_HISTORY_MARKER


def test_build_iteration_history_is_pure_rendering() -> None:
    metadata: Dict[str, Any] = {
        "working_memory": {"current_turn_phases": []},
        "unrelated": "value",
    }
    snapshot = copy.deepcopy(metadata)

    _ = build_iteration_history(
        trace_observations=["observation"],
        trace_reasoning=["reasoning"],
        metadata=metadata,
        turn_sequence=1,
    )

    assert metadata == snapshot


def test_build_iteration_history_from_state_returns_marker() -> None:
    interactive = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="scan host",
            metadata={"working_memory": {"current_turn_phases": []}},
        ),
        trace=TraceState(
            observations=["observation"],
            reasoning=["reasoning"],
        ),
    )

    assert build_iteration_history_from_state(interactive) == EMPTY_ITERATION_HISTORY_MARKER
