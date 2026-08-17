"""Regression tests for the canonical tool-execution subgraph state updates."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import run_tool_execution
from agent.tool_runtime import ToolExecutionOutcome, ToolCatalogEntry
from tests.tool_execution_module_helper import patch_tool_execution_attr


class _StubCoordinator:
    async def run(self, request):  # noqa: ANN001
        catalog = [ToolCatalogEntry(tool_id="information_gathering.network_discovery.nmap", name="nmap", category="network", description="")]
        return ToolExecutionOutcome(
            tool_id="information_gathering.network_discovery.nmap",
            parameters={"target": request.targets[0], "ports": "1-1024"},
            catalog=catalog,
            result={
                "tool": "information_gathering.network_discovery.nmap",
                "success": True,
                "stdout_excerpt": "Scan complete",
                "stderr_excerpt": "",
                "observation": "Open ports discovered",
                "status": "success",
            },
            summary="Summarised output",
            reasoning=["Planner reasoning"],
            duration=0.1,
        )


@pytest.mark.asyncio
async def test_run_tool_execution_updates_state(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=42,
        message="Run nmap",
        capability="simple_tool_execution",
        selected_tool="information_gathering.network_discovery.nmap",
        tool_parameters={
            "information_gathering.network_discovery.nmap": {"target": "10.0.0.1", "ports": "1-1024"}
        },
        intent_hints={"targets": ["10.0.0.1"]},
        metadata={
            "api_key": "key",
            "model": "model",
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="tool-exec-test-conv",
                turn_id="turn-1",
                turn_sequence=1,
                messages=[{"role": "user", "content": "Run nmap"}],
                current_message="Run nmap",
            ),
            "tool_plan_prepared": True,
            "planner_plan": {
                "selected_tools": ["information_gathering.network_discovery.nmap"],
                "tool_parameters": {
                    "information_gathering.network_discovery.nmap": {
                        "target": "10.0.0.1",
                        "ports": "1-1024",
                    }
                },
                "execution_strategy": "sequential",
                "reasoning": "",
                "expected_outcome": "",
                "tool_batch": {
                    "tool_batch_id": "tb-tool-exec-test",
                    "requested_execution_strategy": "sequential",
                    "deferred_followups": [],
                    "selection_rationale": "test canonical batch",
                    "tool_calls": [
                        {
                            "tool_call_id": "tc-nmap",
                            "tool_id": "information_gathering.network_discovery.nmap",
                            "parameters": {
                                "target": "10.0.0.1",
                                "ports": "1-1024",
                            },
                            "intent": "run nmap",
                        }
                    ],
                },
            },
        },
    )
    state = InteractiveState(facts=facts)

    context = GraphRuntimeContext(task_id=42, user_id=1, workspace_path="/workspace", feature_flags={}, model="model")

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert updated.facts.selected_tool == "information_gathering.network_discovery.nmap"
    assert updated.facts.metadata["last_tool_result"]["parameters"]["target"] == "10.0.0.1"
    assert updated.facts.metadata["last_tool_result"]["status"] == "success"
    assert updated.trace.executed_tools[-1].tool_id == "information_gathering.network_discovery.nmap"
