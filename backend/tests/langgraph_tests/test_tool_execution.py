"""End-to-end tests for the simple-tool (direct-executor) graph.

These tests exercise the wired ``build_simple_tool_graph`` with fakes on both
ends of the tool-execution subgraph (planner coordinator, upstream LLM-driven
nodes, downstream synthesizer/finalizer). They characterize the
direct-executor contract at the graph level:

- One-shot direct execution finalizes after a single tool call.
- Lightweight conditional multi-step direct execution can loop back from
  ``post_tool_reasoning`` into ``select_tool_categories`` exactly once
  when the policy sanctions a follow-up step, then finalizes.
- Terminal "no progress" path: when PTR's decision is not ``call_tool``,
  routing falls through to ``format_results`` / ``finalize`` (no extra
  tool dispatch).

Upstream LLM-bound nodes (``select_tool_categories``, ``articulation``,
``working_memory``, ``memory_retrieval``) are stubbed as no-ops via
``monkeypatch`` on the builder module so the tests exercise the
direct-executor routing seam without requiring a populated
``ConversationContextBundle`` or live LLM access.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.persistence import get_default_checkpointer
from agent.graph.state import InteractiveInput, InteractiveState


class _StubCoordinator:
    def __init__(self, config) -> None:
        self.config = config

    async def run(self, request):
        from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome

        result = {
            "success": True,
            "status": "success",
            "stdout": "open port 80",
            "stderr": "",
            "stdout_excerpt": "open port 80",
            "stderr_excerpt": "",
            "exit_code": 0,
            "observation": "open port 80",
            "approval_granted": True,
            "approval_reason": None,
            "approval_metadata": {},
            "duration": 0.2,
        }
        return ToolExecutionOutcome(
            tool_id="information_gathering.network_discovery.nmap",
            parameters={"target": "127.0.0.1"},
            catalog=[
                ToolCatalogEntry(
                    tool_id="information_gathering.network_discovery.nmap",
                    name="information_gathering.network_discovery.nmap",
                    category="information_gathering",
                    description="stubbed nmap tool",
                )
            ],
            result=result,
            summary="open port 80",
            reasoning=[],
            duration=0.2,
        )


async def _noop_async_node(state, **_kwargs):
    """Pass state through unchanged. Used to stub LLM-bound upstream nodes."""
    return state


def _noop_sync_node(state, **_kwargs):
    """Sync pass-through stub for sync upstream nodes (e.g. working memory)."""
    return state


def _stub_upstream_llm_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub LLM-bound upstream nodes so graph runs without a context bundle.

    The simple-tool graph entry point is ``classification`` →
    ``update_working_memory`` → ``memory_retrieval`` →
    ``select_tool_categories`` → ``prepare_tool_plan`` → ``articulation``
    → ``approval_gate`` → ``dispatch_tool`` → ``tool_synthesizer`` →
    ``post_tool_reasoning`` → [``format_results`` or loop back].

    Upstream LLM-bound nodes are replaced with pass-throughs so the test
    can focus on direct-executor behavior (the planner coordinator,
    post-tool reasoning seam, and routing).
    """
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.update_working_memory_node",
        _noop_sync_node,
    )
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.memory_retrieval_node",
        _noop_async_node,
    )
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.select_tool_categories_node",
        _noop_async_node,
    )
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.articulate_tool_intent",
        _noop_async_node,
    )


def _base_payload_metadata() -> Dict[str, Any]:
    """Pre-seeded planner plan so ``prepare_tool_plan`` short-circuits.

    Also seeds the hot-path ``ConversationContextBundle`` because the
    planner request-context builder requires it after Phase 5 authority
    cutover (see ``request_context._resolve_planner_history``).
    """
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    bundle = build_conversation_context_bundle(
        conversation_id="direct-exec-test-conv",
        turn_id="turn-1",
        turn_sequence=0,
        messages=[],
    )
    return {
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["127.0.0.1"]},
        "eligible_routes": ["simple_tool_execution"],
        "tool_plan_prepared": True,
        "planner_plan": {
            "selected_tools": ["information_gathering.network_discovery.nmap"],
            "tool_parameters": {
                "information_gathering.network_discovery.nmap": {"target": "127.0.0.1"}
            },
            "execution_strategy": "single",
            "reasoning": "",
            "expected_outcome": "",
        },
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
    }


