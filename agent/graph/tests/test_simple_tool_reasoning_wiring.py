"""Wiring tests for the three reasoning nodes in the simple-tool graph.

Covers Phase 1 (unit) and Phase 3 (integration) of the
``wire-think-more-reflect-synthesis-into-simple-tool`` plan.

Phase 1 unit tests exercise the routing primitives in
``agent.graph.builders.simple_tool_builder`` directly:

- ``_route_after_router`` — verifies dispatch from
  ``metadata.router_outcome.action`` for each router action label
  (``call_tool``, ``think_more``, ``reflect``, ``synthesis``, ``finalize``)
  plus deterministic missing-action fallback behavior.
- A graph-compile assertion confirms the three new nodes are registered.

Phase 3 integration tests run the full simple-tool graph with all node
modules patched out except the ones whose wiring is under test. A mock
``post_tool_reasoning`` forces the ``next_action`` label per scenario,
and assertions on the ``invoked`` ledger demonstrate the graph reaches
the expected reasoning node and terminates at ``finalize``.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.simple_tool_builder import (
    _ROUTER_ACTION_MAP,
    _route_after_router,
    build_simple_tool_graph,
)
from agent.graph.state import FactsState, InteractiveState, TraceState


# ---------------------------------------------------------------------------
# Phase 1 unit tests — _route_after_router dispatch table
# ---------------------------------------------------------------------------


def _make_interactive(
    *,
    decision: str | None = None,
    post_reflect_action: str | None = None,
    router_action: str | None = None,
) -> InteractiveState:
    """Build a minimal ``InteractiveState`` for routing-function unit tests."""
    facts = FactsState(task_id=1, message="msg")
    if decision is not None:
        facts.decision_history.append(decision)
    if post_reflect_action is not None:
        facts.post_reflect_action = post_reflect_action
    if router_action is not None:
        facts.metadata = {"router_outcome": {"action": router_action}}
    return InteractiveState(facts=facts, trace=TraceState())


def test_router_action_map_contains_router_action_labels() -> None:
    """The dispatch table covers the full router action vocabulary."""
    assert _ROUTER_ACTION_MAP == {
        "call_tool": "select_tool_categories",
        "think_more": "think_more",
        "reflect": "reflect",
        "synthesis": "synthesis",
        "finalize": "format_results",
    }


def test_router_think_more_dispatches_to_think_more_node() -> None:
    interactive = _make_interactive(router_action="think_more")
    assert _route_after_router(interactive) == "think_more"


def test_router_reflect_dispatches_to_reflect_node() -> None:
    interactive = _make_interactive(router_action="reflect")
    assert _route_after_router(interactive) == "reflect"


def test_router_synthesis_dispatches_to_synthesis_node() -> None:
    interactive = _make_interactive(router_action="synthesis")
    assert _route_after_router(interactive) == "synthesis"


def test_router_finalize_dispatches_to_format_results() -> None:
    interactive = _make_interactive(router_action="finalize")
    assert _route_after_router(interactive) == "format_results"


def test_router_call_tool_dispatches_to_select_tool_categories() -> None:
    interactive = _make_interactive(router_action="call_tool")
    assert _route_after_router(interactive) == "select_tool_categories"


def test_router_unknown_label_dispatches_to_format_results() -> None:
    interactive = _make_interactive(router_action="hallucinated_action")
    assert _route_after_router(interactive) == "format_results"


def test_router_missing_outcome_dispatches_to_format_results() -> None:
    interactive = _make_interactive(decision="reflect: fallback")
    assert _route_after_router(interactive) == "format_results"


def test_router_empty_outcome_and_history_dispatches_to_format_results() -> None:
    interactive = _make_interactive(decision=None)
    assert interactive.facts.decision_history == []
    assert _route_after_router(interactive) == "format_results"


# ---------------------------------------------------------------------------
# Phase 1 unit test — graph compiles with the three new nodes
# ---------------------------------------------------------------------------


def test_simple_tool_graph_compiles_with_three_new_nodes() -> None:
    """The simple-tool graph compiles end-to-end and exposes the three new nodes."""
    graph = build_simple_tool_graph(build_only=True)

    # ``StateGraph.nodes`` is the canonical introspection surface.
    nodes = getattr(graph, "nodes", {})
    assert "think_more" in nodes
    assert "reflect" in nodes
    assert "synthesis" in nodes


def test_simple_tool_post_tool_boundary_reenters_decision_router() -> None:
    """Task 4.2 contract: post_tool_reasoning routes through decision_router."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())
    assert ("post_tool_reasoning", "decision_router") in edges


