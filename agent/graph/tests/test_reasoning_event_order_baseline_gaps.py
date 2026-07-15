"""Reasoning event-order gap baselines and regressions.

These tests started as Phase 1 Task 1.2 baseline-gap assertions and are
promoted to regressions as each scoped implementation task lands.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes import hitl_helpers as hitl_helpers_module
from agent.graph.state import FactsState, InteractiveState, TraceState


class _FakeLLMResponse:
    def __init__(self, payload: dict | None = None, *, content: str = "{}"):
        self.content = content
        self.structured_output = payload if payload is not None else {}
        self.usage = {}


class _PlannerOrderProbeLLM:
    def __init__(self, *, timeline: list[str], payload: dict):
        self._timeline = timeline
        self._payload = payload

    async def chat_with_usage(self, *_args, **_kwargs):
        self._timeline.append("planner_llm_call")
        return _FakeLLMResponse(self._payload)


class _NodeOrderProbeLLM:
    def __init__(
        self,
        *,
        timeline: list[str],
        marker: str,
        payload: dict | None = None,
        content: str = "{}",
    ):
        self._timeline = timeline
        self._marker = marker
        self._payload = payload
        self._content = content

    async def chat_with_usage(self, *_args, **_kwargs):
        self._timeline.append(self._marker)
        return _FakeLLMResponse(self._payload, content=self._content)


async def _await_passthrough(awaitable, **_kwargs):
    return await awaitable


def _planning_state() -> dict:
    facts = FactsState(
        task_id=11,
        message="Enumerate exposed services on 10.0.0.7",
        conversation_id="conv-baseline-gap",
        capability="deep_reasoning",
        metadata={
            "agent_mode": "full_access",
            "turn_id": "turn-baseline-gap",
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _tool_execution_state_without_prepared_plan() -> dict:
    planner_plan = {
        "selected_tools": ["shell.exec"],
        "tool_parameters": {"shell.exec": {"command": "echo ok"}},
        "execution_strategy": "sequential",
        "tool_batch": {
            "tool_batch_id": "tb_event_order",
            "requested_execution_strategy": "sequential",
            "deferred_followups": [],
            "selection_rationale": "Event-order fixture",
            "tool_calls": [
                {
                    "tool_call_id": "tc_event_order_1",
                    "tool_id": "shell.exec",
                    "parameters": {"command": "echo ok"},
                    "intent": "Run planned command",
                }
            ],
        },
    }
    facts = FactsState(
        task_id=12,
        message="Run the planned command",
        conversation_id="conv-tool-gap",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo ok"}},
        metadata={
            "agent_mode": "agent",
            "planner_plan": planner_plan,
            "graph_runtime_context": {
                "task_id": 12,
                "tenant_id": 1,
                "runtime_placement_mode": "local",
                "workspace_id": "task-12",
                "actor_type": "system",
                "actor_id": "langgraph",
                "workspace_path": "/tmp",
            },
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-tool-gap",
                turn_id="turn-tool-gap",
                turn_sequence=0,
                messages=[],
            ),
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _reflection_state() -> dict:
    facts = FactsState(
        task_id=13,
        message="Recover from repeated failed actions",
        conversation_id="conv-reflect-gap",
        capability="deep_reasoning",
        stuck_counter=3,
        decision_history=[
            "call_tool: run nmap",
            "call_tool: run nmap",
            "call_tool: run nmap",
        ],
        metadata={
            "agent_mode": "full_access",
            "turn_id": "turn-reflect-gap",
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _synthesis_state() -> dict:
    facts = FactsState(
        task_id=14,
        message="Summarize partial findings",
        conversation_id="conv-synthesis-gap",
        capability="deep_reasoning",
        iterations=4,
        metadata={
            "agent_mode": "full_access",
            "turn_id": "turn-synthesis-gap",
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _post_tool_reasoning_state() -> dict:
    facts = FactsState(
        task_id=16,
        message="Summarize tool output and decide next action",
        conversation_id="conv-post-tool-gap",
        capability="deep_reasoning",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo ok"}},
        iterations=1,
        metadata={
            "agent_mode": "full_access",
            "turn_id": "turn-post-tool-gap",
            "synthesized_output": {
                "tool": "shell.exec",
                "summary": "Command completed successfully",
                "observation": "ok",
            },
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _fake_tool_outcome() -> SimpleNamespace:
    def _to_graph_metadata() -> dict:
        return {"tool_id": "shell.exec", "result": {"success": True}}

    return SimpleNamespace(
        tool_id="shell.exec",
        duration=0.1,
        parameters={"command": "echo ok"},
        result={
            "success": True,
            "status": "success",
            "stdout": "ok",
            "stdout_excerpt": "ok",
            "stderr": "",
            "stderr_excerpt": "",
            "observation": "ok",
            "duration": 1,
            "exit_code": 0,
        },
        catalog=[],
        reasoning=["Executed shell command"],
        summary="ok",
        to_graph_metadata=_to_graph_metadata,
    )


class _PostToolStreamResponse:
    def __init__(self) -> None:
        self.content_iterator = self._iter()

    async def _iter(self):
        yield "Observation confirms command success."

    def get_final_usage(self) -> dict[str, Any]:
        return {
            "prompt_tokens": 10,
            "completion_tokens": 6,
            "total_tokens": 16,
            "model": "gpt-5.2-mini",
            "provider": "openai",
        }


class _PostToolStreamingLLM:
    model = "gpt-5.2-mini"

    async def stream_chat_messages_with_usage(self, *_args, **_kwargs):
        return _PostToolStreamResponse()


@pytest.mark.asyncio
async def test_planner_llm_call_is_inside_reasoning_section_boundary(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.graph.nodes import planner_generation as planner_generation_module
    from agent.graph.nodes import planner_setup as planner_setup_module

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    llm_payload = {
        "mode": "plan_ready",
        "plan": ["Step 1: Enumerate service exposure."],
        "todo_list": ["Enumerate service exposure."],
        "first_goal": "Enumerate service exposure.",
    }

    monkeypatch.setattr(
        planner_generation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _PlannerOrderProbeLLM(timeline=timeline, payload=llm_payload),
    )
    monkeypatch.setattr(hitl_helpers_module, "should_require_plan_approval", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(planner_generation_module, "wait_for_with_timeout", _await_passthrough)
    monkeypatch.setattr(planner_setup_module, "load_and_format_environment", lambda *_args, **_kwargs: ({}, ""))
    monkeypatch.setattr(
        planner_generation_module,
        "validate_plan_against_scope",
        lambda *_args, **_kwargs: {"valid": True, "violations": []},
    )

    await planner_module.planner_node(_planning_state(), writer=_writer)

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    assert "planner_llm_call" in timeline, f"missing planner_llm_call marker, timeline={timeline}"
    assert timeline.count("reasoning_section_end") == 1, (
        "planner lifecycle should close exactly once per planning section "
        f"(timeline={timeline})"
    )

    start_idx = timeline.index("reasoning_start")
    call_idx = timeline.index("planner_llm_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < call_idx < end_idx, (
        "planner gap baseline: planning reasoning section closed before the awaited "
        f"LLM call (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_plan_route_keeps_thinking_active_until_plan_event(monkeypatch) -> None:
    from agent.graph.nodes import plan_review as plan_review_module
    from agent.graph.nodes import planner as planner_module
    from agent.graph.nodes import planner_generation as planner_generation_module
    from agent.graph.nodes import planner_setup as planner_setup_module

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    llm_payload = {
        "mode": "plan_ready",
        "plan": ["Step 1: Enumerate service exposure."],
        "todo_list": ["Enumerate service exposure."],
        "first_goal": "Enumerate service exposure.",
    }

    monkeypatch.setattr(
        planner_generation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _PlannerOrderProbeLLM(timeline=timeline, payload=llm_payload),
    )
    monkeypatch.setattr(hitl_helpers_module, "should_require_plan_approval", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(planner_generation_module, "wait_for_with_timeout", _await_passthrough)
    monkeypatch.setattr(planner_setup_module, "load_and_format_environment", lambda *_args, **_kwargs: ({}, ""))
    monkeypatch.setattr(
        planner_generation_module,
        "validate_plan_against_scope",
        lambda *_args, **_kwargs: {"valid": True, "violations": []},
    )

    planner_update = await planner_module.planner_node(_planning_state(), writer=_writer)
    await plan_review_module.plan_review_node(planner_update, writer=_writer)

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "planner_llm_call" in timeline, f"missing planner_llm_call marker, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    assert timeline.count("reasoning_section_end") == 1, (
        "plan route should close the planning reasoning section exactly once "
        f"(timeline={timeline})"
    )
    assert any(event_type in {"plan_created", "todo_progress"} for event_type in timeline), (
        "missing plan event on plan route timeline: expected plan_created or todo_progress "
        f"(timeline={timeline})"
    )

    start_idx = timeline.index("reasoning_start")
    call_idx = timeline.index("planner_llm_call")
    end_idx = timeline.index("reasoning_section_end")
    plan_event_idx = next(
        idx for idx, event_type in enumerate(timeline) if event_type in {"plan_created", "todo_progress"}
    )

    assert start_idx < call_idx < end_idx < plan_event_idx, (
        "PLAN route must keep the Thinking section active around plan generation and emit "
        "plan output only after that section closes "
        f"(timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_dispatch_fallback_tool_planning_runs_inside_reasoning_section_before_tool_start() -> None:
    from agent.graph.builders.common_edges import wrap_with_context_async
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    async def _mark_ensure_action_plan(*_args, **_kwargs) -> None:
        timeline.append("ensure_action_plan_call")

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
        side_effect=_mark_ensure_action_plan,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_tool_outcome(),
    ):
        wrapped_node = wrap_with_context_async(run_tool_execution)
        await wrapped_node(_tool_execution_state_without_prepared_plan(), writer=_writer)

    ensure_mock.assert_awaited_once()
    assert "tool_start" in timeline, f"missing tool_start marker, timeline={timeline}"
    assert "ensure_action_plan_call" in timeline, f"missing ensure marker, timeline={timeline}"
    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    assert timeline.count("reasoning_section_end") == 1, (
        "dispatch fallback tool-planning should close exactly once per section "
        f"(timeline={timeline})"
    )

    start_idx = timeline.index("reasoning_start")
    ensure_idx = timeline.index("ensure_action_plan_call")
    end_idx = timeline.index("reasoning_section_end")
    tool_start_idx = timeline.index("tool_start")

    assert start_idx < ensure_idx < end_idx < tool_start_idx, (
        "tool-planning gap baseline: fallback planning reached ensure_action_plan without an "
        f"active reasoning section before tool_start (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_prepare_tool_execution_plan_runs_action_plan_ensure_inside_reasoning_section() -> None:
    from agent.graph.builders.common_edges import wrap_with_context_async
    from agent.graph.subgraphs.tool_execution import prepare_tool_execution_plan

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    async def _mark_ensure_action_plan(*_args, **_kwargs) -> None:
        timeline.append("ensure_action_plan_call")

    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
        side_effect=_mark_ensure_action_plan,
    ) as ensure_mock:
        wrapped_node = wrap_with_context_async(prepare_tool_execution_plan)
        await wrapped_node(_tool_execution_state_without_prepared_plan(), writer=_writer)

    ensure_mock.assert_awaited_once()
    assert "ensure_action_plan_call" in timeline, f"missing ensure marker, timeline={timeline}"
    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    assert timeline.count("reasoning_section_end") == 1, (
        "prepare tool-planning should close exactly once per section "
        f"(timeline={timeline})"
    )

    start_idx = timeline.index("reasoning_start")
    ensure_idx = timeline.index("ensure_action_plan_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < ensure_idx < end_idx, (
        "tool-planning gap baseline: prepare_tool_execution_plan awaited ensure_action_plan "
        f"without an active reasoning section (timeline={timeline})"
    )


def test_category_selection_node_exposes_writer_boundary_for_reasoning_lifecycle() -> None:
    from agent.graph.nodes.select_tool_categories import select_tool_categories_node

    signature = inspect.signature(select_tool_categories_node)
    assert "writer" in signature.parameters, (
        "structured selection gap baseline: select_tool_categories_node has no writer boundary, "
        "so reasoning section lifecycle cannot be emitted around awaited category selection"
    )


def test_tool_execution_prepare_and_dispatch_nodes_expose_writer_boundary() -> None:
    from agent.graph.subgraphs.tool_execution import (
        dispatch_tool_execution_node,
        prepare_tool_execution_plan,
    )

    prepare_signature = inspect.signature(prepare_tool_execution_plan)
    dispatch_signature = inspect.signature(dispatch_tool_execution_node)
    assert "writer" in prepare_signature.parameters, (
        "prepare gap baseline: prepare_tool_execution_plan has no writer boundary, "
        "so wrapped nodes cannot forward writer for tool-planning reasoning lifecycle"
    )
    assert "writer" in dispatch_signature.parameters, (
        "dispatch gap baseline: dispatch_tool_execution_node has no writer boundary, "
        "so wrapped nodes cannot forward writer for dispatch/run reasoning lifecycle"
    )


def test_reflect_node_exposes_writer_boundary_for_reasoning_lifecycle() -> None:
    from agent.graph.nodes.reflect import reflect_node

    signature = inspect.signature(reflect_node)
    assert "writer" in signature.parameters, (
        "reflection gap baseline: reflect_node has no writer boundary, "
        "so reasoning section lifecycle cannot be emitted around awaited reflection calls"
    )


def test_synthesis_node_exposes_writer_boundary_for_reasoning_lifecycle() -> None:
    from agent.graph.nodes.synthesis import synthesis_node

    signature = inspect.signature(synthesis_node)
    assert "writer" in signature.parameters, (
        "synthesis gap baseline: synthesis_node has no writer boundary, "
        "so reasoning section lifecycle cannot be emitted around awaited synthesis calls"
    )


@pytest.mark.asyncio
async def test_category_selection_llm_call_is_inside_reasoning_section_boundary(monkeypatch) -> None:
    from agent.graph.builders.common_edges import wrap_with_context_async
    from agent.graph.nodes import select_tool_categories as category_module

    timeline: list[str] = []
    events: list[dict[str, Any]] = []

    def _writer(event: dict) -> None:
        events.append(event)
        timeline.append(str(event.get("type") or ""))

    monkeypatch.setattr(
        "agent.tools.category_utils.get_tool_categories",
        lambda: ["information_gathering"],
    )
    monkeypatch.setattr(
        "agent.tools.category_utils.get_category_descriptions",
        lambda: {"information_gathering": "Gather information from target systems."},
    )
    monkeypatch.setattr(
        category_module,
        "_build_category_selection_prompt",
        lambda **_kwargs: "Choose categories",
    )

    async def _mark_category_llm_call(*_args, **_kwargs) -> list[str]:
        timeline.append("category_llm_call")
        return ["information_gathering"]

    monkeypatch.setattr(category_module, "_call_llm_for_categories", _mark_category_llm_call)

    wrapped_node = wrap_with_context_async(category_module.select_tool_categories_node)
    await wrapped_node(_planning_state(), writer=_writer)

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_delta" in timeline, f"missing reasoning_delta label event, timeline={timeline}"
    assert "category_llm_call" in timeline, f"missing category_llm_call marker, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    assert any(
        event.get("type") == "reasoning_delta"
        and event.get("content") == "Selecting relevant tool categories."
        for event in events
    ), f"missing expected operational label delta, events={events}"

    start_idx = timeline.index("reasoning_start")
    delta_idx = timeline.index("reasoning_delta")
    llm_idx = timeline.index("category_llm_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < delta_idx < llm_idx < end_idx, (
        "category-selection gap baseline: awaited category selection should remain inside the "
        f"tool_category_selection reasoning section (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_reflect_llm_call_is_inside_reasoning_section_boundary(monkeypatch) -> None:
    from agent.graph.builders.common_edges import wrap_with_context_async
    from agent.graph.nodes import reflect as reflect_module

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    llm_payload = {
        "root_cause": "Repeated unchanged tool invocation",
        "alternative_approaches": ["Adjust scan parameters"],
    }

    monkeypatch.setattr(
        reflect_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _NodeOrderProbeLLM(
            timeline=timeline,
            marker="reflect_llm_call",
            payload=llm_payload,
        ),
    )
    monkeypatch.setattr(reflect_module, "wait_for_with_timeout", _await_passthrough)

    wrapped_node = wrap_with_context_async(reflect_module.reflect_node)
    await wrapped_node(_reflection_state(), writer=_writer)

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reflect_llm_call" in timeline, f"missing reflect_llm_call marker, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"

    start_idx = timeline.index("reasoning_start")
    llm_idx = timeline.index("reflect_llm_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < llm_idx < end_idx, (
        "reflection gap baseline: awaited reflection call should remain inside the "
        f"reflection reasoning section (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_synthesis_llm_call_is_inside_reasoning_section_boundary(monkeypatch) -> None:
    from agent.graph.builders.common_edges import wrap_with_context_async
    from agent.graph.nodes import synthesis as synthesis_module

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    monkeypatch.setattr(
        synthesis_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _NodeOrderProbeLLM(
            timeline=timeline,
            marker="synthesis_llm_call",
            content="Synthesis complete.",
        ),
    )
    monkeypatch.setattr(synthesis_module, "wait_for_with_timeout", _await_passthrough)

    wrapped_node = wrap_with_context_async(synthesis_module.synthesis_node)
    await wrapped_node(_synthesis_state(), writer=_writer)

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "synthesis_llm_call" in timeline, f"missing synthesis_llm_call marker, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"

    start_idx = timeline.index("reasoning_start")
    llm_idx = timeline.index("synthesis_llm_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < llm_idx < end_idx, (
        "synthesis gap baseline: awaited synthesis call should remain inside the "
        f"synthesis reasoning section (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_post_tool_decision_analysis_is_inside_observation_section(monkeypatch) -> None:
    from types import SimpleNamespace

    from agent.graph.nodes.post_tool_reasoning import node as ptr_module
    from agent.graph.nodes.post_tool_reasoning.models import (
        PostToolReasoningDecisionOutput,
    )
    from agent.graph.nodes.post_tool_reasoning.streaming import base as stream_base

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    async def _mark_decision_analysis(*_args, **_kwargs) -> PostToolReasoningDecisionOutput:
        timeline.append("decision_analysis_call")
        return PostToolReasoningDecisionOutput(
            next_action="finalize",
            action_reasoning="Tool output fully answers the user request.",
            user_goal_achieved=True,
        )

    monkeypatch.setattr(ptr_module, "analyze_tool_result", _mark_decision_analysis)
    monkeypatch.setattr(
        ptr_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _PostToolStreamingLLM(),
    )
    monkeypatch.setattr(
        ptr_module,
        "resolve_llm_call_settings",
        lambda *_args, **_kwargs: SimpleNamespace(provider="openai", model="gpt-5.2-mini"),
    )
    monkeypatch.setattr(ptr_module, "get_llm_reasoning_effort", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ptr_module, "resolve_turn_sequence", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(stream_base, "require_usage_aware_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stream_base, "wait_for_with_timeout", _await_passthrough)
    monkeypatch.setattr(stream_base, "iter_with_idle_timeout", lambda iterator, **_kwargs: iterator)

    await ptr_module.post_tool_reasoning(
        _post_tool_reasoning_state(),
        writer=_writer,
    )

    assert "observation_start" in timeline, f"missing observation_start, timeline={timeline}"
    assert "decision_analysis_call" in timeline, f"missing decision call marker, timeline={timeline}"
    assert "observation_section_end" in timeline, f"missing observation_section_end, timeline={timeline}"

    start_idx = timeline.index("observation_start")
    decision_idx = timeline.index("decision_analysis_call")
    end_idx = timeline.index("observation_section_end")

    assert start_idx < decision_idx < end_idx, (
        "post-tool decision analysis should run while Observation is active; no separate "
        f"Thinking section is needed unless Observation closes first (timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_unified_stream_reasoning_closes_section_on_error() -> None:
    from agent.graph.emission.unified_emitter import UnifiedEventEmitter

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    class _FailingLLM:
        def stream_chat_messages(self, *_args, **_kwargs):
            async def _iterator():
                yield "partial"
                raise RuntimeError("stream failed")

            return _iterator()

    emitter = UnifiedEventEmitter(
        _writer,
        conversation_id="conv-u",
        turn_id="turn-u",
        turn_sequence=1,
    )

    with pytest.raises(RuntimeError, match="stream failed"):
        await emitter.stream_reasoning(
            _FailingLLM(),
            [{"role": "user", "content": "hi"}],
            step="thinking",
        )

    assert timeline == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]


@pytest.mark.asyncio
async def test_unified_stream_reasoning_closes_section_on_cancellation() -> None:
    from agent.graph.emission.unified_emitter import UnifiedEventEmitter

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    class _CancelledLLM:
        def stream_chat_messages(self, *_args, **_kwargs):
            async def _iterator():
                raise asyncio.CancelledError()
                yield ""  # pragma: no cover

            return _iterator()

    emitter = UnifiedEventEmitter(
        _writer,
        conversation_id="conv-u-cancel",
        turn_id="turn-u-cancel",
        turn_sequence=2,
    )

    with pytest.raises(asyncio.CancelledError):
        await emitter.stream_reasoning(
            _CancelledLLM(),
            [{"role": "user", "content": "hi"}],
            step="thinking",
        )

    assert timeline == [
        "reasoning_start",
        "reasoning_section_end",
    ]


@pytest.mark.asyncio
async def test_articulation_stream_failure_closes_tool_intent_section(monkeypatch) -> None:
    from agent.graph.nodes import tool_articulation as articulation_module

    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    class _BrokenStreamResponse:
        def __init__(self) -> None:
            self.content_iterator = self._iter()

        async def _iter(self):
            yield "chunk"
            raise RuntimeError("articulation stream failed")

        def get_final_usage(self):
            raise AssertionError("usage should not be requested when stream fails")

    class _BrokenStreamLLM:
        model = "stub-tool-articulation"

        async def stream_chat_messages_with_usage(self, *_args, **_kwargs):
            return _BrokenStreamResponse()

    monkeypatch.setattr(articulation_module, "get_stream_writer", lambda: _writer)
    monkeypatch.setattr(
        articulation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _BrokenStreamLLM(),
    )
    monkeypatch.setattr(
        articulation_module,
        "resolve_llm_call_settings",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        articulation_module,
        "get_llm_reasoning_effort",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        articulation_module,
        "build_tool_articulation_prompt",
        lambda **_kwargs: "prompt",
    )
    monkeypatch.setattr(
        articulation_module,
        "require_usage_aware_streaming",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(articulation_module, "wait_for_with_timeout", _await_passthrough)
    monkeypatch.setattr(
        articulation_module,
        "iter_with_idle_timeout",
        lambda iterator, **_kwargs: iterator,
    )

    state = {
        "facts": {
            "task_id": 15,
            "message": "run nmap",
            "conversation_id": "conv-articulation-gap",
            "selected_tool": "nmap",
            "tool_parameters": {"nmap": {"target": "10.0.0.1"}},
            "metadata": {
                "api_key": "test-key",
                "model": "stub-tool-articulation",
                "working_memory": {"intent_brief": {"overall_goal": "enumerate"}},
            },
        },
        "trace": {"reasoning": []},
    }

    with pytest.raises(RuntimeError, match="articulation stream failed"):
        await articulation_module.articulate_tool_intent(state, context=None, config={})

    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"
    start_idx = timeline.index("reasoning_start")
    end_idx = timeline.index("reasoning_section_end")
    assert start_idx < end_idx, f"expected start before end, timeline={timeline}"


@pytest.mark.asyncio
async def test_think_more_stream_fallback_non_stream_call_is_inside_reasoning_section(
    monkeypatch,
) -> None:
    import agent.graph.emission.reasoning_section as section_module
    from agent.graph.nodes import think_more as think_more_module

    timeline: list[str] = []

    class _FallbackLLM:
        async def chat_with_usage(self, *_args, **_kwargs):
            timeline.append("fallback_llm_call")
            return _FakeLLMResponse(
                {
                    "reasoning": "Analyze and continue.",
                    "updated_plan": [],
                    "next_goal": "Continue gathering evidence",
                    "key_observations": [],
                },
                content='{"reasoning":"Analyze and continue."}',
            )

    class _ProbeEmitter:
        def emit_reasoning_start(self, _step: str = "thinking") -> None:
            timeline.append("reasoning_start")

        def emit_reasoning_delta(self, _content: str) -> None:
            return None

        def emit_reasoning_section_end(self, _section_name: str = "thinking") -> None:
            timeline.append("reasoning_section_end")

        async def stream_reasoning(self, *_args, **_kwargs) -> str:
            timeline.append("stream_reasoning_call")
            raise RuntimeError("stream path failed")

    monkeypatch.setattr(
        think_more_module.EventEmitterFactory,
        "create",
        lambda *_args, **_kwargs: _ProbeEmitter(),
    )
    monkeypatch.setattr(
        section_module.EventEmitterFactory,
        "create",
        lambda *_args, **_kwargs: _ProbeEmitter(),
    )
    monkeypatch.setattr(
        think_more_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _FallbackLLM(),
    )
    monkeypatch.setattr(
        think_more_module,
        "get_llm_reasoning_effort",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(think_more_module, "wait_for_with_timeout", _await_passthrough)

    await think_more_module.think_more_node(
        _planning_state(),
        context=None,
        config=None,
        writer=lambda _event: None,
    )

    assert "stream_reasoning_call" in timeline, f"missing stream call marker, timeline={timeline}"
    assert "fallback_llm_call" in timeline, f"missing fallback marker, timeline={timeline}"
    assert "reasoning_start" in timeline, f"missing reasoning_start, timeline={timeline}"
    assert "reasoning_section_end" in timeline, f"missing reasoning_section_end, timeline={timeline}"

    start_idx = timeline.index("reasoning_start")
    fallback_idx = timeline.index("fallback_llm_call")
    end_idx = timeline.index("reasoning_section_end")

    assert start_idx < fallback_idx < end_idx, (
        "non-streaming think_more fallback should run inside a bounded reasoning section "
        f"(timeline={timeline})"
    )


@pytest.mark.asyncio
async def test_think_more_stream_refusal_does_not_call_non_stream_fallback(
    monkeypatch,
) -> None:
    from agent.graph.nodes import think_more as think_more_module
    from agent.providers.llm.core.exceptions import LLMRefusalError

    fallback_calls = 0
    refusal = LLMRefusalError(
        "Provider declined the request",
        provider="openai",
        model="gpt-5",
        explanation="Blocked by policy.",
    )

    class _FallbackLLM:
        async def chat_with_usage(self, *_args, **_kwargs):
            nonlocal fallback_calls
            fallback_calls += 1
            return _FakeLLMResponse()

    class _RefusingEmitter:
        async def stream_reasoning(self, *_args, **_kwargs) -> str:
            raise refusal

    monkeypatch.setattr(
        think_more_module.EventEmitterFactory,
        "create",
        lambda *_args, **_kwargs: _RefusingEmitter(),
    )
    monkeypatch.setattr(
        think_more_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _FallbackLLM(),
    )
    monkeypatch.setattr(
        think_more_module,
        "get_llm_reasoning_effort",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await think_more_module.think_more_node(
            _planning_state(),
            context=None,
            config=None,
            writer=lambda _event: None,
        )

    assert exc_info.value is refusal
    assert fallback_calls == 0
