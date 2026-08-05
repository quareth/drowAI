"""Behavior tests for the generic definition-configured subagent runtime."""

from __future__ import annotations

import ast
import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent.context.token_counter_registry import estimate_text_tokens
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.context.serialization import render_completed_agent_results_section
from agent.graph.memory.memory_manager import MemoryManager
from agent.graph.nodes.hitl_helpers import should_require_approval
from agent.graph.subgraphs.tool_execution_runtime.batch_result_application import (
    finish_with_batch_result,
)
from agent.graph.subgraphs.tool_execution_runtime.batch_runner import (
    deserialize_tool_batch_from_plan_data,
)
from agent.graph.state import InteractiveState, ToolExecutionRecord
from agent.graph.utils import iteration_memory
from agent.providers.llm.core.base import ToolCall as ProviderToolCall
from agent.providers.llm.core.base import ToolCallResult
from agent.subagents.contracts import (
    AgentAssignment,
    AgentResultProjection,
    AgentRuntimeIdentity,
)
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime import model as runtime_model
from agent.subagents.runtime.complete import (
    SUBAGENT_RESULT_PROJECTION_METADATA_KEY,
    complete_subagent_result,
)
from agent.subagents.runtime.model import (
    SUBAGENT_ACTION_METADATA_KEY,
    SUBAGENT_EXECUTION_STRATEGY_KEY,
    SUBAGENT_FORCED_FINAL_METADATA_KEY,
    SUBAGENT_RESULT_METADATA_KEY,
    record_subagent_observation_and_budget,
    run_subagent_model_turn,
)
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
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolCallResult as BatchToolCallResult,
    ToolCallStatus,
)
from agent.tools.tool_call_specs import make_function_name_for_tool
from core.prompts.builders.subagent_runtime import SubagentRuntimePromptBuilder


FPING_TOOL_ID = "information_gathering.network_discovery.fping"
NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"
SHELL_EXEC_TOOL_ID = "shell.exec"
SHELL_WRITE_STDIN_TOOL_ID = "shell.write_stdin"


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


