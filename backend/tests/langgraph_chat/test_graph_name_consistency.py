"""Regression tests for canonical LangGraph runtime graph names."""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def test_backend_hitl_constants_reexport_agent_graph_names() -> None:
    from agent.graph.graph_names import (
        DEFAULT_GRAPH_NAME,
        GRAPH_NAME_DEEP_REASONING,
        GRAPH_NAME_INTERRUPT_RESUME,
        GRAPH_NAME_NORMAL_CHAT,
        GRAPH_NAME_SIMPLE_TOOL,
        GRAPH_NAME_SUBAGENT,
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
    assert hitl_constants.GRAPH_NAME_SUBAGENT == GRAPH_NAME_SUBAGENT == "subagent"
    assert hitl_constants.DEFAULT_GRAPH_NAME == DEFAULT_GRAPH_NAME == GRAPH_NAME_SIMPLE_TOOL


def test_builder_graph_names_are_runtime_names() -> None:
    from agent.graph.builders.deep_reasoning_builder import GRAPH_NAME as deep_reasoning_name
    from agent.graph.builders.simple_tool_builder import GRAPH_NAME as simple_tool_name
    from agent.graph.graph_names import (
        GRAPH_NAME_DEEP_REASONING,
        GRAPH_NAME_SIMPLE_TOOL,
        GRAPH_NAME_SUBAGENT,
    )
    assert simple_tool_name == GRAPH_NAME_SIMPLE_TOOL
    assert deep_reasoning_name == GRAPH_NAME_DEEP_REASONING
    assert GRAPH_NAME_SUBAGENT == "subagent"


def test_subagent_graph_cache_key_covers_the_complete_definition() -> None:
    from agent.subagents.definition import load_subagent_definitions
    from agent.subagents.runtime.graph import graph_cache_key_for_definition

    definition = next(
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    )
    equivalent = replace(definition)
    changed = replace(definition, instructions=f"{definition.instructions}\nChanged")

    assert graph_cache_key_for_definition(definition) == graph_cache_key_for_definition(
        equivalent
    )
    assert graph_cache_key_for_definition(definition) != graph_cache_key_for_definition(
        changed
    )


def test_subagent_graph_registry_isolates_definitions(monkeypatch) -> None:
    from agent.graph.infrastructure.graph_registry import GraphRegistry
    from agent.subagents.definition import load_subagent_definitions
    from agent.subagents.runtime import graph as graph_runtime

    definition = next(
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    )
    changed = replace(definition, instructions=f"{definition.instructions}\nChanged")

    class FakeGraph:
        def __init__(self, bound_definition):
            self.bound_definition = bound_definition

        def compile(self, *, checkpointer):
            return {
                "definition": self.bound_definition,
                "checkpointer": checkpointer,
            }

    monkeypatch.setattr(
        graph_runtime,
        "build_subagent_graph",
        lambda bound_definition, *, build_only: FakeGraph(bound_definition),
    )
    monkeypatch.setattr(graph_runtime, "get_default_checkpointer", lambda: object())
    registry = GraphRegistry()

    first = graph_runtime.get_compiled_subagent_graph(definition, registry=registry)
    duplicate = graph_runtime.get_compiled_subagent_graph(
        replace(definition),
        registry=registry,
    )
    second = graph_runtime.get_compiled_subagent_graph(changed, registry=registry)

    assert duplicate is first
    assert second is not first
    assert first["definition"] == definition
    assert second["definition"] == changed


def test_usage_extractor_import_path_stays_compatible() -> None:
    from backend.services.langgraph_chat.handlers.normal_chat_handler import (
        _extract_usage_from_state,
    )
    from backend.services.langgraph_chat.handlers.turn_runtime import extract_usage_from_state

    assert _extract_usage_from_state is extract_usage_from_state
