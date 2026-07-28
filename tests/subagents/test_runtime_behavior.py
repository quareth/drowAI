"""Behavior tests for the generic definition-configured subagent runtime."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent.config import AgentConfig
from agent.graph.state import InteractiveState, ToolExecutionRecord
from agent.providers.llm.core.base import ToolCall as ProviderToolCall
from agent.providers.llm.core.base import ToolCallResult
from agent.subagents.contracts import AgentAssignment, AgentRuntimeIdentity
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.complete import complete_subagent_result
from agent.subagents.runtime.model import (
    SUBAGENT_ACTION_METADATA_KEY,
    SUBAGENT_EXECUTION_STRATEGY_KEY,
    SUBAGENT_OBSERVATION_TRANSCRIPT_KEY,
    SUBAGENT_RESULT_METADATA_KEY,
    SubagentToolBuilderPromptBuilder,
    record_subagent_observation_and_budget,
    run_subagent_model_turn,
)
from agent.subagents.runtime import model as runtime_model
from agent.subagents.runtime.profile import (
    SubagentToolProfile,
    SubagentToolSpec,
    resolve_subagent_tool_profile,
)
from agent.subagents.runtime.state import (
    SUBAGENT_METADATA_KEY,
    build_subagent_initial_state,
    subagent_state_from_graph_state,
)
from agent.tools.tool_call_specs import make_function_name_for_tool
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
    """Return configured native calls and capture the builder request."""

    def __init__(
        self,
        calls: list[ProviderToolCall] | None = None,
        *,
        content: str | None = None,
    ) -> None:
        self.calls = calls
        self.content = content
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
            content=self.content,
            tool_calls=self.calls,
            raw=None,
            usage=_FakeUsage(),
        )


def _pathfinder_definition() -> SubagentDefinition:
    [pathfinder] = [
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    ]
    return pathfinder


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
        agent_id="pathfinder",
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


def _profile() -> SubagentToolProfile:
    return SubagentToolProfile(
        tools=(
            SubagentToolSpec(
                tool_id=FPING_TOOL_ID,
                display_name="fping",
                capabilities=("host_discovery",),
            ),
            SubagentToolSpec(
                tool_id=NMAP_TOOL_ID,
                display_name="nmap",
                capabilities=("port_scanning", "service_enumeration"),
            ),
        )
    )


def _generic_state() -> dict[str, Any]:
    return build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )


def _native_call(
    tool_id: str,
    *,
    parameters: dict[str, Any],
    strategy: str,
    intent: str,
) -> ProviderToolCall:
    return ProviderToolCall(
        id="provider-call-1",
        name=make_function_name_for_tool(tool_id),
        arguments=json.dumps(
            {
                **parameters,
                "_builder_intent": intent,
                SUBAGENT_EXECUTION_STRATEGY_KEY: strategy,
            }
        ),
    )


def test_runtime_profile_resolves_definition_owned_tools() -> None:
    definition = _pathfinder_definition()

    profile = resolve_subagent_tool_profile(definition, definition.tool_ids)

    assert profile.tool_ids == definition.tool_ids
    assert profile.capabilities_for_tool(FPING_TOOL_ID) == ("host_discovery",)
    assert profile.capabilities_for_tool(NMAP_TOOL_ID) == (
        "port_scanning",
        "service_enumeration",
    )


def test_runtime_initial_state_uses_generic_metadata_key() -> None:
    state = _generic_state()
    metadata = state["facts"]["metadata"]

    assert SUBAGENT_METADATA_KEY == "subagent"
    assert set(metadata) >= {
        "agent_id",
        "agent_kind",
        "agent_display_name",
        "subagent",
    }
    assert "scout" not in metadata
    assert (
        subagent_state_from_graph_state(
            state,
            definition=_pathfinder_definition(),
        ).model_dump(mode="json")
        == metadata["subagent"]
    )


def test_runtime_model_prompt_matches_current_pathfinder_golden() -> None:
    definition = _pathfinder_definition()
    prompt = SubagentToolBuilderPromptBuilder(definition).build_system_prompt(
        max_committed_tools_per_batch=AgentConfig().max_committed_tools_per_batch
    )

    assert_golden("subagent_tool_builder__system.txt", prompt)


def test_runtime_model_user_prompt_matches_current_pathfinder_golden() -> None:
    definition = _pathfinder_definition()
    prompt = SubagentToolBuilderPromptBuilder(definition).build_user_prompt(
        assignment=_assignment().model_dump(mode="json"),
        tool_ids=_profile().tool_ids,
        working_memory={
            "findings": ["prior ping sweep found one host"],
            "todos": ["confirm exposed services"],
        },
        previous_tool_summary={
            "tool": FPING_TOOL_ID,
            "summary": "10.0.0.10 responded",
            "key_findings": ["host alive"],
        },
    )

    assert_golden("subagent_tool_builder__user.txt", prompt)


def test_runtime_model_prompt_identity_and_boundaries_come_from_definition() -> None:
    definition = replace(
        _pathfinder_definition(),
        id="artifact_auditor",
        display_name="ArtifactAuditor",
        kind="audit",
        description="Inspect generated artifacts and report integrity notes.",
        ownership_boundary="Own only artifact integrity review.",
        supported_task_categories=("artifact_review",),
        excluded_task_categories=("network_reconnaissance",),
        icon="artifact_auditor",
        instructions=(
            "You are ArtifactAuditor, a generated-artifact review subagent.\n"
            "Inspect only assigned artifacts."
        ),
        tool_builder_role_prompt=(
            "You are ArtifactAuditor, a generated-artifact review subagent.\n"
            "Emit artifact review notes only."
        ),
        tool_builder_boundary_rules=(
            "Use only the assigned artifacts and review objective.",
            "Do not scan networks, modify files, or produce final user-facing reports.",
        ),
    )

    prompt = SubagentToolBuilderPromptBuilder(definition).build_system_prompt(
        max_committed_tools_per_batch=2
    )

    assert prompt.startswith(
        "You are ArtifactAuditor, a generated-artifact review subagent.\n"
        "Emit artifact review notes only.\n\n"
    )
    assert (
        "ArtifactAuditor boundaries:\n"
        "- Use only the assigned artifacts and review objective.\n"
        "- Do not scan networks, modify files, or produce final user-facing reports."
    ) in prompt
    assert "Pathfinder" not in prompt
    assert "bounded recon subagent" not in prompt


@pytest.mark.asyncio
async def test_runtime_model_records_generic_route_metadata_and_call_topology() -> None:
    calls = [
        _native_call(
            FPING_TOOL_ID,
            parameters={"target": "10.0.0.10"},
            strategy="sequential",
            intent="Check whether the approved host responds.",
        )
    ]
    llm = _FakeBuilderLLM(calls)
    resolver_calls: list[dict[str, Any]] = []

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        _generic_state(),
        llm_resolver=lambda *args, **kwargs: (
            resolver_calls.append({"args": args, "kwargs": kwargs}) or llm
        ),
    )

    metadata = update["facts"]["metadata"]
    action = metadata[SUBAGENT_ACTION_METADATA_KEY]
    assert SUBAGENT_ACTION_METADATA_KEY == "subagent_action"
    assert action["route"] == "tool"
    assert action["agent_id"] == "pathfinder"
    assert action["tool_ids"] == [FPING_TOOL_ID]
    assert action["tool_batch_id"].startswith("subagent-batch-")
    assert metadata["planner_plan"]["tool_batch"]["tool_calls"][0][
        "tool_call_id"
    ].startswith("subagent-call-")
    assert update["trace"]["usage_records"][0]["source"] == "subagent_runtime_model"
    assert len(resolver_calls) == 1
    assert resolver_calls[0]["kwargs"]["role"] == "reasoning_main"
    assert len(llm.requests) == 1
    request = _request_projection(llm.requests[0])
    assert request["tool_ids"] == [
        FPING_TOOL_ID,
        NMAP_TOOL_ID,
    ]
    assert request["kwargs"] == {
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "temperature": 0.1,
        "max_tokens": 5000,
    }
    assert "Remaining Limits:" in request["user_prompt"]
    assert '"remaining_iterations": 3' in request["user_prompt"]
    assert '"remaining_tool_calls_this_iteration": 3' in request["user_prompt"]
    assert all(
        SUBAGENT_EXECUTION_STRATEGY_KEY in required
        for required in request["required"]
    )


@pytest.mark.asyncio
async def test_runtime_model_builder_appends_one_usage_record_after_existing() -> None:
    calls = [
        _native_call(
            FPING_TOOL_ID,
            parameters={"target": "10.0.0.10"},
            strategy="sequential",
            intent="Check whether the approved host responds.",
        )
    ]
    llm = _FakeBuilderLLM(calls)
    state = _generic_state()
    existing_usage = {
        "source": "pre_existing_parent_call",
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    state["trace"]["usage_records"] = [existing_usage]

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        state,
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    assert update["trace"]["usage_records"] == [
        existing_usage,
        {
            "source": "subagent_runtime_model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "request_mode": "non_streaming",
        },
    ]


@pytest.mark.asyncio
async def test_runtime_resolver_injection_avoids_global_cross_run_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [
        _native_call(
            FPING_TOOL_ID,
            parameters={"target": "10.0.0.10"},
            strategy="sequential",
            intent="Check whether the approved host responds.",
        )
    ]
    llm = _FakeBuilderLLM(calls)

    def _poison_resolver(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("global runtime resolver leaked into injected run")

    monkeypatch.setattr(runtime_model, "resolve_llm_client", _poison_resolver)

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        _generic_state(),
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    assert runtime_model.resolve_llm_client is _poison_resolver
    assert len(llm.requests) == 1
    assert update["facts"]["metadata"][SUBAGENT_ACTION_METADATA_KEY][
        "tool_batch_id"
    ].startswith("subagent-batch-")


@pytest.mark.asyncio
async def test_runtime_model_accepts_text_handoff_without_tool_call() -> None:
    llm = _FakeBuilderLLM(
        [],
        content="Pathfinder found one live host and recommends service enumeration.",
    )

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        _generic_state(),
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    metadata = update["facts"]["metadata"]
    action = metadata[SUBAGENT_ACTION_METADATA_KEY]
    assert action["route"] == "handoff"
    assert action["forced_final"] is False
    assert SUBAGENT_RESULT_METADATA_KEY not in metadata
    assert update["trace"]["final_text"] == (
        "Pathfinder found one live host and recommends service enumeration."
    )

    completed = complete_subagent_result(_pathfinder_definition(), update)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    assert result["outcome"] == "completed"
    assert result["summary"] == update["trace"]["final_text"]


@pytest.mark.asyncio
async def test_runtime_model_forces_text_handoff_when_iteration_budget_exhausted() -> None:
    state = _generic_state()
    state["facts"]["iterations"] = _pathfinder_definition().max_iterations
    state["facts"]["last_tool_result_compact"] = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "fping found one live host.",
        "key_findings": ["10.0.0.10 is alive."],
    }
    state["facts"]["metadata"]["last_tool_result_compact"] = state["facts"][
        "last_tool_result_compact"
    ]
    state["facts"]["metadata"]["synthesized_output"] = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "fping found one live host.",
        "key_findings": ["10.0.0.10 is alive."],
        "next_actions": ["Run service enumeration next."],
    }
    llm = _FakeBuilderLLM([], content="Budget exhausted after host discovery.")

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        state,
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    request = _request_projection(llm.requests[0])
    assert request["tool_ids"] == []
    assert request["kwargs"] == {
        "tool_choice": "none",
        "temperature": 0.1,
        "max_tokens": 5000,
    }
    metadata = update["facts"]["metadata"]
    assert metadata[SUBAGENT_ACTION_METADATA_KEY]["route"] == "handoff"
    assert metadata[SUBAGENT_OBSERVATION_TRANSCRIPT_KEY][-1]["summary"] == (
        "fping found one live host."
    )

    completed = complete_subagent_result(_pathfinder_definition(), update)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    assert result["outcome"] == "partial"
    assert result["key_findings"] == ["10.0.0.10 is alive."]
    assert result["limitations"] == [
        "Subagent tool or iteration budget was exhausted."
    ]
    assert result["recommended_next_steps"] == ["Run service enumeration next."]


def test_runtime_records_completed_execution_budget_once_by_batch_identity() -> None:
    state = _generic_state()
    state["facts"]["metadata"][SUBAGENT_ACTION_METADATA_KEY] = {
        "route": "tool",
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "tool_batch_id": "batch-1",
    }
    state["facts"]["metadata"]["last_tool_result_compact"] = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "same observation",
        "key_findings": ["same finding"],
    }
    state["facts"]["metadata"]["synthesized_output"] = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "same observation",
        "key_findings": ["same finding"],
    }

    first = record_subagent_observation_and_budget(_pathfinder_definition(), state)
    second = record_subagent_observation_and_budget(_pathfinder_definition(), first)
    second["facts"]["metadata"][SUBAGENT_ACTION_METADATA_KEY]["tool_batch_id"] = (
        "batch-2"
    )
    third = record_subagent_observation_and_budget(_pathfinder_definition(), second)

    assert first["facts"]["iterations"] == 1
    assert second["facts"]["iterations"] == 1
    assert third["facts"]["iterations"] == 2
    assert len(third["facts"]["metadata"][SUBAGENT_OBSERVATION_TRANSCRIPT_KEY]) == 1


def test_runtime_completion_projects_generic_result_metadata() -> None:
    interactive = InteractiveState.from_mapping(_generic_state())
    interactive.facts.last_tool_result_compact = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "fping found one live host.",
        "key_findings": ["10.0.0.10 is alive."],
        "report_recommendations": ["Run service enumeration next."],
        "artifact_refs": [{"path": "/workspace/fping.json", "label": "fping"}],
    }
    interactive.facts.metadata["last_tool_result_compact"] = (
        interactive.facts.last_tool_result_compact
    )
    interactive.trace.executed_tools.append(
        ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success")
    )

    update = complete_subagent_result(
        _pathfinder_definition(),
        interactive.as_graph_state(),
    )

    metadata = update["facts"]["metadata"]
    result = metadata[SUBAGENT_RESULT_METADATA_KEY]
    assert SUBAGENT_RESULT_METADATA_KEY == "subagent_result"
    assert result["agent_run_id"] == "run-1"
    assert result["agent_id"] == "pathfinder"
    assert result["agent_kind"] == "recon"
    assert result["summary"] == "fping found one live host."
    assert update["trace"]["history"][-1]["type"] == "subagent_result"


def test_runtime_modules_do_not_import_backend_services() -> None:
    runtime_root = Path("agent/subagents/runtime")
    for path in runtime_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "backend" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("backend")


def _request_projection(request: dict[str, Any]) -> dict[str, Any]:
    kwargs = dict(request["kwargs"])
    tools = kwargs.pop("tools")
    return {
        "system_prompt": request["system_prompt"],
        "user_prompt": request["user_prompt"],
        "kwargs": kwargs,
        "tool_ids": [tool.tool_id for tool in tools],
        "required": [tool.parameters_schema["required"] for tool in tools],
    }
