"""Planner-context tests for artifact tool exposure gating."""

from __future__ import annotations

from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs import tool_execution as tool_execution_module
from agent.tool_runtime import ToolExecutionRequest
from agent.tool_runtime.artifact_tool_policy import (
    ARTIFACT_READ_TOOL_ID,
    ARTIFACT_SEARCH_TOOL_ID,
)


def _base_interactive_state() -> InteractiveState:
    facts = FactsState(
        task_id=42,
        message="continue",
        capability="deep_reasoning",
        metadata={},
    )
    return InteractiveState(facts=facts)


def test_build_planner_context_hides_artifact_tools_when_not_exposed(monkeypatch) -> None:
    interactive = _base_interactive_state()
    request = ToolExecutionRequest(
        capability="deep_reasoning",
        targets=["10.0.0.1"],
        message="continue",
        task_id=42,
        history=[],
        metadata={},
    )

    monkeypatch.setattr(
        tool_execution_module,
        "_get_full_tool_catalog_for_planner",
        lambda _config: [
            "shell.exec",
            ARTIFACT_SEARCH_TOOL_ID,
            ARTIFACT_READ_TOOL_ID,
        ],
    )
    context = tool_execution_module._build_planner_context(interactive, request)
    assert ARTIFACT_SEARCH_TOOL_ID not in context["resolved_tools"]
    assert ARTIFACT_READ_TOOL_ID not in context["resolved_tools"]


def test_build_planner_context_hides_search_even_when_available_in_registry(monkeypatch) -> None:
    interactive = _base_interactive_state()
    request = ToolExecutionRequest(
        capability="deep_reasoning",
        targets=["10.0.0.1"],
        message="need prior output evidence",
        task_id=42,
        history=[],
        metadata={},
    )

    monkeypatch.setattr(
        tool_execution_module,
        "_get_full_tool_catalog_for_planner",
        lambda _config: ["shell.exec"],
    )
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: ["shell.exec", ARTIFACT_SEARCH_TOOL_ID, ARTIFACT_READ_TOOL_ID],
        raising=False,
    )
    context = tool_execution_module._build_planner_context(interactive, request)
    assert ARTIFACT_SEARCH_TOOL_ID not in context["resolved_tools"]
    assert ARTIFACT_READ_TOOL_ID not in context["resolved_tools"]
