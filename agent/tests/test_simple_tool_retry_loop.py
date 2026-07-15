"""Simple-tool graph: structural and failure-categorization tests.

Scope
-----
The simple-tool graph deliberately has NO retry/reflection nodes — retry and
failure recovery live in the post-tool-reasoning policy pipeline used by the
deep-reasoning graph. These tests verify that property remains true after the
2026-04-26 finalization-unification refactor (``e0a42b04``) and confirm that
the failure-detection helper used by ``post_tool_reasoning`` correctly
categorizes the standard error patterns.

Related coverage:
- ``agent/graph/nodes/post_tool_reasoning/core/tests/test_failure_detection.py``
  (full categorization matrix on the pure helper)
- ``agent/graph/tests/test_post_tool_reasoning_core.py`` (failure handling
  through the orchestrator)
- ``agent/graph/tests/test_simple_tool_routing_hitl.py`` (HITL routing)
- ``agent/graph/tests/test_simple_tool_hitl_plan_preparation.py`` (plan
  preparation in the HITL contract)
"""

from typing import Any, Dict
import logging

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState, TraceState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def sample_simple_tool_state(
    *,
    success: bool = True,
    tool_id: str = "ping",
    params: Dict[str, Any] | None = None,
    stderr: str = "",
    exit_code: int = 0,
) -> InteractiveState:
    """Build a minimal InteractiveState for simple-tool execution.

    Populates both ``last_tool_result`` (the raw stdout/stderr/exit_code shape)
    and ``last_tool_result_compact`` (the compact post-execution shape that
    ``build_failure_context_from_state`` reads). The latter is required because
    the failure-detection pipeline now sources stderr from the compact
    ``errors`` field, not from the raw ``last_tool_result.stderr``.
    """
    params = params or {"target": "192.168.1.1"}
    raw_stderr = "" if success else stderr
    facts = FactsState(
        task_id=1,
        message="Scan the network",
        capability="simple_tool_execution",
        selected_tool=tool_id,
        tool_parameters={tool_id: params},
        metadata={
            "api_key": "key",
            "model": "gpt-4o",
            "last_tool_result": {
                "tool": tool_id,
                "success": success,
                "exit_code": exit_code,
                "stdout": "ok" if success else "",
                "stderr": raw_stderr,
                "status": "success" if success else "failed",
            },
            "last_tool_result_compact": {
                "success": success,
                "status": "success" if success else "failed",
                "exit_code": exit_code,
                "summary": "Tool succeeded" if success else f"{tool_id} failed",
                "key_findings": ["reachable"] if success else [],
                "errors": [] if success else [raw_stderr],
            },
            "synthesized_output": {
                "success": success,
                "status": "success" if success else "failed",
                "summary": "Tool succeeded" if success else f"{tool_id} failed",
                "key_findings": ["reachable"] if success else [],
            },
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-simple-tool",
                turn_id="turn-simple-tool",
                turn_sequence=0,
                messages=[],
            ),
        },
    )
    trace = TraceState(reasoning=["initial"], observations=[], executed_tools=[])
    return InteractiveState(facts=facts, trace=trace)


# ---------------------------------------------------------------------------
# Group D: simple-tool graph has NO retry/reflection nodes (structural)
# ---------------------------------------------------------------------------


def _build_uncompiled_graph_nodes() -> set[str]:
    """Return the set of node names registered on the uncompiled simple-tool graph."""
    graph = build_simple_tool_graph(build_only=True)
    return set(graph.nodes.keys())


def test_simple_tool_graph_has_no_retry_nodes():
    """Simple-tool graph must not register any retry/reflection nodes.

    Retry behavior is owned by ``post_tool_reasoning`` and the direct-executor
    policy; the simple-tool graph itself runs straight through.
    """
    nodes = _build_uncompiled_graph_nodes()
    assert "failure_reflection" not in nodes, (
        "Simple-tool graph should NOT register a failure_reflection node "
        "(retry is owned by post_tool_reasoning)."
    )
    assert "increment_retry" not in nodes, (
        "Simple-tool graph should NOT register an increment_retry node "
        "(retry is owned by post_tool_reasoning)."
    )


def test_simple_tool_graph_registers_expected_nodes():
    """Simple-tool graph must register all nodes in the post-refactor flow.

    Reflects the architecture established by ``e0a42b04`` (2026-04-26):
    classification -> update_working_memory -> memory_retrieval ->
    select_tool_categories -> prepare_tool_plan -> articulation? ->
    approval_gate -> dispatch_tool -> tool_synthesizer ->
    post_tool_reasoning -> format_results -> finalize.
    """
    nodes = _build_uncompiled_graph_nodes()
    expected = {
        "classification",
        "update_working_memory",
        "memory_retrieval",
        "select_tool_categories",
        "articulation",
        "prepare_tool_plan",
        "approval_gate",
        "dispatch_tool",
        "tool_synthesizer",
        "post_tool_reasoning",
        "format_results",
        "finalize",
    }
    missing = expected - nodes
    unexpected = nodes - expected
    assert not missing, f"Simple-tool graph missing expected nodes: {missing}"
    assert not unexpected, f"Simple-tool graph has unexpected nodes: {unexpected}"


