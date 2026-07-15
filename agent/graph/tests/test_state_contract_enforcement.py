"""CI guardrail tests that enforce compact-only tool-output state contracts."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution import run_tool_execution
from agent.graph.tests._state_assertions import (
    FORBIDDEN_RAW_OUTPUT_KEYS,
    assert_no_raw_tool_output_in_state,
)
from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from tests.tool_execution_module_helper import patch_tool_execution_attr


def _stub_coordinator_outcome() -> ToolExecutionOutcome:
    """Return a tool result payload that contains raw fields pre-sanitization."""
    return ToolExecutionOutcome(
        tool_id="shell.exec",
        parameters={"command": "echo hello"},
        catalog=[ToolCatalogEntry(tool_id="shell.exec", name="shell", category="shell", description="")],
        result={
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "stdout": "hello\nraw stdout leak marker",
            "stderr": "raw stderr leak marker",
            "stdout_excerpt": "raw stdout leak marker",
            "stderr_excerpt": "raw stderr leak marker",
            "observation": "Command completed",
            "exit_code": 0,
        },
        summary="Command completed",
        reasoning=[],
        duration=0.1,
    )


class _StubCoordinator:
    async def run(self, request):  # noqa: ANN001
        return _stub_coordinator_outcome()


def _base_facts() -> FactsState:
    """Create base facts with planner output to bypass planner node."""
    return FactsState(
        task_id=1,
        message="Run echo",
        capability="simple_tool_execution",
        intent_hints={"targets": ["localhost"]},
        metadata={
            "api_key": "key",
            "model": "model",
            "tool_plan_prepared": True,
            "planner_plan": {
                "selected_tools": ["shell.exec"],
                "tool_parameters": {"shell.exec": {"command": "echo hello"}},
                "execution_strategy": "sequential",
                "reasoning": "",
                "expected_outcome": "",
                "tool_batch": {
                    "tool_batch_id": "tb-state-contract",
                    "tool_calls": [
                        {
                            "tool_call_id": "tc-state-contract-shell",
                            "tool_id": "shell.exec",
                            "parameters": {"command": "echo hello"},
                            "intent": "Run echo",
                        }
                    ],
                    "requested_execution_strategy": "sequential",
                    "deferred_followups": [],
                    "selection_rationale": "state contract fixture",
                },
            },
            # Phase 5 cutover: the hot-path ConversationContextBundle is
            # required by the tool-execution request-context builder.
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-state-contract",
                turn_id="turn-state-contract",
                turn_sequence=0,
                messages=[],
            ),
        },
    )


def _base_context() -> GraphRuntimeContext:
    """Create runtime context required by run_tool_execution."""
    return GraphRuntimeContext(
        task_id=1,
        user_id=1,
        workspace_path="/workspace",
        feature_flags={},
        api_key="key",
        model="model",
    )


def _patch_tool_execution_for_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch tool coordinator and stream writer for deterministic execution tests."""
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)


@pytest.mark.asyncio
async def test_graph_execution_state_never_leaks_raw_tool_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph execution state must strip raw stdout/stderr and excerpt fields."""
    _patch_tool_execution_for_contract(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)

    assert_no_raw_tool_output_in_state(updated.facts.metadata)


@pytest.mark.asyncio
async def test_tool_history_result_is_sanitized_in_compact_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each tool_history entry result should exclude forbidden raw keys."""
    _patch_tool_execution_for_contract(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    history = updated.facts.metadata.get("tool_history", [])

    assert isinstance(history, list)
    assert history
    first_result = history[0].get("result", {})
    assert isinstance(first_result, dict)
    assert FORBIDDEN_RAW_OUTPUT_KEYS.isdisjoint(first_result.keys())


def test_post_tool_prompt_never_injects_raw_output_content() -> None:
    """Prompt builder should include compact summaries and exclude raw-output excerpts."""
    builder = PostToolReasoningPromptBuilder()
    leak_marker_stdout = "raw stdout leak marker"
    leak_marker_stderr = "raw stderr leak marker"

    interactive: Dict[str, Any] = {
        "facts": {
            "message": "Analyze scan output",
            "capability": "deep_reasoning",
            "selected_tool": "shell.exec",
            "metadata": {
                "last_tool_result": {
                    "parameters": {"command": "echo hello"},
                    "stdout_excerpt": leak_marker_stdout,
                    "stderr_excerpt": leak_marker_stderr,
                    "was_truncated": False,
                    "chars_truncated": 0,
                    "suggest_file_reading": False,
                },
                "last_tool_result_compact": {
                    "summary": "Command printed hello and exited successfully.",
                    "key_findings": ["stdout contains hello"],
                    "errors": [],
                    "report_recommendations": ["Continue to next verification step"],
                },
            },
        }
    }
    synthesized = {
        "tool": "shell.exec",
        "summary": "Command printed hello and exited successfully.",
        "key_findings": ["stdout contains hello"],
        "vulnerabilities": [],
        "next_actions": ["Continue to next verification step"],
    }

    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized=synthesized,
        failure_context={},
        environment_context="",
    )

    assert "## Tool Output Summary" in prompt
    assert "## Raw Output Excerpt" not in prompt
    assert leak_marker_stdout not in prompt
    assert leak_marker_stderr not in prompt


@pytest.mark.asyncio
async def test_serialized_state_contains_no_forbidden_raw_tool_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serialized graph state should preserve compact-only metadata contract."""
    _patch_tool_execution_for_contract(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await run_tool_execution(state.as_graph_state(), context=_base_context())
    serialized = InteractiveState.from_mapping(result).as_graph_state()

    facts = serialized.get("facts", {})
    metadata = facts.get("metadata", {})
    assert isinstance(metadata, dict)
    assert_no_raw_tool_output_in_state(metadata)