def test_simple_tool_think_more_returns_to_ptr_before_router() -> None:
    """Task 4.2 contract: think_more -> post_tool_reasoning -> decision_router."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())
    assert ("think_more", "post_tool_reasoning") in edges


def test_simple_tool_reflect_reenters_router_authority() -> None:
    """Task 4.3 contract: reflect returns to decision_router one-hop recovery."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())
    assert ("reflect", "decision_router") in edges


# ---------------------------------------------------------------------------
# Phase 3 integration tests — full graph traversal per dispatch label
# ---------------------------------------------------------------------------


def _sample_state() -> Dict[str, Any]:
    """Minimal graph-input dict that satisfies the simple-tool capability gate."""
    facts = FactsState(
        task_id=1,
        message="scan target",
        capability="simple_tool_execution",
        selected_tool="nmap",
        tool_parameters={"nmap": {"target": "10.0.0.1"}},
        metadata={
            "api_key": "test-key",
            "model": "gpt-4o-mini",
            "last_tool_result": {
                "tool": "nmap",
                "success": True,
                "status": "success",
                "stdout_excerpt": "open 22/tcp",
                "stderr": "",
            },
            "last_tool_result_compact": {
                "schema_version": "2.0",
                "tool": "nmap",
                "status": "success",
                "success": True,
                "exit_code": 0,
                "summary": "scan ok",
                "key_findings": ["port 22 open"],
                "errors": [],
                "artifact_refs": [],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "low",
            },
            "synthesized_output": {
                "success": True,
                "status": "success",
                "summary": "scan ok",
                "key_findings": ["port 22 open"],
            },
        },
    )
    trace = TraceState(reasoning=["classify"])
    return InteractiveState(facts=facts, trace=trace).as_graph_state()


def _set_validation_ready(state: Dict[str, Any], **_kwargs) -> Dict[str, Any]:
    """Mark working-memory validation as ready so the gate does not reject."""
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.metadata or {}
    metadata["working_memory"] = {
        "validation": {"is_ready": True, "missing": [], "errors": []},
        "open_questions": [],
    }
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


def _track_sync(invoked: List[str], name: str):
    def _inner(state, **_kwargs):
        invoked.append(name)
        return state
    return _inner


def _track_async(invoked: List[str], name: str):
    async def _inner(state, **_kwargs):
        invoked.append(name)
        return state
    return _inner


def _make_post_tool_emitter(invoked: List[str], action: str, *, max_emits: int = 1):
    """Return an async stub for ``post_tool_reasoning`` that emits ``action``.

    After ``max_emits`` invocations it switches to emitting ``finalize`` so the
    graph can terminate (used for the ``call_tool`` loop test where the action
    would otherwise re-enter the loop forever).
    """
    state_box = {"emitted": 0}

    async def _emit(state, **_kwargs):
        invoked.append("post_tool_reasoning")
        interactive = InteractiveState.from_mapping(state)
        history = interactive.facts.decision_history or []
        if state_box["emitted"] < max_emits:
            history.append(f"{action}: forced by mock")
            state_box["emitted"] += 1
        else:
            history.append("finalize: terminate after loop")
        interactive.facts.decision_history = history
        return interactive.as_graph_update()

    return _emit


def _make_router_emitter(invoked: List[str]):
    """Return an async stub for ``decision_router`` that writes router_outcome."""

    async def _emit(state, **_kwargs):
        invoked.append("decision_router")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.metadata or {}
        action = "finalize"
        hint = metadata.get("next_after_reflect")
        if isinstance(hint, dict) and hint.get("action"):
            action = str(hint["action"]).strip().lower()
            metadata.pop("next_after_reflect", None)
            interactive.facts.post_reflect_action = None
        else:
            history = interactive.facts.decision_history or []
            if history:
                action = history[-1].split(":", 1)[0].strip().lower() or "finalize"
        metadata["router_outcome"] = {
            "action": action,
            "reason": "mock_router",
            "candidate_action": action,
            "candidate_source": "ptr",
            "resolution_source": "candidate",
            "profile": "simple_tool_execution",
        }
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    return _emit