def _stub_planner_cache_and_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch planner-cache and LangGraph streaming helpers to test-friendly no-ops.

    - ``should_invalidate_plan`` now lives in
      ``agent.graph.utils.cache_invalidation`` and is imported by
      ``tool_execution_runtime.planner_service``; patch it there so the
      pre-seeded ``planner_plan`` is preserved and the stub coordinator
      is reached without a live LLM planner fallback.
    - ``get_stream_writer`` is neutralized since tests invoke the graph
      outside a LangGraph streaming context.
    """
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.planner_service.should_invalidate_plan",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution.get_stream_writer", lambda: None
    )


def _run_simple_tool_turn(monkeypatch: pytest.MonkeyPatch) -> InteractiveState:
    async def _stub_finalize_results(state, **_kwargs):
        interactive = InteractiveState.from_mapping(state)
        interactive.trace.final_text = "open port 80"
        metadata = dict(interactive.facts.metadata or {})
        metadata["tool_summaries"] = [{"status": "success"}]
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator",
        _StubCoordinator,
    )
    _stub_planner_cache_and_streaming(monkeypatch)
    _stub_upstream_llm_nodes(monkeypatch)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.synthesize_tool_output", _noop_async_node)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.post_tool_reasoning", _noop_async_node)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.finalize_results", _stub_finalize_results)

    payload = InteractiveInput(
        task_id=123,
        message="Scan 127.0.0.1 with nmap",
        metadata=_base_payload_metadata(),
    )
    start_state = payload.to_state()
    compiled = build_simple_tool_graph(checkpointer=get_default_checkpointer())
    result = asyncio.run(
        compiled.ainvoke(
            start_state.as_graph_state(),
            config={"configurable": {"thread_id": "test-thread"}},
        )
    )
    return InteractiveState.from_mapping(result)


def test_simple_tool_graph_produces_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """One-shot direct execution: dispatch one tool and finalize.

    Exercises the backward-compatible path where PTR does not request a
    follow-up ``call_tool`` (the stub is a no-op, so ``decision_history``
    is empty and the builder routes to ``format_results``).
    """
    state = _run_simple_tool_turn(monkeypatch)

    assert state.trace.final_text
    assert "open port 80" in state.trace.final_text
    summaries = state.facts.metadata.get("tool_summaries")
    assert summaries and summaries[-1]["status"] == "success"


# ---------------------------------------------------------------------------
# Lightweight conditional multi-step direct execution
# ---------------------------------------------------------------------------


def test_simple_tool_graph_handles_conditional_followup_then_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy-sanctioned follow-up loops back once, then finalizes.

    This simulates a lightweight conditional workflow (e.g., ping →
    conditional nmap): the first ``post_tool_reasoning`` invocation
    records a sanctioned ``call_tool`` decision, routing back to
    ``select_tool_categories``; the second invocation records a
    non-``call_tool`` decision, routing to ``format_results``.
    The graph must terminate deterministically without a third
    dispatch.
    """
    dispatch_calls: List[int] = []
    ptr_invocations: List[int] = []

    async def _stub_coordinator_run(self, request):  # noqa: ANN001
        from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome

        dispatch_calls.append(len(dispatch_calls) + 1)
        return ToolExecutionOutcome(
            tool_id="information_gathering.network_discovery.nmap",
            parameters={"target": "127.0.0.1"},
            catalog=[
                ToolCatalogEntry(
                    tool_id="information_gathering.network_discovery.nmap",
                    name="information_gathering.network_discovery.nmap",
                    category="information_gathering",
                    description="stubbed nmap tool",
                )
            ],
            result={
                "success": True,
                "status": "success",
                "stdout": f"iteration {len(dispatch_calls)} output",
                "stderr": "",
                "stdout_excerpt": f"iteration {len(dispatch_calls)} output",
                "stderr_excerpt": "",
                "exit_code": 0,
                "observation": "progress",
                "approval_granted": True,
                "approval_reason": None,
                "approval_metadata": {},
                "duration": 0.1,
            },
            summary=f"iter {len(dispatch_calls)}",
            reasoning=[],
            duration=0.1,
        )

    async def _stub_post_tool(state, **_kwargs):
        """Emit ``call_tool`` on first invocation, ``finalize`` on second."""
        interactive = InteractiveState.from_mapping(state)
        invocation_index = len(ptr_invocations) + 1
        ptr_invocations.append(invocation_index)
        history = list(interactive.facts.decision_history or [])
        if invocation_index == 1:
            history.append("call_tool: policy-sanctioned follow-up")
        else:
            history.append("finalize: goal satisfied after follow-up")
        interactive.facts.decision_history = history
        return interactive.as_graph_update()

    async def _stub_finalize_results(state, **_kwargs):
        interactive = InteractiveState.from_mapping(state)
        interactive.trace.final_text = "multi-step complete"
        return interactive.as_graph_update()

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator",
        lambda config: type(
            "_C",
            (),
            {"run": _stub_coordinator_run, "__init__": lambda self, _c=None: None},
        )(),
    )
    _stub_planner_cache_and_streaming(monkeypatch)
    _stub_upstream_llm_nodes(monkeypatch)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.synthesize_tool_output", _noop_async_node)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.post_tool_reasoning", _stub_post_tool)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.finalize_results", _stub_finalize_results)

    payload = InteractiveInput(
        task_id=456,
        message="Scan 127.0.0.1 and follow up if needed",
        metadata=_base_payload_metadata(),
    )
    start_state = payload.to_state()
    compiled = build_simple_tool_graph(checkpointer=get_default_checkpointer())
    result = asyncio.run(
        compiled.ainvoke(
            start_state.as_graph_state(),
            config={"configurable": {"thread_id": "multi-step-thread"}},
        )
    )
    final_state = InteractiveState.from_mapping(result)

    # Exactly two dispatches: the initial tool call and the sanctioned
    # follow-up. The third PTR invocation must NOT happen because the
    # second decision was not ``call_tool`` — graph must terminate.
    assert dispatch_calls == [1, 2], f"expected two dispatches, got {dispatch_calls}"
    assert len(ptr_invocations) == 2
    assert final_state.trace.final_text == "multi-step complete"
    assert final_state.facts.decision_history[-1].startswith("finalize")


