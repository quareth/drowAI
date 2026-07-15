"""
Note: Retry and failure recovery tests have moved to post_tool_reasoning tests.
See:
- file:agent/graph/tests/test_post_tool_reasoning_core.py
- file:agent/graph/nodes/post_tool_reasoning/tests/test_failure_detection.py

Simple tool graph now follows a direct execution path without retry logic.
Retry and recovery are handled by the deep reasoning graph via post_tool_reasoning.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.state import FactsState, InteractiveState, TraceState


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def sample_simple_tool_state(
    *,
    success: bool = True,
    tool_id: str = "nmap",
    params: Dict[str, Any] | None = None,
    summary: str | None = None,
    stderr: str = "",
) -> Dict[str, Any]:
    params = params or {tool_id: {"target": "10.0.0.1"}}
    # Compact envelope required by tool_synthesizer and failure_detection
    # Use exit_code 124 for timeout when stderr contains timeout-related text
    exit_code = 0 if success else (124 if "timeout" in stderr.lower() else 1)
    last_tool_result_compact = {
        "schema_version": "2.0",
        "tool": tool_id,
        "status": "success" if success else "failed",
        "success": success,
        "exit_code": exit_code,
        "summary": summary or ("scan ok" if success else "scan failed"),
        "key_findings": ["port 22 open"] if success else [],
        "errors": [stderr] if stderr else [],
        "artifact_refs": [],
        "report_recommendations": [],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "medium" if not success else "low",
    }
    facts = FactsState(
        task_id=1,
        message="scan target",
        capability="simple_tool_execution",
        selected_tool=tool_id,
        tool_parameters=params,
        metadata={
            "api_key": "test-key",
            "model": "gpt-4o-mini",
            "last_tool_result": {
                "tool": tool_id,
                "success": success,
                "status": "success" if success else "failed",
                "stdout_excerpt": "open 22/tcp" if success else "",
                "stderr": stderr,
            },
            "last_tool_result_compact": last_tool_result_compact,
            "synthesized_output": {
                "success": success,
                "status": "success" if success else "failed",
                "summary": summary or ("scan ok" if success else "scan failed"),
                "key_findings": ["port 22 open"] if success else [],
            },
        },
    )
    trace = TraceState(reasoning=["classify"], observations=[])
    return InteractiveState(facts=facts, trace=trace).as_graph_state()


def set_metadata_field(state: Dict[str, Any], key: str, value: Any) -> Dict[str, Any]:
    interactive = InteractiveState.from_mapping(state)
    interactive.facts.metadata[key] = value
    return interactive.as_graph_update()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _noop_return_state(state, **_kwargs):
    return state


async def _noop_return_state_async(state, **_kwargs):
    return state


def _set_validation_ready(state, **_kwargs):
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.metadata or {}
    metadata["working_memory"] = {
        "validation": {"is_ready": True, "missing": [], "errors": []},
        "open_questions": [],
    }
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


@pytest.mark.asyncio
async def test_simple_tool_synthesizer_integration():
    """Synthesizer should enrich metadata for downstream nodes."""
    graph = build_simple_tool_graph()
    initial_state = sample_simple_tool_state(success=True)

    async def synthesize(state, **_kwargs):
        return set_metadata_field(
            state,
            "synthesized_output",
            {"success": True, "status": "success", "summary": "enriched summary", "key_findings": ["k1"]},
        )

    async def _post_tool_format_async(state, **_kwargs):
        """Set decision to format_results so graph proceeds without retry."""
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=_noop_return_state), \
        patch("agent.graph.builders.simple_tool_builder.update_working_memory_node", side_effect=_set_validation_ready), \
        patch("agent.graph.builders.simple_tool_builder.select_tool_categories_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.articulate_tool_intent", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.approval_gate_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.synthesize_tool_output", new_callable=AsyncMock, side_effect=synthesize), \
        patch("agent.graph.builders.simple_tool_builder.post_tool_reasoning", new_callable=AsyncMock, side_effect=_post_tool_format_async), \
        patch("agent.graph.builders.simple_tool_builder.finalize_results", new_callable=AsyncMock, side_effect=_noop_return_state_async):
        final_state = None
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "synth-int"}}, stream_mode="values"
        ):
            final_state = event

    interactive = InteractiveState.from_mapping(final_state)
    synth = interactive.facts.metadata["synthesized_output"]
    assert synth["summary"] == "enriched summary"
    assert synth["key_findings"] == ["k1"]


@pytest.mark.asyncio
async def test_simple_tool_format_results_integration():
    """format_results node should stream structured data without retry metadata."""
    graph = build_simple_tool_graph()
    initial_state = sample_simple_tool_state(success=True, summary="initial")

    async def format_results(state, **_kwargs):
        return set_metadata_field(
            state,
            "formatted_result",
            {"status": "ok", "cards": [{"type": "tool_result", "summary": "ok"}]},
        )

    async def _post_tool_format_async(state, **_kwargs):
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=_noop_return_state), \
        patch("agent.graph.builders.simple_tool_builder.update_working_memory_node", side_effect=_set_validation_ready), \
        patch("agent.graph.builders.simple_tool_builder.select_tool_categories_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.articulate_tool_intent", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.approval_gate_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.synthesize_tool_output", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.post_tool_reasoning", new_callable=AsyncMock, side_effect=_post_tool_format_async), \
        patch("agent.graph.builders.simple_tool_builder.finalize_results", new_callable=AsyncMock, side_effect=format_results):
        final_state = None
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "format-int"}}, stream_mode="values"
        ):
            final_state = event

    interactive = InteractiveState.from_mapping(final_state)
    assert interactive.facts.metadata["formatted_result"]["status"] == "ok"
    assert "retry_tracking" not in interactive.facts.metadata


@pytest.mark.asyncio
async def test_simple_tool_end_to_end_success():
    """End-to-end simple tool flow completes successfully without retry nodes."""
    graph = build_simple_tool_graph()
    invoked: List[str] = []

    def track_sync(name: str):
        def _inner(state, **_kwargs):
            invoked.append(name)
            return state
        return _inner

    async def track_async(name: str):
        async def _inner(state, **_kwargs):
            invoked.append(name)
            return state
        return _inner

    async def _post_tool_track(state, **_kwargs):
        invoked.append("post_tool_reasoning")
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    initial_state = sample_simple_tool_state(success=True)
    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=track_sync("classification")), \
        patch("agent.graph.builders.simple_tool_builder.update_working_memory_node", side_effect=_set_validation_ready), \
        patch("agent.graph.builders.simple_tool_builder.select_tool_categories_node", new_callable=AsyncMock, side_effect=await track_async("select_tool_categories")), \
        patch("agent.graph.builders.simple_tool_builder.articulate_tool_intent", new_callable=AsyncMock, side_effect=await track_async("articulation")), \
        patch("agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan", new_callable=AsyncMock, side_effect=await track_async("prepare_tool_plan")), \
        patch("agent.graph.builders.simple_tool_builder.approval_gate_node", new_callable=AsyncMock, side_effect=await track_async("approval_gate")), \
        patch("agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node", new_callable=AsyncMock, side_effect=await track_async("dispatch_tool")), \
        patch("agent.graph.builders.simple_tool_builder.synthesize_tool_output", new_callable=AsyncMock, side_effect=await track_async("tool_synthesizer")), \
        patch("agent.graph.builders.simple_tool_builder.post_tool_reasoning", new_callable=AsyncMock, side_effect=_post_tool_track), \
        patch("agent.graph.builders.simple_tool_builder.finalize_results", new_callable=AsyncMock, side_effect=await track_async("format_results")), \
        patch("agent.graph.builders.simple_tool_builder.finalize_turn", side_effect=track_sync("finalize")):
        final_state = None
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "success-e2e"}}, stream_mode="values"
        ):
            final_state = event

    expected = [
        "classification",
        "select_tool_categories",
        "prepare_tool_plan",
        "articulation",
        "approval_gate",
        "dispatch_tool",
        "tool_synthesizer",
        "post_tool_reasoning",
        "format_results",
        "finalize",
    ]
    assert invoked == expected
    assert final_state is not None
    assert "retry_tracking" not in InteractiveState.from_mapping(final_state).facts.metadata


@pytest.mark.asyncio
async def test_simple_tool_end_to_end_with_failure():
    """End-to-end failure completes without retry and preserves failure metadata."""
    graph = build_simple_tool_graph()
    invoked: List[str] = []
    initial_state = sample_simple_tool_state(success=False, stderr="network down")

    def track_sync(name: str):
        def _inner(state, **_kwargs):
            invoked.append(name)
            return state
        return _inner

    async def track_async(name: str):
        async def _inner(state, **_kwargs):
            invoked.append(name)
            return state
        return _inner

    async def _post_tool_track_fail(state, **_kwargs):
        invoked.append("post_tool_reasoning")
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=track_sync("classification")), \
        patch("agent.graph.builders.simple_tool_builder.update_working_memory_node", side_effect=_set_validation_ready), \
        patch("agent.graph.builders.simple_tool_builder.select_tool_categories_node", new_callable=AsyncMock, side_effect=await track_async("select_tool_categories")), \
        patch("agent.graph.builders.simple_tool_builder.articulate_tool_intent", new_callable=AsyncMock, side_effect=await track_async("articulation")), \
        patch("agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan", new_callable=AsyncMock, side_effect=await track_async("prepare_tool_plan")), \
        patch("agent.graph.builders.simple_tool_builder.approval_gate_node", new_callable=AsyncMock, side_effect=await track_async("approval_gate")), \
        patch("agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node", new_callable=AsyncMock, side_effect=await track_async("dispatch_tool")), \
        patch("agent.graph.builders.simple_tool_builder.synthesize_tool_output", new_callable=AsyncMock, side_effect=await track_async("tool_synthesizer")), \
        patch("agent.graph.builders.simple_tool_builder.post_tool_reasoning", new_callable=AsyncMock, side_effect=_post_tool_track_fail), \
        patch("agent.graph.builders.simple_tool_builder.finalize_results", new_callable=AsyncMock, side_effect=await track_async("format_results")), \
        patch("agent.graph.builders.simple_tool_builder.finalize_turn", side_effect=track_sync("finalize")):
        final_state = None
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "failure-e2e"}}, stream_mode="values"
        ):
            final_state = event

    assert invoked[-1] == "finalize"
    interactive = InteractiveState.from_mapping(final_state)
    assert interactive.facts.metadata["last_tool_result"]["success"] is False
    assert "retry_tracking" not in interactive.facts.metadata


@pytest.mark.asyncio
async def test_simple_tool_metadata_flow():
    """Metadata (api_key, model, synthesized output) flows through all nodes."""
    graph = build_simple_tool_graph()
    initial_state = sample_simple_tool_state(success=True, summary="orig summary")

    async def synthesize(state, **_kwargs):
        return set_metadata_field(
            state,
            "synthesized_output",
            {"success": True, "status": "success", "summary": "updated summary", "key_findings": ["f1"]},
        )

    async def finalize(state, **_kwargs):
        return set_metadata_field(state, "finalized", True)

    async def _post_tool_format_async(state, **_kwargs):
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=_noop_return_state), \
        patch("agent.graph.builders.simple_tool_builder.update_working_memory_node", side_effect=_set_validation_ready), \
        patch("agent.graph.builders.simple_tool_builder.select_tool_categories_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.articulate_tool_intent", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.approval_gate_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node", new_callable=AsyncMock, side_effect=_noop_return_state_async), \
        patch("agent.graph.builders.simple_tool_builder.synthesize_tool_output", new_callable=AsyncMock, side_effect=synthesize), \
        patch("agent.graph.builders.simple_tool_builder.post_tool_reasoning", new_callable=AsyncMock, side_effect=_post_tool_format_async), \
        patch("agent.graph.builders.simple_tool_builder.finalize_results", new_callable=AsyncMock, side_effect=finalize):
        final_state = None
        async for event in graph.astream(
            initial_state, {"configurable": {"thread_id": "meta-flow"}}, stream_mode="values"
        ):
            final_state = event

    interactive = InteractiveState.from_mapping(final_state)
    assert interactive.facts.metadata["api_key"] == "test-key"
    assert interactive.facts.metadata["model"] == "gpt-4o-mini"
    assert interactive.facts.metadata["synthesized_output"]["summary"] == "updated summary"
    assert interactive.facts.metadata["finalized"] is True
    assert "retry_tracking" not in interactive.facts.metadata


@pytest.mark.asyncio
async def test_simple_tool_failure_passed_to_post_tool_reasoning():
    """Test that failures are passed to post_tool_reasoning for decision making.
    
    Simple tool graph completes with failure state intact. The DR graph's
    post_tool_reasoning node then analyzes the failure and decides on retry.
    """
    from agent.graph.nodes.post_tool_reasoning.node import _detect_tool_failure, _can_retry, _increment_retry_count, _get_retry_count
    from agent.graph.nodes.post_tool_reasoning.models import PostToolReasoningOutput, ToolIntent
    
    # Simulate simple tool graph completing with failure (stderr with "timeout" + exit_code 124)
    failure_state_dict = sample_simple_tool_state(
        success=False,
        stderr="connection timeout after 30s",
        summary="Scan timed out after 30s",
    )
    
    interactive = InteractiveState.from_mapping(failure_state_dict)
    
    # Verify failure detection works
    failure_detected, failure_category = _detect_tool_failure(interactive)
    assert failure_detected is True
    assert failure_category == "timeout"
    
    # Verify retry budget available
    assert _can_retry(interactive) is True
    
    # Simulate post_tool_reasoning deciding to retry
    ptr_output = PostToolReasoningOutput(
        observation="The scan timed out. I will retry with a longer timeout.",
        next_action="call_tool",
        action_reasoning="Timeout suggests network latency; retry with extended timeout",
        tool_intent=ToolIntent(
            description="Retry scan with longer timeout",
            target="10.0.0.1",
            focus="port scan",
        ),
        user_goal_achieved=False,
        todo_progress=[],
        effective_next_goal=None,
        failure_detected=True,
        failure_category="timeout",
        retry_suggested=True,
    )
    
    # Verify PTR output structure
    assert ptr_output.failure_detected is True
    assert ptr_output.failure_category == "timeout"
    assert ptr_output.retry_suggested is True
    assert ptr_output.next_action == "call_tool"
    assert ptr_output.tool_intent is not None
    
    # Simulate retry count increment (done by PTR node)
    _increment_retry_count(interactive)
    assert _get_retry_count(interactive) == 1