def _assignment(
    *,
    assignment_id: str = "assign-1",
    agent_run_id: str = "run-1",
    agent_mode: str | None = "full_access",
) -> AgentAssignment:
    relevant_context = {"ticket": "ENG-123"}
    if agent_mode is not None:
        relevant_context["agent_mode"] = agent_mode
    return AgentAssignment(
        assignment_id=assignment_id,
        agent_run_id=agent_run_id,
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
        relevant_context=relevant_context,
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


def _profile_with_universal_shell_tools() -> SubagentToolProfile:
    return SubagentToolProfile(
        tools=(
            *_profile().tools,
            SubagentToolSpec(
                tool_id=SHELL_EXEC_TOOL_ID,
                display_name="shell.exec",
                capabilities=(),
            ),
            SubagentToolSpec(
                tool_id=SHELL_WRITE_STDIN_TOOL_ID,
                display_name="shell.write_stdin",
                capabilities=(),
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


def _generic_state_with_universal_shell_tools() -> dict[str, Any]:
    return build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=_assignment(),
        graph_thread_id="child-thread-shell-utilities",
        tool_profile=_profile_with_universal_shell_tools(),
    )


def _generic_state_for_assignment(
    assignment: AgentAssignment,
    *,
    graph_thread_id: str,
) -> dict[str, Any]:
    return build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        tool_profile=_profile(),
    )


def test_subagent_initial_state_restores_parent_approval_policy() -> None:
    state = _generic_state_for_assignment(
        _assignment(agent_mode="agent"),
        graph_thread_id="child-thread-agent-mode",
    )

    metadata = state["facts"]["metadata"]
    assert metadata["agent_mode"] == "agent"
    assert should_require_approval(metadata) is True


def test_subagent_initial_state_rejects_missing_parent_approval_policy() -> None:
    with pytest.raises(
        ValueError,
        match="Subagent assignment is missing a valid agent_mode",
    ):
        _generic_state_for_assignment(
            _assignment(agent_mode=None),
            graph_thread_id="child-thread-missing-agent-mode",
        )


def _tool_iteration(
    *,
    tool_id: str,
    execution_id: str,
    summary: str,
    finding: str,
    next_action: str,
    report_recommendations: list[str] | None = None,
    status: str = "success",
    success: bool = True,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "execution_id": execution_id,
        "summary": summary,
        "finding": finding,
        "next_action": next_action,
        "report_recommendations": report_recommendations or [],
        "status": status,
        "success": success,
        "errors": errors or [],
    }


def _host_discovery_iteration(*, execution_id: str = "exec-fping-1") -> dict[str, Any]:
    return _tool_iteration(
        tool_id=FPING_TOOL_ID,
        execution_id=execution_id,
        summary="fping found 10.0.0.10 alive.",
        finding="10.0.0.10 is alive.",
        next_action="Run nmap service enumeration next.",
    )


def _service_enumeration_iteration(
    *,
    execution_id: str = "exec-nmap-2",
    report_recommendations: list[str] | None = None,
) -> dict[str, Any]:
    return _tool_iteration(
        tool_id=NMAP_TOOL_ID,
        execution_id=execution_id,
        summary="nmap found tcp/80 open on 10.0.0.10.",
        finding="10.0.0.10 exposes tcp/80.",
        next_action="Inspect HTTP response headers next.",
        report_recommendations=report_recommendations,
    )


def _failed_service_iteration() -> dict[str, Any]:
    return _tool_iteration(
        tool_id=NMAP_TOOL_ID,
        execution_id="exec-nmap-failed",
        summary="nmap timed out before completing service enumeration.",
        finding="Service enumeration was incomplete.",
        next_action="Retry with a narrower port set.",
        status="failed",
        success=False,
        errors=["nmap timed out after the configured deadline."],
    )


def _state_after_tool_iterations(
    iterations: list[dict[str, Any]],
) -> dict[str, Any]:
    state = _generic_state()
    for index, iteration in enumerate(iterations, start=1):
        state = _apply_tool_iteration_projection(state, iteration, index=index)
        state = record_subagent_observation_and_budget(
            _pathfinder_definition(),
            state,
        )
    return state


def _apply_tool_iteration_projection(
    state: dict[str, Any],
    iteration: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    facts = state["facts"]
    metadata = facts["metadata"]
    tool_id = iteration["tool_id"]
    execution_id = iteration["execution_id"]
    compact = {
        "schema_version": "2.0",
        "tool": tool_id,
        "status": iteration["status"],
        "success": iteration["success"],
        "exit_code": 0 if iteration["success"] else 1,
        "summary": iteration["summary"],
        "key_findings": [iteration["finding"]],
        "errors": iteration["errors"],
        "report_recommendations": iteration["report_recommendations"],
        "structured_signals": [],
        "decision_evidence": [f"fixture_iteration={index}"],
        "lossiness_risk": "low",
        "artifact_refs": [
            {
                "path": f"/workspace/artifacts/{execution_id}.json",
                "label": execution_id,
            }
        ],
    }
    last_result = {
        "tool": tool_id,
        "parameters": {"target": "10.0.0.10"},
        "status": iteration["status"],
        "success": iteration["success"],
        "exit_code": compact["exit_code"],
    }
    metadata["turn_sequence"] = 1
    metadata["last_tool_result"] = last_result
    metadata["last_tool_result_compact"] = compact
    metadata["last_tool_result_compact_batch"] = {
        "tool_batch_id": f"batch-{execution_id}",
        "execution_strategy": "sequential",
        "status": "completed",
        "success": True,
        "results": [
            {
                "tool_call_id": f"tc-{execution_id}",
                "tool_id": tool_id,
                "intent": f"Run {tool_id}",
                "status": iteration["status"],
                "success": iteration["success"],
                "compact_tool_result": compact,
            }
        ],
    }
    metadata["synthesized_output"] = {
        **compact,
        "next_actions": [iteration["next_action"]],
    }
    metadata[SUBAGENT_ACTION_METADATA_KEY] = {
        "route": "tool",
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "tool_batch_id": f"batch-{execution_id}",
    }
    metadata["working_memory"] = MemoryManager.reduce_tool_result(
        previous=metadata.get("working_memory"),
        tool_id=tool_id,
        tool_params={"target": "10.0.0.10"},
        compact_envelope=compact,
        artifact_refs=compact["artifact_refs"],
        execution_id=execution_id,
        observed_findings=[
            {
                "kind": "host_up",
                "target": "10.0.0.10",
                "subject": "10.0.0.10",
                "details": {"summary": iteration["finding"]},
                "assertion_level": "observed",
                "confidence": 1.0,
                "seen_at": 1_700_000_000 + index,
                "ttl_seconds": 600,
            }
        ],
    )
    iteration_memory.append(
        metadata,
        turn_sequence=1,
        source="tool",
        payload={
            "sections": [
                {
                    "heading": "Tool Executed",
                    "body": f"Tool: {tool_id}\nParameters: target=10.0.0.10",
                },
                {"heading": "Tool Output Summary", "body": iteration["summary"]},
                {"heading": "Key Findings", "body": f"- {iteration['finding']}"},
                {"heading": "Compression Lossiness", "body": "lossiness_risk: low"},
            ]
        },
    )
    facts["selected_tool"] = tool_id
    facts["tool_parameters"] = {"target": "10.0.0.10"}
    facts["last_tool_result_compact"] = compact
    return state


def _stable_json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _extract_prompt_json(user_prompt: str, label: str) -> dict[str, Any]:
    match = re.search(rf"^- {re.escape(label)}: (.+)$", user_prompt, re.MULTILINE)
    assert match is not None
    return json.loads(match.group(1))


def _extract_prompt_section_json(user_prompt: str, label: str) -> dict[str, Any]:
    match = re.search(
        rf"^{re.escape(label)}:\n(.+?)(?:\n\n|$)",
        user_prompt,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _assert_projection_excludes_private_state(
    value: Any,
    *additional_forbidden: str,
) -> None:
    projection_json = json.dumps(value)
    for forbidden in (
        "current_turn_phases",
        "subagent_observation_transcript",
        *additional_forbidden,
    ):
        assert forbidden not in projection_json


def _native_call(
    tool_id: str,
    *,
    parameters: dict[str, Any],
    strategy: str,
    intent: str,
    provider_call_id: str = "provider-call-1",
) -> ProviderToolCall:
    return ProviderToolCall(
        id=provider_call_id,
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
    bundle = metadata[METADATA_CONTEXT_BUNDLE_KEY]
    working_memory = metadata["working_memory"]

    assert SUBAGENT_METADATA_KEY == "subagent"
    assert set(metadata) >= {
        "agent_id",
        "agent_kind",
        "agent_display_name",
        "subagent",
    }
    assert "scout" not in metadata
    subagent_state = subagent_state_from_graph_state(
        state,
        definition=_pathfinder_definition(),
    )
    assert subagent_state.as_metadata() == metadata["subagent"]
    assert subagent_state.tool_profile.tool_ids == (FPING_TOOL_ID, NMAP_TOOL_ID)
    assert metadata["graph_thread_id"] == "child-thread-1"
    assert metadata["parent_graph_thread_id"] == "parent-thread-1"
    assert bundle["conversation_id"] == "conversation-1"
    assert bundle["turn_id"] == "turn-1"
    assert bundle["transcript_window"]["turns"] == []
    assert bundle["classifier_transcript_window"]["turns"] == []
    assert bundle["current_user_turn"] == {
        "role": "user",
        "content": "Map live hosts on the approved target.",
    }
    assert "conversation_history" not in metadata
    assert "subagent_observation_transcript" not in metadata
    assert set(metadata).isdisjoint(
        {"subagent_working_memory", "subagent_phase_memory", "child_memory"}
    )
    assert working_memory["ids"]["task_id"] == 42
    assert working_memory["ids"]["conversation_id"] == "conversation-1"
    assert working_memory["ids"]["parent_turn_id"] == "turn-1"
    assert working_memory["ids"]["parent_graph_thread_id"] == "parent-thread-1"
    assert working_memory["ids"]["graph_thread_id"] == "child-thread-1"
    assert working_memory["ids"]["agent_run_id"] == "run-1"
    assert working_memory["objective"] == {
        "text": "Map live hosts on the approved target.",
        "status": "active",
        "source": "intent_turn_interpretation",
        "provenance": {
            "authority": "derived",
            "source": "intent_turn_interpretation",
        },
    }
    assert working_memory["active"]["target_id"] == "target:intent:target"
    assert working_memory["referents"]["intent:target"]["value"] == "10.0.0.10"
    assert working_memory["constraints"]["scope"] == [
        "Approved internal test host only."
    ]
    assert "subagent" not in working_memory["constraints"]
    assert working_memory["constraints"]["tool_policy"] == {}
    assert working_memory["constraints"]["boundaries"] == []
    assert working_memory["stage"] == "tool_selection"
    assert working_memory["current_turn_phases"] == []
    assert working_memory["current_turn_phase_counter"] == 0
    assert bundle["runtime_state"]["active_target"] == {
        "target_id": "target:intent:target",
        "value": "10.0.0.10",
        "kind": "ip",
    }
    assert bundle["runtime_state"]["current_goal"] == {
        "text": "Map live hosts on the approved target.",
        "status": "active",
    }


def test_runtime_initial_state_seeds_independent_child_working_memory() -> None:
    first = _generic_state_for_assignment(
        _assignment(assignment_id="assign-1", agent_run_id="run-1"),
        graph_thread_id="child-thread-1",
    )
    second = _generic_state_for_assignment(
        _assignment(assignment_id="assign-2", agent_run_id="run-2"),
        graph_thread_id="child-thread-2",
    )

    first_memory = first["facts"]["metadata"]["working_memory"]
    second_memory = second["facts"]["metadata"]["working_memory"]

    assert first_memory is not second_memory
    assert first_memory["ids"]["agent_run_id"] == "run-1"
    assert second_memory["ids"]["agent_run_id"] == "run-2"
    assert first_memory["ids"]["graph_thread_id"] == "child-thread-1"
    assert second_memory["ids"]["graph_thread_id"] == "child-thread-2"
    first_memory["current_turn_phases"].append(
        {"turn_sequence": 1, "phase_sequence": 0, "source": "tool", "sections": []}
    )
    assert second_memory["current_turn_phases"] == []


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
        runtime_role_prompt=(
            "You are ArtifactAuditor, a generated-artifact review subagent.\n"
            "Emit artifact review notes only."
        ),
        runtime_boundary_rules=(
            "Use only the assigned artifacts and review objective.",
            "Do not scan networks, modify files, or produce final user-facing reports.",
        ),
    )

    prompt = SubagentRuntimePromptBuilder().build_system_prompt(
        definition_id=definition.id,
        display_name=definition.display_name,
        role_prompt=definition.runtime_role_prompt or definition.instructions,
        definition_instructions=definition.instructions,
        ownership_boundary=definition.ownership_boundary,
        boundary_rules=definition.runtime_boundary_rules
        or (definition.ownership_boundary,),
        max_committed_tools_per_batch=2,
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
async def test_runtime_model_exposes_and_commits_universal_shell_utilities() -> None:
    calls = [
        _native_call(
            SHELL_EXEC_TOOL_ID,
            parameters={"command": "printf ready"},
            strategy="sequential",
            intent="Start a bounded shell session for a quick runtime check.",
        )
    ]
    llm = _FakeBuilderLLM(calls)

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        _generic_state_with_universal_shell_tools(),
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    request = _request_projection(llm.requests[0])
    assert request["tool_ids"] == [
        FPING_TOOL_ID,
        NMAP_TOOL_ID,
        SHELL_EXEC_TOOL_ID,
        SHELL_WRITE_STDIN_TOOL_ID,
    ]
    assert SHELL_EXEC_TOOL_ID in request["schemas_by_tool_id"]
    assert SHELL_WRITE_STDIN_TOOL_ID in request["schemas_by_tool_id"]
    assert "Use shell.exec to start a command." in request["system_prompt"]
    assert "Use shell.write_stdin with chars=\"\" to poll" in request["system_prompt"]
    assert "run shells" not in request["system_prompt"]
    assert "run shells" not in request["user_prompt"]
    assert set(request["schemas_by_tool_id"][SHELL_EXEC_TOOL_ID]["properties"]) >= {
        "command",
        "cwd",
        "env",
        "yield_time_ms",
        "max_output_chars",
        "max_runtime_sec",
    }
    assert set(
        request["schemas_by_tool_id"][SHELL_WRITE_STDIN_TOOL_ID]["properties"]
    ) >= {
        "session_id",
        "chars",
        "yield_time_ms",
        "max_output_chars",
    }
    metadata = update["facts"]["metadata"]
    action = metadata[SUBAGENT_ACTION_METADATA_KEY]
    assert action["route"] == "tool"
    assert action["tool_ids"] == [SHELL_EXEC_TOOL_ID]
    assert update["facts"]["tool_candidates"] == request["tool_ids"]
    assert metadata["planner_plan"]["tool_batch"]["tool_calls"][0]["tool_id"] == (
        SHELL_EXEC_TOOL_ID
    )
    assert metadata["planner_plan"]["tool_batch"]["tool_calls"][0]["parameters"] == {
        "command": "printf ready",
        "cwd": None,
        "env": None,
        "yield_time_ms": 10000,
        "max_output_chars": 32000,
        "max_runtime_sec": 120,
    }


@pytest.mark.asyncio
async def test_runtime_model_commits_shell_write_stdin_for_running_shell_result() -> None:
    public_session_id = "shs_subagent_continuation_123"
    state = _generic_state_with_universal_shell_tools()
    metadata = state["facts"]["metadata"]
    metadata["last_tool_result"] = {
        "tool": SHELL_EXEC_TOOL_ID,
        "success": True,
        "status": "success",
        "process_status": "running",
        "session_id": public_session_id,
        "stdout": "started",
        "stderr": "",
        "exit_code": None,
        "stdin_available": True,
        "truncated": False,
        "summary": f"Command is still running; poll session {public_session_id}.",
        "parameters": {"command": "sleep 1; printf done"},
    }
    metadata["last_tool_result_compact"] = {
        "tool": SHELL_EXEC_TOOL_ID,
        "success": True,
        "status": "success",
        "process_status": "running",
        "session_id": public_session_id,
        "summary": f"Command is still running; poll session {public_session_id}.",
    }
    calls = [
        _native_call(
            SHELL_WRITE_STDIN_TOOL_ID,
            parameters={
                "session_id": public_session_id,
                "chars": "",
                "yield_time_ms": 1000,
                "max_output_chars": 32000,
            },
            strategy="sequential",
            intent="Poll the running shell session.",
        )
    ]
    llm = _FakeBuilderLLM(calls)

    update = await run_subagent_model_turn(
        _pathfinder_definition(),
        state,
        llm_resolver=lambda *_args, **_kwargs: llm,
    )

    request = _request_projection(llm.requests[0])
    assert SHELL_WRITE_STDIN_TOOL_ID in request["tool_ids"]
    assert public_session_id in request["user_prompt"]
    tool_call = update["facts"]["metadata"]["planner_plan"]["tool_batch"]["tool_calls"][0]
    assert tool_call["tool_id"] == SHELL_WRITE_STDIN_TOOL_ID
    assert tool_call["parameters"] == {
        "session_id": public_session_id,
        "chars": "",
        "yield_time_ms": 1000,
        "max_output_chars": 32000,
    }


@pytest.mark.asyncio
async def test_runtime_model_reconstructs_full_profile_after_committed_batch() -> None:
    first_llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="sequential",
                intent="Check whether the approved host responds.",
            )
        ]
    )
    after_fping = await run_subagent_model_turn(
        _pathfinder_definition(),
        _generic_state(),
        llm_resolver=lambda *_args, **_kwargs: first_llm,
    )

    assert after_fping["facts"]["tool_ids"] == [FPING_TOOL_ID]
    assert after_fping["facts"]["tool_candidates"] == [FPING_TOOL_ID, NMAP_TOOL_ID]

    next_llm = _FakeBuilderLLM(
        [
            _native_call(
                NMAP_TOOL_ID,
                parameters={"target": "10.0.0.10", "ports": "80,443"},
                strategy="sequential",
                intent="Enumerate services after host discovery.",
            )
        ]
    )
    next_update = await run_subagent_model_turn(
        _pathfinder_definition(),
        after_fping,
        llm_resolver=lambda *_args, **_kwargs: next_llm,
    )

    next_request = _request_projection(next_llm.requests[0])
    assert next_request["tool_ids"] == [FPING_TOOL_ID, NMAP_TOOL_ID]
    assert next_update["facts"]["metadata"][SUBAGENT_ACTION_METADATA_KEY][
        "tool_ids"
    ] == [NMAP_TOOL_ID]


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
    assert "subagent_observation_transcript" not in metadata

    completed = complete_subagent_result(_pathfinder_definition(), update)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    assert result["outcome"] == "partial"
    assert result["key_findings"] == ["10.0.0.10 is alive."]
    assert result["limitations"] == [
        "Subagent tool or iteration budget was exhausted."
    ]
    assert result["recommended_next_steps"] == ["Run service enumeration next."]


def test_runtime_syncs_completed_execution_budget_from_tool_phase_memory() -> None:
    state = _generic_state()
    iteration_memory.append(
        state["facts"]["metadata"],
        turn_sequence=1,
        source="tool",
        payload={
            "sections": [
                {"heading": "Tool Executed", "body": f"Tool: {FPING_TOOL_ID}"}
            ]
        },
    )

    first = record_subagent_observation_and_budget(_pathfinder_definition(), state)
    second = record_subagent_observation_and_budget(_pathfinder_definition(), first)
    iteration_memory.append(
        second["facts"]["metadata"],
        turn_sequence=1,
        source="tool",
        payload={
            "sections": [
                {"heading": "Tool Executed", "body": f"Tool: {NMAP_TOOL_ID}"}
            ]
        },
    )
    third = record_subagent_observation_and_budget(_pathfinder_definition(), second)

    assert first["facts"]["iterations"] == 1
    assert second["facts"]["iterations"] == 1
    assert third["facts"]["iterations"] == 2
    assert "subagent_completed_iteration_markers" not in third["facts"]["metadata"]
    assert "subagent_observation_transcript" not in third["facts"]["metadata"]


@pytest.mark.asyncio
async def test_runtime_counts_real_multi_call_batch_application_as_one_iteration() -> None:
    state = _generic_state()
    first_llm = _FakeBuilderLLM(
        [
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.10"},
                strategy="parallel",
                intent="Check whether the approved host responds.",
                provider_call_id="provider-call-1",
            ),
            _native_call(
                FPING_TOOL_ID,
                parameters={"target": "10.0.0.11"},
                strategy="parallel",
                intent="Check whether a second approved host responds.",
                provider_call_id="provider-call-2",
            ),
        ]
    )
    planned = await run_subagent_model_turn(
        _pathfinder_definition(),
        state,
        llm_resolver=lambda *_args, **_kwargs: first_llm,
    )
    metadata = planned["facts"]["metadata"]
    metadata["turn_sequence"] = 1
    batch = deserialize_tool_batch_from_plan_data(metadata["planner_plan"])
    assert batch is not None
    assert batch.tool_batch_id == metadata[SUBAGENT_ACTION_METADATA_KEY][
        "tool_batch_id"
    ]
    assert len(batch.tool_calls) == 2

    compact_by_call_id: dict[str, dict[str, Any]] = {}
    projection_by_call_id: dict[str, dict[str, Any]] = {}
    result_rows: list[BatchToolCallResult] = []
    for index, call in enumerate(batch.tool_calls, start=1):
        compact = {
            "schema_version": "2.0",
            "tool": call.tool_id,
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": f"fping fixture {index} completed.",
            "key_findings": [f"{call.parameters['target']} responded."],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [f"batch_call={index}"],
            "lossiness_risk": "low",
        }
        compact_by_call_id[call.tool_call_id] = compact
        projection_by_call_id[call.tool_call_id] = {
            "compact_result_dict": compact,
            "result_for_metadata": {
                "tool": call.tool_id,
                "parameters": dict(call.parameters),
                "status": "success",
                "success": True,
                "exit_code": 0,
            },
        }
        result_rows.append(
            BatchToolCallResult(
                tool_call_id=call.tool_call_id,
                tool_id=call.tool_id,
                status=ToolCallStatus.SUCCESS,
                raw_result={"success": True, "status": "success"},
            )
        )

    class _NoopLogger:
        def debug(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def info(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    interactive = InteractiveState.from_mapping(planned)
    applied = finish_with_batch_result(
        interactive=interactive,
        facts=interactive.facts,
        batch=batch,
        result=BatchResult(
            tool_batch_id=batch.tool_batch_id,
            status=BatchStatus.COMPLETED,
            call_results=tuple(result_rows),
            effective_execution_strategy=batch.requested_execution_strategy,
            requested_execution_strategy=batch.requested_execution_strategy,
        ),
        original_plan=metadata["planner_plan"],
        compact_by_call_id=compact_by_call_id,
        deterministic_compact_by_call_id={},
        projection_by_call_id=projection_by_call_id,
        outcome_by_call_id={},
        execution_id_by_call_id={call.tool_call_id: None for call in batch.tool_calls},
        tool_catalog_by_call_id={},
        cached_dispatch_by_call_id={},
        dispatch_cache_entry_by_call_id={},
        trace_delta_by_call_id={},
        metadata_patch_by_call_id={},
        observation_by_call_id={},
        dr_execution_by_call_id={},
        budget_consumed_call_ids=set(),
        deps={
            "MemoryManager": MemoryManager,
            "ToolExecutionRecord": ToolExecutionRecord,
            "_TOOL_DISPATCH_CACHE_KEY": "tool_dispatch_cache",
            "_apply_cached_dispatch_result": _noop,
            "_clear_approval_gate_metadata": _noop,
            "_clear_tool_plan_prepared_flag": _noop,
            "_compact_observation_text": (
                lambda compact, fallback=None: str(
                    compact.get("summary") or fallback or ""
                )
            ),
            "apply_result_state_projection_service": _noop,
            "decrement_tool_call_budget": lambda _state: {},
            "logger": _NoopLogger(),
            "record_dr_tool_execution": _noop,
            "refresh_trace_scratchpad": _noop,
            "safe_inc": _noop,
        },
        turn_sequence=1,
        approval_response=None,
        batch_emit_lifecycle=False,
        has_writer=False,
        emitter=None,
    )
    tool_phase_records = [
        record
        for record in iteration_memory.get_ledger(applied["facts"]["metadata"])
        if record.get("source") == "tool"
    ]
    synced = record_subagent_observation_and_budget(
        _pathfinder_definition(),
        applied,
    )
    next_llm = _FakeBuilderLLM([], content="Observed both host checks.")

    await run_subagent_model_turn(
        _pathfinder_definition(),
        synced,
        llm_resolver=lambda *_args, **_kwargs: next_llm,
    )

    request = _request_projection(next_llm.requests[0])
    remaining_limits = _extract_prompt_section_json(
        request["user_prompt"],
        "Remaining Limits",
    )
    assert len(tool_phase_records) == 1
    assert synced["facts"]["iterations"] == 1
    assert remaining_limits["completed_iterations"] == 1
    assert remaining_limits["remaining_iterations"] == 2
    assert "subagent_completed_iteration_markers" not in synced["facts"]["metadata"]
    assert "subagent_observation_transcript" not in synced["facts"]["metadata"]


def test_subagent_tool_projection_uses_canonical_phase_memory_without_transcript() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(),
        ]
    )
    metadata = state["facts"]["metadata"]
    working_memory = metadata["working_memory"]
    compact = metadata["last_tool_result_compact"]
    ledger = working_memory["current_turn_phases"]

    assert working_memory["tool_runs"][0]["summary"] == compact["summary"]
    assert working_memory["tool_runs"][0]["key_findings"] == compact["key_findings"]
    assert working_memory["available_findings"][0]["kind"] == "host_up"
    assert working_memory["available_findings"][0]["target"] == "10.0.0.10"
    assert working_memory["available_findings"][0]["details"]["summary"] == (
        "10.0.0.10 is alive."
    )
    assert ledger == [
        {
            "turn_sequence": 1,
            "phase_sequence": 0,
            "source": "tool",
            "sections": [
                {
                    "heading": "Tool Executed",
                    "body": (
                        "Tool: information_gathering.network_discovery.fping\n"
                        "Parameters: target=10.0.0.10"
                    ),
                },
                {
                    "heading": "Tool Output Summary",
                    "body": "fping found 10.0.0.10 alive.",
                },
                {"heading": "Key Findings", "body": "- 10.0.0.10 is alive."},
                {"heading": "Compression Lossiness", "body": "lossiness_risk: low"},
            ],
        }
    ]
    phase_render = iteration_memory.render_phase_memory_section(
        metadata,
        turn_sequence=1,
    )
    assert "## Prior Current-Turn Phase Memory" in phase_render
    assert "source=tool" in phase_render
    assert "Tool Output Summary" in phase_render
    assert "fping found 10.0.0.10 alive." in phase_render
    assert "subagent_observation_transcript" not in metadata
    assert "next_actions" not in compact
    assert "Run nmap service enumeration next." not in json.dumps(ledger)


@pytest.mark.asyncio
async def test_subagent_prompt_state_and_model_call_efficiency_baseline() -> None:
    one_tool_state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(),
        ]
    )
    multi_iteration_state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(),
            _service_enumeration_iteration(),
        ]
    )

    one_tool_llm = _FakeBuilderLLM([], content="One-tool fixture handoff.")
    multi_iteration_llm = _FakeBuilderLLM(
        [],
        content="Multi-iteration fixture handoff.",
    )
    await run_subagent_model_turn(
        _pathfinder_definition(),
        one_tool_state,
        llm_resolver=lambda *_args, **_kwargs: one_tool_llm,
    )
    await run_subagent_model_turn(
        _pathfinder_definition(),
        multi_iteration_state,
        llm_resolver=lambda *_args, **_kwargs: multi_iteration_llm,
    )

    one_tool_request = one_tool_llm.requests[0]
    multi_iteration_request = multi_iteration_llm.requests[0]
    one_tool_user_prompt = one_tool_request["user_prompt"]
    multi_iteration_user_prompt = multi_iteration_request["user_prompt"]
    combined_multi_prompt = (
        multi_iteration_request["system_prompt"] + "\n" + multi_iteration_user_prompt
    )
    token_estimate = estimate_text_tokens(
        combined_multi_prompt,
        provider="baseline",
        model="subagent-characterization",
    )
    previous_tool_summary = _extract_prompt_json(
        multi_iteration_user_prompt,
        "Previous tool executed",
    )
    working_memory_summary = _extract_prompt_json(
        multi_iteration_user_prompt,
        "Working memory snapshot",
    )
    phase_memory = iteration_memory.render_phase_memory_section(
        multi_iteration_state["facts"]["metadata"],
        turn_sequence=1,
    )
    completed_for_parent_handoff = complete_subagent_result(
        _pathfinder_definition(),
        copy.deepcopy(multi_iteration_state),
    )
    parent_handoff_projection = completed_for_parent_handoff["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]
    parent_handoff_section = render_completed_agent_results_section(
        {"completed_agent_results": [parent_handoff_projection]}
    )

    assert previous_tool_summary == {
        "last_tool_result": multi_iteration_state["facts"]["last_tool_result_compact"],
        "current_turn_phase_memory": phase_memory,
    }
    assert "current_turn_phases" not in working_memory_summary
    assert "current_turn_phase_counter" not in working_memory_summary
    assert "current_turn_phase_turn" not in working_memory_summary
    assert len(one_tool_llm.requests) == 1
    assert len(multi_iteration_llm.requests) == 1
    assert "RAW_STDOUT" not in multi_iteration_user_prompt
    assert "parent-thread transcript" not in multi_iteration_user_prompt
    assert "subagent_observation_transcript" not in multi_iteration_user_prompt
    assert "subagent_observations" not in multi_iteration_user_prompt
    assert "current_turn_phase_memory" in multi_iteration_user_prompt

    baseline = {
        "one_tool_model_calls": len(one_tool_llm.requests),
        "multi_iteration_model_calls": len(multi_iteration_llm.requests),
        "one_tool_serialized_state_chars": _stable_json_size(one_tool_state),
        "multi_iteration_serialized_state_chars": _stable_json_size(
            multi_iteration_state
        ),
        "multi_iteration_prompt_chars": len(combined_multi_prompt),
        "multi_iteration_prompt_tokens": token_estimate.tokens,
        "bounded_parent_handoff_projection_chars": _stable_json_size(
            parent_handoff_projection
        ),
        "bounded_parent_handoff_render_chars": len(parent_handoff_section),
        "parent_handoff_projection_model_calls": 0,
        "token_estimator": {
            "provider": token_estimate.provider,
            "model": token_estimate.model,
            "strategy": token_estimate.strategy,
            "precision": token_estimate.precision,
        },
        "phase_record_count": len(
            multi_iteration_state["facts"]["metadata"]["working_memory"][
                "current_turn_phases"
            ]
        ),
        "subagent_observation_transcript_present": (
            "subagent_observation_transcript"
            in multi_iteration_state["facts"]["metadata"]
        ),
    }
    # Serialized state includes canonical turn/phase metadata for bounded
    # parent handoff identity; prompt size and model-call counts remain fixed.
    assert baseline == {
        "one_tool_model_calls": 1,
        "multi_iteration_model_calls": 1,
        "one_tool_serialized_state_chars": 9686,
        "multi_iteration_serialized_state_chars": 10908,
        "multi_iteration_prompt_chars": 11543,
        "multi_iteration_prompt_tokens": 4123,
        "bounded_parent_handoff_projection_chars": 501,
        "bounded_parent_handoff_render_chars": 281,
        "parent_handoff_projection_model_calls": 0,
        "token_estimator": {
            "provider": "baseline",
            "model": "subagent-characterization",
            "strategy": "provider_agnostic_char_heuristic",
            "precision": "heuristic",
        },
        "phase_record_count": 2,
        "subagent_observation_transcript_present": False,
    }


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


def test_runtime_completion_derives_terminal_fields_from_canonical_sources() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(),
            _service_enumeration_iteration(
                report_recommendations=["Capture the HTTP service banner."]
            ),
        ]
    )
    state["trace"]["executed_tools"] = [
        ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success").model_dump(
            mode="json"
        ),
        ToolExecutionRecord(tool_id=NMAP_TOOL_ID, status="success").model_dump(
            mode="json"
        ),
    ]
    state["facts"]["metadata"]["working_memory"]["available_findings"][0]["details"][
        "raw_output"
    ] = "RAW_STDOUT should stay private"

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    projection = completed["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]
    parsed = AgentResultProjection.model_validate(projection)

    assert result["summary"] == "nmap found tcp/80 open on 10.0.0.10."
    assert result["key_findings"] == [
        "10.0.0.10 is alive.",
        "10.0.0.10 exposes tcp/80.",
    ]
    assert result["evidence_refs"] == [
        {"path": "/workspace/artifacts/exec-nmap-2.json", "label": "exec-nmap-2"}
    ]
    assert result["tools_used"] == [FPING_TOOL_ID, NMAP_TOOL_ID]
    assert result["recommended_next_steps"] == [
        "Inspect HTTP response headers next.",
        "Capture the HTTP service banner.",
    ]
    assert parsed.key_findings == tuple(result["key_findings"])
    _assert_projection_excludes_private_state(projection, "RAW_STDOUT")


def test_runtime_completion_keeps_metadata_only_compact_evidence_refs() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(execution_id="exec-fping-cached"),
        ]
    )
    metadata = state["facts"]["metadata"]
    metadata.pop("last_tool_result_compact_batch", None)
    metadata["last_tool_result_compact"]["raw_output"] = "RAW_STDOUT should stay private"
    metadata["checkpoint_state"] = {"messages": ["CHILD_CHECKPOINT_PRIVATE"]}
    state["facts"].pop("last_tool_result_compact", None)

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    projection = completed["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]

    assert result["evidence_refs"] == [
        {
            "path": "/workspace/artifacts/exec-fping-cached.json",
            "label": "exec-fping-cached",
        }
    ]
    _assert_projection_excludes_private_state(
        projection,
        "RAW_STDOUT",
        "checkpoint_state",
        "CHILD_CHECKPOINT_PRIVATE",
    )


def test_runtime_completion_retains_failed_partial_budget_limitations() -> None:
    state = _state_after_tool_iterations(
        [
            _failed_service_iteration(),
        ]
    )
    metadata = state["facts"]["metadata"]
    metadata[SUBAGENT_FORCED_FINAL_METADATA_KEY] = True
    metadata["model_limitations"] = ["The model could not verify UDP exposure."]
    metadata["tool_gaps"] = ["Credentials were not available for authenticated checks."]
    metadata["synthesized_output"]["limitations"] = [
        "HTTP service metadata is unverified."
    ]

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    projection = completed["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]

    assert result["outcome"] == "partial"
    assert result["limitations"] == [
        "The model could not verify UDP exposure.",
        "Credentials were not available for authenticated checks.",
        "HTTP service metadata is unverified.",
        "nmap timed out after the configured deadline.",
        "Latest compact tool result status was failed.",
        (
            f"Tool call {NMAP_TOOL_ID} (tc-exec-nmap-failed) reported failed: "
            "nmap timed out after the configured deadline."
        ),
        "Subagent tool or iteration budget was exhausted.",
    ]
    assert result["recommended_next_steps"] == ["Retry with a narrower port set."]
    _assert_projection_excludes_private_state(projection)


def test_runtime_completion_marks_partial_compact_result_as_partial() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(execution_id="exec-fping-partial"),
        ]
    )
    compact = state["facts"]["metadata"]["last_tool_result_compact"]
    compact["status"] = "partial"
    compact["success"] = True

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]

    assert result["outcome"] == "partial"
    assert result["limitations"] == [
        "Latest compact tool result status was partial."
    ]


