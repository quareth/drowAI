"""Tests for Scout's direct native tool-batch builder."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.providers.llm.core.base import ToolCall as ProviderToolCall
from agent.providers.llm.core.base import ToolCallResult
from agent.subagents.contracts import AgentAssignment, AgentRuntimeIdentity
from agent.subagents.scout.nodes.choose_action import (
    SCOUT_ACTION_METADATA_KEY,
    SCOUT_EXECUTION_STRATEGY_KEY,
    ScoutActionSelectionError,
    choose_scout_action,
)
from agent.subagents.scout.profile import ScoutToolProfile, ScoutToolSpec
from agent.subagents.scout.state import build_scout_initial_state
from agent.tools.tool_call_specs import make_function_name_for_tool
from core.prompts.builders.scout_tool_builder import ScoutToolBuilderPromptBuilder
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder
from core.prompts.tests._golden import assert_golden


FPING_TOOL_ID = "information_gathering.network_discovery.fping"
NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"


class _FakeUsage:
    """Minimal usage value accepted by the shared graph usage converter."""

    def to_dict(self, source: str) -> dict[str, Any]:
        return {
            "source": source,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }


class _FakeBuilderLLM:
    """Return configured native calls and capture the single builder request."""

    def __init__(
        self,
        calls: list[ProviderToolCall],
        *,
        with_usage: bool = False,
    ) -> None:
        self.calls = calls
        self.with_usage = with_usage
        self.requests: list[dict[str, Any]] = []

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> ToolCallResult:
        self.requests.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            }
        )
        return ToolCallResult(
            content=None,
            tool_calls=self.calls,
            raw=None,
            usage=_FakeUsage() if self.with_usage else None,
        )


def _runtime_identity() -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        user_id=3,
        workspace_id="task-42",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        feature_flags={},
    )


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assign-1",
        agent_run_id="run-1",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map live hosts on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(),
    )


def _state() -> dict[str, Any]:
    profile = ScoutToolProfile(
        tools=(
            ScoutToolSpec(
                tool_id=FPING_TOOL_ID,
                display_name="fping",
                scout_capabilities=("host_discovery",),
            ),
            ScoutToolSpec(
                tool_id=NMAP_TOOL_ID,
                display_name="nmap",
                scout_capabilities=("port_scan", "service_enum"),
            ),
        )
    )
    return build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=profile,
    )


def _native_call(
    tool_id: str,
    *,
    parameters: dict[str, Any],
    strategy: str,
    intent: str,
    call_id: str,
) -> ProviderToolCall:
    return ProviderToolCall(
        id=call_id,
        name=make_function_name_for_tool(tool_id),
        arguments=json.dumps(
            {
                **parameters,
                "_builder_intent": intent,
                SCOUT_EXECUTION_STRATEGY_KEY: strategy,
            }
        ),
    )


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    llm: _FakeBuilderLLM,
) -> None:
    monkeypatch.setattr(
        "agent.subagents.scout.nodes.choose_action.resolve_llm_client",
        lambda *_args, **_kwargs: llm,
    )


@pytest.mark.asyncio
async def test_choose_action_binds_all_tools_in_one_builder_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="sequential",
                intent="Check whether the approved host responds.",
                call_id="provider-call-1",
            )
        ],
        with_usage=True,
    )
    _patch_llm(monkeypatch, llm)

    update = await choose_scout_action(_state())

    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert not hasattr(llm, "chat_with_usage")
    assert request["kwargs"]["tool_choice"] == "required"
    assert request["kwargs"]["parallel_tool_calls"] is True
    assert [spec.tool_id for spec in request["kwargs"]["tools"]] == [
        FPING_TOOL_ID,
        NMAP_TOOL_ID,
    ]
    for spec in request["kwargs"]["tools"]:
        assert SCOUT_EXECUTION_STRATEGY_KEY in spec.parameters_schema["required"]

    metadata = update["facts"]["metadata"]
    assert metadata["planned_execution_strategy"] == "sequential"
    assert metadata["planner_plan"]["selected_tools"] == [FPING_TOOL_ID]
    assert len(update["trace"]["usage_records"]) == 1
    assert update["trace"]["usage_records"][0]["source"] == "scout_tool_builder"


@pytest.mark.asyncio
async def test_choose_action_emits_attributed_reasoning_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "80"},
                strategy="sequential",
                intent="Check the approved target on port 80.",
                call_id="provider-call-1",
            )
        ]
    )
    _patch_llm(monkeypatch, llm)
    events: list[dict[str, Any]] = []

    await choose_scout_action(_state(), writer=events.append)

    assert [event["type"] for event in events] == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_delta",
        "reasoning_section_end",
    ]
    assert events[0]["step"] == "scout_action_selection"
    assert events[1]["content"] == (
        "Selecting reconnaissance tools and preparing the execution batch."
    )
    assert events[2]["content"] == "Check the approved target on port 80."
    assert events[3]["section_name"] == "scout_action_selection"
    assert all(event["ind"] == 0 for event in events)
    assert all(event["agent_run_id"] == "run-1" for event in events)
    assert all(event["producer_type"] == "subagent" for event in events)


@pytest.mark.asyncio
async def test_choose_action_closes_reasoning_when_builder_output_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM([])
    _patch_llm(monkeypatch, llm)
    events: list[dict[str, Any]] = []

    with pytest.raises(ScoutActionSelectionError, match="returned no calls"):
        await choose_scout_action(_state(), writer=events.append)

    assert [event["type"] for event in events] == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]
    assert events[-1]["section_name"] == "scout_action_selection"


@pytest.mark.asyncio
async def test_choose_action_commits_parallel_calls_without_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="parallel",
                intent="Check host liveness.",
                call_id="provider-call-1",
            ),
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "80,443"},
                strategy="parallel",
                intent="Check the specified web ports.",
                call_id="provider-call-2",
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    update = await choose_scout_action(_state())

    metadata = update["facts"]["metadata"]
    assert metadata[SCOUT_ACTION_METADATA_KEY]["tool_ids"] == [
        FPING_TOOL_ID,
        NMAP_TOOL_ID,
    ]
    plan = metadata["planner_plan"]
    assert plan["execution_strategy"] == "parallel"
    assert [
        call["tool_id"] for call in plan["tool_batch"]["tool_calls"]
    ] == [FPING_TOOL_ID, NMAP_TOOL_ID]
    assert SCOUT_EXECUTION_STRATEGY_KEY not in (
        plan["tool_batch"]["tool_calls"][0]["parameters"]
    )


@pytest.mark.asyncio
async def test_choose_action_preserves_repeated_sequential_calls_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "80"},
                strategy="sequential",
                intent="Check port 80 first.",
                call_id="provider-call-1",
            ),
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "443"},
                strategy="sequential",
                intent="Then check port 443.",
                call_id="provider-call-2",
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    update = await choose_scout_action(_state())

    plan = update["facts"]["metadata"]["planner_plan"]
    assert plan["execution_strategy"] == "sequential"
    assert plan["selected_tools"] == [NMAP_TOOL_ID, NMAP_TOOL_ID]
    assert [
        call["parameters"]["ports"]
        for call in plan["tool_batch"]["tool_calls"]
    ] == ["80", "443"]


@pytest.mark.asyncio
async def test_choose_action_rejects_inconsistent_batch_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="parallel",
                intent="Check host liveness.",
                call_id="provider-call-1",
            ),
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "80"},
                strategy="sequential",
                intent="Check port 80.",
                call_id="provider-call-2",
            ),
        ]
    )
    _patch_llm(monkeypatch, llm)

    with pytest.raises(ScoutActionSelectionError, match="inconsistent"):
        await choose_scout_action(_state())


@pytest.mark.asyncio
async def test_choose_action_normalizes_parallel_single_call_to_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="parallel",
                intent="Check host liveness.",
                call_id="provider-call-1",
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    update = await choose_scout_action(_state())

    assert (
        update["facts"]["metadata"]["planner_plan"]["execution_strategy"]
        == "sequential"
    )


@pytest.mark.asyncio
async def test_choose_action_rejects_calls_above_shared_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [
        _native_call(
            FPING_TOOL_ID,
            parameters={"target": f"10.0.0.{index}"},
            strategy="parallel",
            intent=f"Check host {index}.",
            call_id=f"provider-call-{index}",
        )
        for index in range(1, 5)
    ]
    llm = _FakeBuilderLLM(calls)
    _patch_llm(monkeypatch, llm)

    with pytest.raises(ScoutActionSelectionError, match="exceeded"):
        await choose_scout_action(_state())


@pytest.mark.asyncio
async def test_choose_action_rejects_unbound_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeBuilderLLM(
        [
            ProviderToolCall(
                id="provider-call-1",
                name="tool__shell_exec",
                arguments=json.dumps(
                    {
                        "command": "id",
                        SCOUT_EXECUTION_STRATEGY_KEY: "sequential",
                    }
                ),
            )
        ]
    )
    _patch_llm(monkeypatch, llm)

    with pytest.raises(ScoutActionSelectionError, match="unbound function"):
        await choose_scout_action(_state())


def test_scout_prompt_reuses_only_selector_independent_builder_sections() -> None:
    canonical_builder = ToolPlanningPromptBuilder()
    exact_shared = canonical_builder.build_native_tool_call_shared_guidance(
        max_committed_tools_per_batch=3,
    )
    scout_prompt = ScoutToolBuilderPromptBuilder().build_system_prompt(
        max_committed_tools_per_batch=3,
    )

    assert exact_shared in scout_prompt
    assert "Call between 1 and 3 candidate tool function(s)" in scout_prompt
    assert "Per-call intent (`_builder_intent`)" in scout_prompt
    assert "<execution_strategy_guidance>" in scout_prompt
    assert "Pattern: parallel, same tool" in scout_prompt
    assert "Pattern: sequential, different tools" in scout_prompt
    assert "Pattern: dependency, not batchable" in scout_prompt
    assert "Commit rules:" in scout_prompt
    assert "upstream selector" not in scout_prompt
    assert "Selector Decision" not in scout_prompt


def test_scout_prompt_exact_current_system_rendering_is_locked() -> None:
    scout_prompt = ScoutToolBuilderPromptBuilder().build_system_prompt(
        max_committed_tools_per_batch=3,
    )

    assert_golden("scout_tool_builder__system.txt", scout_prompt)
