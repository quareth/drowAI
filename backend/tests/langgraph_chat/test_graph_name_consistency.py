"""Regression tests for canonical LangGraph runtime graph names."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def test_backend_hitl_constants_reexport_agent_graph_names() -> None:
    from agent.graph.graph_names import (
        DEFAULT_GRAPH_NAME,
        GRAPH_NAME_DEEP_REASONING,
        GRAPH_NAME_INTERRUPT_RESUME,
        GRAPH_NAME_NORMAL_CHAT,
        GRAPH_NAME_SIMPLE_TOOL,
    )
    from backend.services.langgraph_chat import hitl_constants

    assert hitl_constants.GRAPH_NAME_SIMPLE_TOOL == GRAPH_NAME_SIMPLE_TOOL == "simple_tool"
    assert hitl_constants.GRAPH_NAME_DEEP_REASONING == GRAPH_NAME_DEEP_REASONING == "deep_reasoning"
    assert hitl_constants.GRAPH_NAME_NORMAL_CHAT == GRAPH_NAME_NORMAL_CHAT == "normal_chat"
    assert (
        hitl_constants.GRAPH_NAME_INTERRUPT_RESUME
        == GRAPH_NAME_INTERRUPT_RESUME
        == "interrupt_resume"
    )
    assert hitl_constants.DEFAULT_GRAPH_NAME == DEFAULT_GRAPH_NAME == GRAPH_NAME_SIMPLE_TOOL


def test_builder_graph_names_are_runtime_names() -> None:
    from agent.graph.builders.deep_reasoning_builder import GRAPH_NAME as deep_reasoning_name
    from agent.graph.builders.simple_tool_builder import GRAPH_NAME as simple_tool_name
    from agent.graph.graph_names import GRAPH_NAME_DEEP_REASONING, GRAPH_NAME_SIMPLE_TOOL

    assert simple_tool_name == GRAPH_NAME_SIMPLE_TOOL
    assert deep_reasoning_name == GRAPH_NAME_DEEP_REASONING


def test_usage_extractor_import_path_stays_compatible() -> None:
    from backend.services.langgraph_chat.handlers.normal_chat_handler import (
        _extract_usage_from_state,
    )
    from backend.services.langgraph_chat.handlers.turn_runtime import extract_usage_from_state

    assert _extract_usage_from_state is extract_usage_from_state