def test_runtime_completion_marks_mixed_batch_as_partial() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(execution_id="exec-fping-success"),
        ]
    )
    metadata = state["facts"]["metadata"]
    primary_compact = metadata["last_tool_result_compact"]
    failed_compact = {
        "schema_version": "2.0",
        "tool": NMAP_TOOL_ID,
        "status": "failed",
        "success": False,
        "exit_code": 1,
        "summary": "nmap timed out before completing service enumeration.",
        "key_findings": ["Service enumeration was incomplete."],
        "errors": ["nmap timed out after the configured deadline."],
        "raw_output": "RAW_STDOUT should stay private",
        "artifact_refs": [
            {
                "path": "/workspace/artifacts/exec-nmap-failed.json",
                "label": "exec-nmap-failed",
            }
        ],
    }
    metadata["last_tool_result_compact_batch"] = {
        "tool_batch_id": "batch-mixed",
        "execution_strategy": "sequential",
        "requested_execution_strategy": "sequential",
        "status": "completed_with_errors",
        "success": False,
        "results": [
            {
                "tool_call_id": "tc-fping-success",
                "tool_id": FPING_TOOL_ID,
                "intent": "Check host liveness",
                "status": "success",
                "success": True,
                "compact_tool_result": primary_compact,
            },
            {
                "tool_call_id": "tc-nmap-failed",
                "tool_id": NMAP_TOOL_ID,
                "intent": "Enumerate services",
                "status": "failed",
                "success": False,
                "failure_category": "tool_error",
                "error_message": "nmap timed out after the configured deadline.",
                "compact_tool_result": failed_compact,
            },
        ],
        "deferred_followups": [],
    }
    metadata["checkpoint_state"] = {"messages": ["CHILD_CHECKPOINT_PRIVATE"]}

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    projection = completed["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]

    assert result["outcome"] == "partial"
    assert result["limitations"] == [
        "Latest compact tool batch status was completed_with_errors.",
        (
            f"Tool call {NMAP_TOOL_ID} (tc-nmap-failed) reported failed: "
            "nmap timed out after the configured deadline."
        ),
    ]
    _assert_projection_excludes_private_state(
        projection,
        "RAW_STDOUT",
        "checkpoint_state",
        "CHILD_CHECKPOINT_PRIVATE",
    )


def test_runtime_completion_marks_nested_partial_batch_row_as_partial() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(execution_id="exec-fping-success"),
        ]
    )
    metadata = state["facts"]["metadata"]
    partial_compact = {
        "schema_version": "2.0",
        "tool": NMAP_TOOL_ID,
        "status": "partial",
        "success": True,
        "summary": "nmap returned incomplete service evidence.",
        "key_findings": ["Service enumeration was incomplete."],
        "errors": [],
    }
    metadata["last_tool_result_compact_batch"] = {
        "tool_batch_id": "batch-partial-row",
        "execution_strategy": "parallel",
        "status": "completed",
        "success": True,
        "results": [
            {
                "tool_call_id": "tc-fping-success",
                "tool_id": FPING_TOOL_ID,
                "status": "success",
                "success": True,
                "compact_tool_result": metadata["last_tool_result_compact"],
            },
            {
                "tool_call_id": "tc-nmap-partial",
                "tool_id": NMAP_TOOL_ID,
                "status": "success",
                "success": True,
                "compact_tool_result": partial_compact,
            },
        ],
    }

    completed = complete_subagent_result(_pathfinder_definition(), state)
    result = completed["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]

    assert result["outcome"] == "partial"
    assert result["limitations"] == [
        f"Tool call {NMAP_TOOL_ID} (tc-nmap-partial) reported partial."
    ]


def test_subagent_parent_handoff_baseline_is_bounded_result_projection() -> None:
    state = _state_after_tool_iterations(
        [
            _host_discovery_iteration(),
            _service_enumeration_iteration(),
        ]
    )

    completed = complete_subagent_result(_pathfinder_definition(), state)
    projection = completed["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]
    parsed = AgentResultProjection.model_validate(projection)

    assert parsed.summary == "nmap found tcp/80 open on 10.0.0.10."
    assert parsed.key_findings == (
        "10.0.0.10 is alive.",
        "10.0.0.10 exposes tcp/80.",
    )
    assert parsed.recommended_next_steps == (
        "Inspect HTTP response headers next.",
    )
    assert "working_memory" not in projection
    assert "current_turn_phases" not in projection
    assert "subagent_observation_transcript" not in projection
    rendered = render_completed_agent_results_section(
        {"completed_agent_results": [projection]}
    )
    assert "Completed Agent Results:" in rendered
    assert "10.0.0.10 exposes tcp/80." in rendered
    assert "current_turn_phases" not in rendered
    assert "subagent_observation_transcript" not in rendered


def test_concurrent_child_results_are_isolated_and_parent_projection_bounded() -> None:
    first = _generic_state_for_assignment(
        _assignment(assignment_id="assign-alpha", agent_run_id="run-alpha"),
        graph_thread_id="child-thread-alpha",
    )
    second = _generic_state_for_assignment(
        _assignment(assignment_id="assign-beta", agent_run_id="run-beta"),
        graph_thread_id="child-thread-beta",
    )
    first = _apply_tool_iteration_projection(
        first,
        _tool_iteration(
            tool_id=FPING_TOOL_ID,
            execution_id="exec-alpha",
            summary="fping found the alpha host alive.",
            finding="Alpha host is alive.",
            next_action="Enumerate alpha services next.",
        ),
        index=1,
    )
    second = _apply_tool_iteration_projection(
        second,
        _tool_iteration(
            tool_id=NMAP_TOOL_ID,
            execution_id="exec-beta",
            summary="nmap found beta tcp/443 open.",
            finding="Beta host exposes tcp/443.",
            next_action="Inspect beta TLS metadata next.",
        ),
        index=1,
    )
    first["facts"]["metadata"]["final_checkpoint_id"] = "checkpoint-alpha"
    second["facts"]["metadata"]["final_checkpoint_id"] = "checkpoint-beta"
    first["facts"]["metadata"]["working_memory"]["current_turn_phases"].append(
        {
            "turn_sequence": 1,
            "phase_sequence": 99,
            "source": "tool",
            "sections": [{"heading": "Private", "body": "ALPHA_PRIVATE_PHASE"}],
        }
    )
    first["facts"]["metadata"]["checkpoint_state"] = {
        "messages": ["ALPHA_PRIVATE_CHECKPOINT_STATE"]
    }
    first["facts"]["metadata"]["working_memory"]["available_findings"][0]["details"][
        "raw_output"
    ] = "ALPHA_RAW_STDOUT"
    second["facts"]["metadata"]["working_memory"]["available_findings"][0]["details"][
        "raw_output"
    ] = "BETA_RAW_STDOUT"

    completed_first = complete_subagent_result(_pathfinder_definition(), first)
    completed_second = complete_subagent_result(_pathfinder_definition(), second)
    first_projection = completed_first["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]
    second_projection = completed_second["facts"]["metadata"][
        SUBAGENT_RESULT_PROJECTION_METADATA_KEY
    ]

    assert first_projection["agent_run_id"] == "run-alpha"
    assert second_projection["agent_run_id"] == "run-beta"
    assert first_projection["key_findings"] == ["Alpha host is alive."]
    assert second_projection["key_findings"] == ["Beta host exposes tcp/443."]
    assert first_projection["final_checkpoint_id"] == "checkpoint-alpha"
    assert second_projection["final_checkpoint_id"] == "checkpoint-beta"
    assert first_projection["evidence_refs"] == [
        {"path": "/workspace/artifacts/exec-alpha.json", "label": "exec-alpha"}
    ]
    assert second_projection["evidence_refs"] == [
        {"path": "/workspace/artifacts/exec-beta.json", "label": "exec-beta"}
    ]
    assert "Alpha host is alive." not in json.dumps(
        second["facts"]["metadata"]["working_memory"]
    )
    assert "Beta host exposes tcp/443." not in json.dumps(
        first["facts"]["metadata"]["working_memory"]
    )

    rendered = render_completed_agent_results_section(
        {"completed_agent_results": [first_projection, second_projection]}
    )
    parent_projection_json = json.dumps(
        {"completed_agent_results": [first_projection, second_projection]}
    )
    for forbidden in (
        "working_memory",
        "current_turn_phases",
        "subagent_observation_transcript",
        "checkpoint_state",
        "ALPHA_PRIVATE_PHASE",
        "ALPHA_PRIVATE_CHECKPOINT_STATE",
        "ALPHA_RAW_STDOUT",
        "BETA_RAW_STDOUT",
    ):
        assert forbidden not in parent_projection_json
        assert forbidden not in rendered


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
        "schemas_by_tool_id": {
            tool.tool_id: tool.parameters_schema
            for tool in tools
        },
    }