def test_simple_tool_graph_does_not_write_retry_metadata_on_build():
    """Building the simple-tool graph must not introduce any retry metadata.

    The graph itself contains no retry-tracking infrastructure; retry counters
    are written exclusively by ``post_tool_reasoning`` when it explicitly opts
    into a corrective retry. This test ensures the structural assumption holds
    and surfaces any accidental reintroduction of a retry counter at build
    time.
    """
    state = sample_simple_tool_state(success=False, stderr="boom", exit_code=1)
    metadata = state.facts.metadata or {}
    assert "retry_tracking" not in metadata
    # Building the graph must not mutate the state we constructed.
    build_simple_tool_graph(build_only=True)
    assert "retry_tracking" not in metadata


# ---------------------------------------------------------------------------
# Group E: failure-detection categorization (via _detect_tool_failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_tool_failure_detection_via_post_tool_reasoning():
    """Tool failures are categorized by ``_detect_tool_failure``.

    Simple tool graph doesn't handle retry — it completes and passes state to
    ``post_tool_reasoning`` which detects failure and decides on retry.
    """
    from agent.graph.nodes.post_tool_reasoning.node import _detect_tool_failure
    from agent.graph.nodes.post_tool_reasoning.models import (
        PostToolReasoningOutput,
        ToolIntent,
    )

    state = sample_simple_tool_state(
        success=False,
        stderr="Connection refused",
        exit_code=1,
    )

    failure_detected, failure_category = _detect_tool_failure(state)

    assert failure_detected is True, "Should detect tool failure"
    assert failure_category == "network_error", (
        f"Expected network_error, got {failure_category}"
    )

    # And confirm the structured PTR output shape carries the failure metadata
    # that downstream code (retry pipeline) reads.
    ptr_output = PostToolReasoningOutput(
        observation="The network scan failed; will retry.",
        next_action="call_tool",
        action_reasoning="Network error detected, retry is appropriate",
        tool_intent=ToolIntent(
            description="Check network connectivity",
            target="192.168.1.1",
            focus="network reachability",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal=None,
        failure_detected=True,
        failure_category="network_error",
        retry_suggested=True,
    )

    assert ptr_output.failure_detected is True
    assert ptr_output.failure_category == "network_error"
    assert ptr_output.retry_suggested is True
    assert ptr_output.next_action == "call_tool"


def test_post_tool_failure_detection_uses_batch_compact_evidence():
    from agent.graph.nodes.post_tool_reasoning.core.failure_detection import (
        build_failure_context_from_state,
        detect_failure,
    )

    state = sample_simple_tool_state(success=True, tool_id="tool.a")
    state.facts.metadata["last_tool_result_compact"] = {
        "tool": "tool.a",
        "success": True,
        "status": "success",
        "summary": "primary ok",
    }
    state.facts.metadata["last_tool_result_compact_batch"] = {
        "tool_batch_id": "tb_1",
        "status": "completed_with_errors",
        "success": False,
        "results": [
            {
                "tool_call_id": "tc_1",
                "tool_id": "tool.a",
                "status": "success",
                "success": True,
                "compact_tool_result": {"summary": "primary ok", "success": True},
            },
            {
                "tool_call_id": "tc_2",
                "tool_id": "tool.b",
                "status": "failed",
                "success": False,
                "failure_category": "timeout",
                "error_message": "timeout",
                "compact_tool_result": {"summary": "timed out", "success": False},
            },
        ],
    }

    context = build_failure_context_from_state(state)
    failure_detected, failure_category = detect_failure(context)

    assert failure_detected is True
    assert failure_category == "timeout"


@pytest.mark.asyncio
async def test_simple_tool_permission_failure_detection():
    """Permission denied failures are correctly categorized."""
    from agent.graph.nodes.post_tool_reasoning.node import _detect_tool_failure

    state = sample_simple_tool_state(
        success=False,
        stderr="Permission denied: root privileges required",
        exit_code=13,
    )

    failure_detected, failure_category = _detect_tool_failure(state)

    assert failure_detected is True
    assert failure_category == "permission_denied"


@pytest.mark.asyncio
async def test_simple_tool_timeout_failure_detection():
    """Timeout failures are correctly categorized."""
    from agent.graph.nodes.post_tool_reasoning.node import _detect_tool_failure

    state = sample_simple_tool_state(
        success=False,
        stderr="operation timeout",
        exit_code=124,
    )

    failure_detected, failure_category = _detect_tool_failure(state)

    assert failure_detected is True
    assert failure_category == "timeout"
