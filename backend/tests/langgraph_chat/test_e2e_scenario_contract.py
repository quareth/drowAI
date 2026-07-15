"""Contract tests for browser-visible deterministic E2E scenario events."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langgraph.types import Command

from agent.graph import InteractiveState
from agent.graph.graph_names import (
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_INTERRUPT_RESUME,
    GRAPH_NAME_SIMPLE_TOOL,
)
from backend.services.langgraph_chat.execution.scenario_factory import get_scenario_graph


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_name", [GRAPH_NAME_SIMPLE_TOOL, GRAPH_NAME_DEEP_REASONING])
async def test_deterministic_tool_events_have_renderable_identity_and_status(graph_name: str) -> None:
    """Tool activity must render a stable completed row in the real chat UI."""
    graph = get_scenario_graph(graph_name, checkpointer=None)
    events = [chunk async for mode, chunk in graph.astream({}, stream_mode="custom") if mode == "custom"]
    tool_events = [event for event in events if event.get("type") in {"tool_start", "tool_end"}]

    assert [event["type"] for event in tool_events] == ["tool_start", "tool_end"]
    assert {event["tool_call_id"] for event in tool_events} == {"deterministic-workspace-read-1"}
    assert all(event["tool"] == "workspace_read" for event in tool_events)
    assert tool_events[-1]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graph_name", "expected_types"),
    [
        (
            GRAPH_NAME_SIMPLE_TOOL,
            ["tool_start", "tool_end", "message_start", "message_delta", "section_end"],
        ),
        (
            GRAPH_NAME_DEEP_REASONING,
            [
                "reasoning_start",
                "reasoning_delta",
                "reasoning_section_end",
                "tool_start",
                "tool_end",
                "observation_start",
                "observation_delta",
                "observation_section_end",
                "message_start",
                "message_delta",
                "section_end",
            ],
        ),
    ],
)
async def test_deterministic_scenario_events_are_ordered(
    graph_name: str,
    expected_types: list[str],
) -> None:
    """Offline scenarios preserve the lifecycle order asserted by browser journeys."""
    graph = get_scenario_graph(graph_name, checkpointer=None)
    events = [chunk async for _, chunk in graph.astream({}, stream_mode="custom")]

    assert [event["type"] for event in events] == expected_types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        ("deterministic-interrupt-approval", "tool_approval"),
        ("deterministic-interrupt-plan-review", "plan_review"),
        ("deterministic-interrupt-clarify", "clarify_request"),
    ],
)
async def test_interrupt_scenario_selects_typed_payload_from_prompt(
    message: str,
    expected_type: str,
) -> None:
    """One E2E-only graph exposes every typed interrupt without production branching."""
    graph = get_scenario_graph(GRAPH_NAME_INTERRUPT_RESUME, checkpointer=None)
    events = [
        chunk
        async for _, chunk in graph.astream(
            {"facts": {"task_id": 42, "message": message, "metadata": {"reserved_message_id": 7}}},
            config={
                "configurable": {
                    "canonical_turn_id": "task-42-turn-1",
                    "canonical_turn_sequence": 1,
                    "canonical_conversation_id": "conv-42",
                }
            },
            stream_mode=["custom", "values"],
        )
    ]

    assert [event.get("type") for event in events[:2]] == ["reasoning_start", "reasoning_delta"]
    interrupt = next(event["__interrupt__"][0] for event in events if "__interrupt__" in event)
    assert interrupt["type"] == expected_type
    assert interrupt["turn_id"] == "task-42-turn-1"
    assert interrupt["turn_sequence"] == 1
    assert interrupt["reserved_message_id"] == 7
    assert interrupt["conversation_id"] == "conv-42"
    if expected_type == "tool_approval":
        assert interrupt["tool_name"] == "Workspace Read"
    elif expected_type == "plan_review":
        assert interrupt["plan_steps"]
    else:
        assert interrupt["questions"][0]["options"] == ["Internal", "External"]


@pytest.mark.asyncio
async def test_interrupt_resume_scenario_emits_parseable_task_local_final_state() -> None:
    """Resume values must satisfy the real continuation parser, not only the graph executor."""
    graph = get_scenario_graph(GRAPH_NAME_INTERRUPT_RESUME, checkpointer=None)
    events = [
        chunk
        async for mode, chunk in graph.astream(
            Command(resume={"action": "approve"}),
            config={
                "configurable": {
                    "runtime_projection": {"task_id": 42},
                    "canonical_conversation_id": "conv-42",
                }
            },
            stream_mode=["custom", "values"],
        )
        if mode == "values"
    ]

    state = InteractiveState.from_mapping(events[-1])
    assert state.facts.task_id == 42
    assert state.facts.conversation_id == "conv-42"
    assert state.trace.final_text == "Approved and resumed."


@pytest.mark.asyncio
async def test_cancellable_scenario_pauses_after_exposing_running_state(monkeypatch) -> None:
    """The browser must have time to stop an active deterministic chat turn."""
    sleep = AsyncMock()
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.scenario_factory.asyncio.sleep",
        sleep,
    )
    graph = get_scenario_graph(GRAPH_NAME_DEEP_REASONING, checkpointer=None)

    events = [
        chunk
        async for mode, chunk in graph.astream(
            {"facts": {"message": "deterministic-cancellable-chat"}},
            stream_mode="custom",
        )
        if mode == "custom"
    ]

    assert [event["type"] for event in events] == ["reasoning_start", "reasoning_delta"]
    sleep.assert_awaited_once_with(60.0)