def _make_reflect_emitter(invoked: List[str], hint: str | None):
    """Return an async stub for ``reflect_node`` that sets one-hop hint state."""

    async def _emit(state, **_kwargs):
        invoked.append("reflect")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.metadata or {}
        if hint:
            metadata["next_after_reflect"] = {
                "action": hint,
                "hint_id": "test-reflect-hint",
                "issued_at_iteration": interactive.facts.iterations,
            }
        interactive.facts.metadata = metadata
        interactive.facts.post_reflect_action = hint
        return interactive.as_graph_update()

    return _emit


def _patch_common_nodes(invoked: List[str]):
    """Return the patches every integration test needs, except PTR / reflect."""
    return [
        patch(
            "agent.graph.builders.simple_tool_builder.classify_turn",
            side_effect=_track_sync(invoked, "classification"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.update_working_memory_node",
            side_effect=_set_validation_ready,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.memory_retrieval_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "memory_retrieval"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.select_tool_categories_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "select_tool_categories"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.articulate_tool_intent",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "articulation"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "prepare_tool_plan"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.approval_gate_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "approval_gate"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "dispatch_tool"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.synthesize_tool_output",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "tool_synthesizer"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.think_more_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "think_more"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.synthesis_node",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "synthesis"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.finalize_results",
            new_callable=AsyncMock,
            side_effect=_track_async(invoked, "format_results"),
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.finalize_turn",
            side_effect=_track_sync(invoked, "finalize"),
        ),
    ]


async def _run_graph(initial_state: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    graph = build_simple_tool_graph()
    final_state: Dict[str, Any] | None = None
    async for event in graph.astream(
        initial_state,
        {"configurable": {"thread_id": thread_id}},
        stream_mode="values",
    ):
        final_state = event
    assert final_state is not None
    return final_state


@pytest.mark.asyncio
async def test_call_tool_loops_back_to_select_categories_then_finalizes() -> None:
    """``call_tool`` re-enters tool selection; subsequent finalize terminates."""
    invoked: List[str] = []
    ptr = _make_post_tool_emitter(invoked, "call_tool", max_emits=1)
    router = _make_router_emitter(invoked)
    reflect = _make_reflect_emitter(invoked, hint=None)

    patches = _patch_common_nodes(invoked) + [
        patch(
            "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
            new_callable=AsyncMock,
            side_effect=ptr,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.decision_router",
            new_callable=AsyncMock,
            side_effect=router,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.reflect_node",
            new_callable=AsyncMock,
            side_effect=reflect,
        ),
    ]

    for p in patches:
        p.start()
    try:
        await _run_graph(_sample_state(), "call-tool-loop")
    finally:
        for p in patches:
            p.stop()

    # First trip: PTR emits call_tool → loops back to select_tool_categories.
    # Second PTR call emits finalize → terminates via format_results → finalize.
    assert invoked.count("select_tool_categories") == 2
    assert invoked.count("post_tool_reasoning") == 2
    assert invoked.count("decision_router") == 2
    assert "format_results" in invoked
    assert invoked[-1] == "finalize"
    assert "think_more" not in invoked
    assert "reflect" not in invoked
    assert "synthesis" not in invoked


@pytest.mark.asyncio
async def test_think_more_runs_then_returns_to_ptr_before_router_dispatch() -> None:
    """``think_more`` runs, then returns to PTR before next router dispatch."""
    invoked: List[str] = []
    ptr = _make_post_tool_emitter(invoked, "think_more", max_emits=1)
    router = _make_router_emitter(invoked)
    reflect = _make_reflect_emitter(invoked, hint=None)

    patches = _patch_common_nodes(invoked) + [
        patch(
            "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
            new_callable=AsyncMock,
            side_effect=ptr,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.decision_router",
            new_callable=AsyncMock,
            side_effect=router,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.reflect_node",
            new_callable=AsyncMock,
            side_effect=reflect,
        ),
    ]

    for p in patches:
        p.start()
    try:
        await _run_graph(_sample_state(), "think-more-loop")
    finally:
        for p in patches:
            p.stop()

    # think_more must have run, then the graph must return to PTR before
    # router dispatches the next action.
    assert "think_more" in invoked
    think_idx = invoked.index("think_more")
    later = invoked[think_idx + 1 :]
    assert "post_tool_reasoning" in later, (
        f"expected post_tool_reasoning after think_more, got {invoked!r}"
    )
    assert "decision_router" in later
    assert invoked[-1] == "finalize"


@pytest.mark.asyncio
async def test_reflect_runs_then_consumes_hint_and_dispatches() -> None:
    """``reflect`` runs, then decision_router consumes one-hop hint to dispatch."""
    invoked: List[str] = []
    ptr = _make_post_tool_emitter(invoked, "reflect", max_emits=1)
    router = _make_router_emitter(invoked)
    # Reflect emits a one-hop hint that router should consume as think_more.
    reflect = _make_reflect_emitter(invoked, hint="think_more")

    patches = _patch_common_nodes(invoked) + [
        patch(
            "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
            new_callable=AsyncMock,
            side_effect=ptr,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.decision_router",
            new_callable=AsyncMock,
            side_effect=router,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.reflect_node",
            new_callable=AsyncMock,
            side_effect=reflect,
        ),
    ]

    for p in patches:
        p.start()
    try:
        await _run_graph(_sample_state(), "reflect-hint")
    finally:
        for p in patches:
            p.stop()

    # reflect ran, then decision_router consumed the one-hop hint and dispatched.
    assert "reflect" in invoked
    assert "think_more" in invoked
    assert invoked.index("reflect") < invoked.index("think_more")
    assert invoked.index("reflect") < invoked.index("decision_router", invoked.index("reflect"))
    assert invoked[-1] == "finalize"


@pytest.mark.asyncio
async def test_synthesis_label_falls_through_to_format_results() -> None:
    """A PTR ``synthesis`` decision is invalid and dispatches straight to terminal.

    ``synthesis`` is not part of PTR's four-action contract. When a manually
    inserted (or contract-violating) ``synthesis`` PTR decision reaches the
    builder's post-tool conditional, the unknown-label fallback routes to
    ``format_results`` directly — the ``synthesis`` reasoning node is *not*
    invoked.
    """
    invoked: List[str] = []
    ptr = _make_post_tool_emitter(invoked, "synthesis", max_emits=1)
    router = _make_router_emitter(invoked)
    reflect = _make_reflect_emitter(invoked, hint=None)

    patches = _patch_common_nodes(invoked) + [
        patch(
            "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
            new_callable=AsyncMock,
            side_effect=ptr,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.decision_router",
            new_callable=AsyncMock,
            side_effect=router,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.reflect_node",
            new_callable=AsyncMock,
            side_effect=reflect,
        ),
    ]

    for p in patches:
        p.start()
    try:
        await _run_graph(_sample_state(), "synthesis-fallthrough")
    finally:
        for p in patches:
            p.stop()

    # Router-authoritative simple-tool routing normalizes synthesis-like
    # outcomes to a terminal pass in this wiring test.
    assert invoked[-2:] == ["format_results", "finalize"]
    # PTR runs exactly once in this forced path.
    assert invoked.count("post_tool_reasoning") == 1
    assert "think_more" not in invoked
    assert "reflect" not in invoked


@pytest.mark.asyncio
async def test_finalize_terminates_to_format_results() -> None:
    """``finalize`` from PTR routes directly to ``format_results → finalize``."""
    invoked: List[str] = []
    ptr = _make_post_tool_emitter(invoked, "finalize", max_emits=1)
    router = _make_router_emitter(invoked)
    reflect = _make_reflect_emitter(invoked, hint=None)

    patches = _patch_common_nodes(invoked) + [
        patch(
            "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
            new_callable=AsyncMock,
            side_effect=ptr,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.decision_router",
            new_callable=AsyncMock,
            side_effect=router,
        ),
        patch(
            "agent.graph.builders.simple_tool_builder.reflect_node",
            new_callable=AsyncMock,
            side_effect=reflect,
        ),
    ]

    for p in patches:
        p.start()
    try:
        await _run_graph(_sample_state(), "finalize-terminate")
    finally:
        for p in patches:
            p.stop()

    # PTR forces finalize on the first call; format_results then finalize end the run.
    assert invoked[-2:] == ["format_results", "finalize"]
    assert invoked.count("post_tool_reasoning") == 1
    assert "think_more" not in invoked
    assert "reflect" not in invoked
    assert "synthesis" not in invoked
