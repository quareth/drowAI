""": HITL approval-to-dispatch flow tests.

Verifies the shared approval_gate -> dispatch_tool contract works correctly
when approval is pre-set (simulating post-resume state)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
)


def _state_with_approval_pre_set(capability: str = "simple_tool_execution") -> dict:
    """State simulating post-resume: approval already completed."""
    planner_plan = {
        "selected_tools": ["shell.exec"],
        "tool_parameters": {"shell.exec": {"command": "echo ok"}},
        "execution_strategy": "sequential",
    }
    facts = FactsState(
        task_id=1,
        message="run echo",
        capability=capability,
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo ok"}},
        metadata={
            "agent_mode": "agent",
            "planner_plan": planner_plan,
            "tool_plan_prepared": True,
            "tool_approval_gate_completed": True,
            "tool_approval_response": {"action": "approve"},
            # Phase 5 cutover: the hot-path ConversationContextBundle is
            # required by the tool-execution request-context builder.
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-hitl",
                turn_id="turn-hitl",
                turn_sequence=0,
                messages=[],
            ),
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _state_with_two_call_batch_needing_approval() -> dict:
    tool_id = "information_gathering.network_discovery.nmap"
    planner_plan = {
        "selected_tools": [tool_id, tool_id],
        "tool_parameters": {tool_id: {"target": "127.0.0.1", "ports": "443"}},
        "execution_strategy": "parallel",
        "tool_batch": {
            "tool_batch_id": "tb_nmap",
            "requested_execution_strategy": "parallel",
            "tool_calls": [
                {
                    "tool_call_id": "tc_80",
                    "tool_id": tool_id,
                    "parameters": {"target": "127.0.0.1", "ports": "80"},
                    "intent": "scan 80",
                },
                {
                    "tool_call_id": "tc_443",
                    "tool_id": tool_id,
                    "parameters": {"target": "127.0.0.1", "ports": "443"},
                    "intent": "scan 443",
                },
            ],
        },
    }
    facts = FactsState(
        task_id=1,
        message="scan two ports",
        capability="simple_tool_execution",
        selected_tool=tool_id,
        tool_parameters={tool_id: {"target": "127.0.0.1", "ports": "443"}},
        metadata={
            "agent_mode": "agent",
            "planner_plan": planner_plan,
            "tool_plan_prepared": True,
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-hitl",
                turn_id="turn-hitl",
                turn_sequence=0,
                messages=[],
            ),
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _fake_outcome() -> SimpleNamespace:
    def _to_graph_metadata() -> dict:
        return {"tool_id": "shell.exec", "result": {"success": True}}

    return SimpleNamespace(
        tool_id="shell.exec",
        parameters={"command": "echo ok"},
        result={
            "success": True,
            "status": "success",
            "stdout": "ok",
            "stderr": "",
            "observation": "ok",
            "duration": 1,
            "exit_code": 0,
        },
        catalog=[],
        reasoning=["Executed"],
        summary="ok",
        to_graph_metadata=_to_graph_metadata,
    )


@pytest.mark.asyncio
async def test_approval_gate_sends_full_tool_batch_items() -> None:
    """The HITL gate must not collapse duplicate tool IDs to one legacy row."""
    captured: dict = {}

    def _capture_approval(**kwargs):
        captured.update(kwargs)
        return {"action": "approve"}

    with patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=_capture_approval,
    ):
        result = await approval_gate_node(_state_with_two_call_batch_needing_approval())

    items = captured.get("items")
    assert captured["tool_batch_id"] == "tb_nmap"
    assert captured["tool_call_id"] == "tc_80"
    assert isinstance(items, list)
    assert [item["tool_call_id"] for item in items] == ["tc_80", "tc_443"]
    assert [item["parameters"]["ports"] for item in items] == ["80", "443"]

    interactive = InteractiveState.from_mapping(result)
    metadata = interactive.facts.metadata
    assert metadata["tool_batch_id"] == "tb_nmap"
    assert metadata["tool_approval_gate_completed"] is True


def _percentile(values: list[float], quantile: float) -> float:
    """Compute deterministic percentile using linear interpolation."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + ((ordered[high] - ordered[low]) * weight)


def _build_latency_report(mode: str, samples_ms: list[float]) -> dict:
    """Return baseline profile payload for reporting."""
    first = float(samples_ms[0]) if samples_ms else 0.0
    second = float(samples_ms[1]) if len(samples_ms) > 1 else first
    warm_samples = samples_ms[1:] if len(samples_ms) > 1 else samples_ms
    return {
        "graph_mode": mode,
        "profile_source": "synthetic_controlled",
        "first_approved_tool_latency_ms": first,
        "second_approved_tool_latency_ms": second,
        "cold_warm_gap_ms": max(0.0, first - second),
        "warm_p50_ms": _percentile(warm_samples, 0.50),
        "p50_ms": _percentile(samples_ms, 0.50),
        "p95_ms": _percentile(samples_ms, 0.95),
        "samples_ms": [round(float(v), 3) for v in samples_ms],
    }


@pytest.mark.asyncio
async def test_dispatch_after_approval_executes_without_re_approval() -> None:
    """Dispatch with pre-set approval executes tool without re-requesting approval."""
    state = _state_with_approval_pre_set()
    run_mock = AsyncMock(return_value=_fake_outcome())
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=AssertionError("dispatch should not request approval"),
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        side_effect=run_mock,
    ):
        result = await dispatch_tool_execution_node(state)

    run_mock.assert_awaited_once()
    interactive = InteractiveState.from_mapping(result)
    compact = interactive.facts.metadata.get("last_tool_result_compact", {})
    assert compact.get("tool") == "shell.exec"
    assert compact.get("success") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "mode_label"),
    [
        ("deep_reasoning", "deep_reasoning"),
        ("simple_tool_execution", "simple_tool"),
    ],
)
async def test_hitl_dispatch_latency_baseline_profile(
    capability: str,
    mode_label: str,
    record_property,
) -> None:
    """Task 1.2: quantify cold-vs-warm dispatch timing profile per graph mode."""
    cold_delay_sec = 0.030
    warm_delay_sec = 0.005
    iterations = 6
    call_count = {"value": 0}

    async def _run_side_effect(*_args, **_kwargs):
        call_count["value"] += 1
        await asyncio.sleep(cold_delay_sec if call_count["value"] == 1 else warm_delay_sec)
        return _fake_outcome()

    latencies_ms: list[float] = []
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=True,
    ), patch(
        "agent.graph.subgraphs.tool_execution.request_tool_approval",
        side_effect=AssertionError("dispatch should not request approval"),
    ), patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution._get_provenance_service",
        return_value=(None, None),
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        side_effect=_run_side_effect,
    ):
        for _ in range(iterations):
            state = _state_with_approval_pre_set(capability=capability)
            started = time.perf_counter()
            await dispatch_tool_execution_node(state)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            latencies_ms.append(elapsed_ms)

    report = _build_latency_report(mode_label, latencies_ms)
    record_property(
        f"hitl_dispatch_latency_baseline_{mode_label}",
        json.dumps(report, sort_keys=True),
    )

    # Compare against warm median instead of only the second sample to avoid
    # occasional scheduler jitter flaking in CI.
    assert report["first_approved_tool_latency_ms"] > report["warm_p50_ms"]
    assert report["cold_warm_gap_ms"] >= 10.0
    assert report["p95_ms"] >= report["p50_ms"]
