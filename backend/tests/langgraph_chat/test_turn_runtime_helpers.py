"""Characterization tests for shared LangGraph handler turn-runtime helpers."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from agent.graph import InteractiveInput, InteractiveState, build_initial_state
from backend.services.langgraph_chat.contracts import ChatInputs
from backend.services.langgraph_chat.handlers.turn_runtime import (
    TurnIdentity,
    apply_agent_thread_config,
    build_cancelled_result,
    build_interrupted_result,
    new_captured_state,
    parse_interactive_state_from_final,
)
from backend.services.langgraph_chat.hitl_constants import GRAPH_NAME_SIMPLE_TOOL
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


def _chat_inputs() -> ChatInputs:
    return ChatInputs(
        task_id=7,
        user_id=11,
        message="run tool",
        conversation_id="conv-7",
        history=[],
    )


def test_apply_agent_thread_config_writes_canonical_graph_and_turn_fields() -> None:
    config = {"configurable": {"thread_id": "graph-" + ("a" * 32)}}
    turn = TurnIdentity(turn_id="task-7-turn-3", turn_number=3, metadata={})

    thread_id = apply_agent_thread_config(
        config,
        task_id=7,
        graph_name=GRAPH_NAME_SIMPLE_TOOL,
        turn=turn,
        conversation_id="conv-7",
    )

    assert thread_id == "graph-" + ("a" * 32)
    assert config["configurable"]["graph_name"] == GRAPH_NAME_SIMPLE_TOOL
    assert config["configurable"]["canonical_turn_id"] == "task-7-turn-3"
    assert config["configurable"]["canonical_turn_sequence"] == 3
    assert config["configurable"]["canonical_conversation_id"] == "conv-7"


def test_cancelled_and_interrupted_results_preserve_execution_metadata() -> None:
    captured_state = new_captured_state(include_interrupted=True)
    captured_state["execution_metadata"] = {"runtime_path": "warm"}

    cancelled = build_cancelled_result(
        chat_inputs=_chat_inputs(),
        thread_id="graph-" + ("a" * 32),
        graph_name=GRAPH_NAME_SIMPLE_TOOL,
        captured_state=captured_state,
    )
    interrupted = build_interrupted_result(
        chat_inputs=_chat_inputs(),
        thread_id="graph-" + ("a" * 32),
        graph_name=GRAPH_NAME_SIMPLE_TOOL,
        captured_state=captured_state,
    )

    assert cancelled.metadata == {
        "cancelled": True,
        "interrupt_type": "run_cancelled",
        "thread_id": "graph-" + ("a" * 32),
        "graph_name": GRAPH_NAME_SIMPLE_TOOL,
        "runtime_path": "warm",
    }
    assert interrupted.metadata == {
        "interrupted": True,
        "interrupt_type": "tool_approval",
        "thread_id": "graph-" + ("a" * 32),
        "graph_name": GRAPH_NAME_SIMPLE_TOOL,
        "runtime_path": "warm",
    }
    assert cancelled.persistence_handled is True
    assert interrupted.persistence_handled is True


def test_deterministic_final_state_fallback_prefers_streamed_answer() -> None:
    payload = InteractiveInput(
        task_id=7,
        message="original request",
        conversation_id="conv-7",
        metadata={},
    )
    starting_state = InteractiveState.from_mapping(build_initial_state(payload))
    state_container = ChatStateContainer()
    state_container.append_answer("streamed answer")

    parsed = parse_interactive_state_from_final(
        final_state={"trace": {"final_text": "snapshot answer"}},
        starting_state=starting_state,
        deterministic_mode=True,
        state_container=state_container,
        task_id=7,
        missing_state_message="missing",
    )

    assert parsed.trace.final_text == "streamed answer"
    assert parsed.facts.message == "streamed answer"