# ---------------------------------------------------------------------------
# Terminal no-progress finalization
# ---------------------------------------------------------------------------


def test_simple_tool_graph_finalizes_when_ptr_declines_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTR decision that is not ``call_tool`` routes to finalize.

    Characterizes the deterministic stop for no-progress or reflect
    decisions surfaced by the direct-executor policy: the builder must
    not re-enter the tool dispatch loop.
    """
    dispatch_calls: List[int] = []

    async def _counting_coordinator_run(self, request):  # noqa: ANN001
        from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome

        dispatch_calls.append(len(dispatch_calls) + 1)
        return ToolExecutionOutcome(
            tool_id="information_gathering.network_discovery.nmap",
            parameters={"target": "127.0.0.1"},
            catalog=[
                ToolCatalogEntry(
                    tool_id="information_gathering.network_discovery.nmap",
                    name="information_gathering.network_discovery.nmap",
                    category="information_gathering",
                    description="stubbed nmap tool",
                )
            ],
            result={
                "success": True,
                "status": "success",
                "stdout": "one output",
                "stderr": "",
                "stdout_excerpt": "one output",
                "stderr_excerpt": "",
                "exit_code": 0,
                "observation": "one observation",
                "approval_granted": True,
                "approval_reason": None,
                "approval_metadata": {},
                "duration": 0.1,
            },
            summary="single dispatch",
            reasoning=[],
            duration=0.1,
        )

    async def _stub_post_tool_reflect(state, **_kwargs):
        """PTR reports ``reflect`` — the direct-executor no-progress override."""
        interactive = InteractiveState.from_mapping(state)
        history = list(interactive.facts.decision_history or [])
        history.append(
            "reflect: Override: direct-executor repeated_no_progress"
        )
        interactive.facts.decision_history = history
        return interactive.as_graph_update()

    async def _stub_finalize_results(state, **_kwargs):
        interactive = InteractiveState.from_mapping(state)
        interactive.trace.final_text = "stopped without progress"
        return interactive.as_graph_update()

    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator",
        lambda config: type(
            "_C",
            (),
            {"run": _counting_coordinator_run, "__init__": lambda self, _c=None: None},
        )(),
    )
    _stub_planner_cache_and_streaming(monkeypatch)
    _stub_upstream_llm_nodes(monkeypatch)
    monkeypatch.setattr("agent.graph.builders.simple_tool_builder.synthesize_tool_output", _noop_async_node)
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
        _stub_post_tool_reflect,
    )
    monkeypatch.setattr(
        "agent.graph.builders.simple_tool_builder.finalize_results",
        _stub_finalize_results,
    )

    payload = InteractiveInput(
        task_id=789,
        message="Scan 127.0.0.1",
        metadata=_base_payload_metadata(),
    )
    start_state = payload.to_state()
    compiled = build_simple_tool_graph(checkpointer=get_default_checkpointer())
    result = asyncio.run(
        compiled.ainvoke(
            start_state.as_graph_state(),
            config={"configurable": {"thread_id": "no-progress-thread"}},
        )
    )
    final_state = InteractiveState.from_mapping(result)

    # Only one dispatch: PTR returned ``reflect``, so the builder routes to
    # ``format_results`` rather than looping back for another tool.
    assert dispatch_calls == [1]
    assert final_state.trace.final_text == "stopped without progress"
    assert "reflect" in final_state.facts.decision_history[-1]
