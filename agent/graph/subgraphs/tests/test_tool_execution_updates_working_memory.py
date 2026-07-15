"""Focused tests for tool-execution working-memory integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

# Force full graph package init to sidestep the pre-existing circular import
# between agent.graph.subgraphs.tool_execution and agent.graph.builders.
# Importing builders here loads them before tool_execution, so the
# lookup resolves cleanly.
import agent.graph.builders  # noqa: F401  # side-effect: break the import cycle
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import run_tool_execution
from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome
from tests.tool_execution_module_helper import patch_tool_execution_attr


def _empty_bundle() -> dict[str, Any]:
    """Produce an empty ``ConversationContextBundle`` for direct-node tests.

    Phase 5 cutover: planner / request_context raise ``RuntimeError``
    when ``metadata[context_bundle]`` is missing. Direct-node tests
    that bypass the facade must install one themselves.
    """
    return dict(
        build_conversation_context_bundle(
            conversation_id="conv-tool-exec-test",
            turn_id="turn-tool-exec-test",
            turn_sequence=0,
            messages=[],
        )
    )


class _DummyCompactCompression:
    source = "llm"
    fallback_reason = None


class _DummyCompactResult:
    compression = _DummyCompactCompression()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "tool": "shell.exec",
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": "Command completed successfully.",
            "key_findings": ["stdout contains hello"],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [{"path": "artifact://scan.json", "count": 2}],
            "compression": {"source": "llm", "fallback_reason": None},
        }


class _DummyCompressionResult:
    compact_output = _DummyCompactResult()
    usage_record = None


def _stub_outcome() -> ToolExecutionOutcome:
    return ToolExecutionOutcome(
        tool_id="shell.exec",
        parameters={"command": "echo hello", "target": "127.0.0.1"},
        catalog=[ToolCatalogEntry(tool_id="shell.exec", name="shell", category="shell", description="")],
        result={
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "stdout": "hello\n",
            "stderr": "",
            "observation": "Command completed",
            "exit_code": 0,
            "duration": 0.1,
            "metadata": {
                "host_status": "up",
                "open_ports": [
                    {
                        "port": 80,
                        "protocol": "tcp",
                        "status": "open",
                        "service": "http",
                        "product": "nginx",
                        "version": "1.24",
                    }
                ],
            },
        },
        summary="Command completed",
        reasoning=[],
        duration=0.1,
    )


class _StubCoordinator:
    async def run(self, request):  # noqa: ANN001
        return _stub_outcome()


def _base_facts() -> FactsState:
    return FactsState(
        task_id=1,
        message="Run echo",
        capability="simple_tool_execution",
        metadata={
            "api_key": "key",
            "model": "model",
            "tool_plan_prepared": True,
            "planner_plan": {
                "selected_tools": ["shell.exec"],
                "tool_parameters": {"shell.exec": {"command": "echo hello", "target": "127.0.0.1"}},
                "execution_strategy": "sequential",
                "reasoning": "",
                "expected_outcome": "",
                "tool_batch": {
                    "tool_batch_id": "tb_working_memory",
                    "requested_execution_strategy": "sequential",
                    "tool_calls": [
                        {
                            "tool_call_id": "tc_shell_exec",
                            "tool_id": "shell.exec",
                            "parameters": {
                                "command": "echo hello",
                                "target": "127.0.0.1",
                            },
                            "intent": "Run echo",
                        },
                    ],
                },
            },
            METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle(),
        },
    )


def _base_context() -> GraphRuntimeContext:
    return GraphRuntimeContext(
        task_id=1,
        user_id=1,
        workspace_path="/workspace",
        feature_flags={},
        api_key="key",
        model="model",
    )


@pytest.mark.asyncio
async def test_tool_execution_updates_working_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)
    patch_tool_execution_attr(
        monkeypatch,
        "compress_tool_output",
        AsyncMock(return_value=_DummyCompressionResult()),
    )
    patch_tool_execution_attr(monkeypatch, "compact_output_size_bytes", lambda _: 128)

    state = InteractiveState(facts=_base_facts())
    result = await run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    metadata = updated.facts.metadata

    assert "working_memory" in metadata
    wm = metadata["working_memory"]
    assert wm["tool_state"]["selected_tool"] == "shell.exec"
    assert wm["tool_runs"]
    assert wm["tool_runs"][-1]["tool_id"] == "shell.exec"
    assert wm["collections"]
    assert wm["collections"][-1]["artifact_ref"]["path"] == "artifact://scan.json"
    assert wm["active"]["subject_id"] is not None
    assert wm["active"]["collection_id"] is not None
    assert wm["active"]["target_id"] is not None
    assert any(item["kind"] == "host_up" for item in wm["available_findings"])
    assert any(item["kind"] == "port_open" for item in wm["available_findings"])
    assert any(item["kind"] == "service_detected" for item in wm["available_findings"])
    assert "last_tool_run: shell.exec" in (updated.trace.scratchpad or "")
