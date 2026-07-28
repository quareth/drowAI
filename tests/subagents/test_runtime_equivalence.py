"""Equivalence tests for the generic subagent runtime extraction."""

from __future__ import annotations

import ast
import asyncio
import json
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
    SUBAGENT_EXECUTION_STRATEGY_KEY,
    SubagentToolBuilderPromptBuilder,
    choose_subagent_action,
)
from agent.subagents.runtime import model as runtime_model
from agent.subagents.runtime.profile import resolve_subagent_tool_profile
from agent.subagents.runtime.state import (
    build_subagent_initial_state,
    subagent_state_from_graph_state,
)
from agent.subagents.scout.nodes.complete import complete_scout_result
from agent.subagents.scout.nodes import choose_action as scout_choose_action
from agent.subagents.scout.nodes.choose_action import (
    SCOUT_EXECUTION_STRATEGY_KEY,
    choose_scout_action,
)
from agent.subagents.scout.profile import (
    ScoutToolProfile,
    ScoutToolSpec,
    resolve_scout_tool_profile,
)
from agent.subagents.scout.state import (
    build_scout_initial_state,
    scout_state_from_graph_state,
)
from agent.tools.tool_call_specs import make_function_name_for_tool
from core.prompts.builders.scout_tool_builder import ScoutToolBuilderPromptBuilder


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

    def __init__(self, calls: list[ProviderToolCall]) -> None:
        self.calls = calls
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
            usage=_FakeUsage(),
        )


def _pathfinder_definition() -> SubagentDefinition:
    definitions = load_subagent_definitions()
    [pathfinder] = [
        definition for definition in definitions if definition.id == "pathfinder"
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


def _profile() -> ScoutToolProfile:
    return ScoutToolProfile(
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


def _legacy_state() -> dict[str, Any]:
    return build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
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


def test_runtime_profile_matches_current_scout_profile() -> None:
    definition = _pathfinder_definition()

    generic_profile = resolve_subagent_tool_profile(definition, definition.tool_ids)
    scout_profile = resolve_scout_tool_profile(definition.tool_ids)

    assert generic_profile.tool_ids == scout_profile.tool_ids
    assert generic_profile.capabilities_for_tool(FPING_TOOL_ID) == (
        scout_profile.capabilities_for_tool(FPING_TOOL_ID)
    )
    assert generic_profile.capabilities_for_tool(NMAP_TOOL_ID) == (
        scout_profile.capabilities_for_tool(NMAP_TOOL_ID)
    )


def test_runtime_initial_state_matches_current_scout_state() -> None:
    generic_state = _generic_state()
    scout_state = _legacy_state()

    assert generic_state == scout_state
    assert (
        subagent_state_from_graph_state(
            generic_state,
            definition=_pathfinder_definition(),
        ).model_dump(mode="json")
        == scout_state_from_graph_state(scout_state).model_dump(mode="json")
    )


def test_runtime_model_prompts_match_current_scout_prompts() -> None:
    definition = _pathfinder_definition()
    generic_builder = SubagentToolBuilderPromptBuilder(definition)
    scout_builder = ScoutToolBuilderPromptBuilder()
    assignment = _assignment().model_dump(mode="json")

    assert generic_builder.build_system_prompt(
        max_committed_tools_per_batch=AgentConfig().max_committed_tools_per_batch
    ) == scout_builder.build_system_prompt(
        max_committed_tools_per_batch=AgentConfig().max_committed_tools_per_batch
    )
    assert generic_builder.build_user_prompt(
        assignment=assignment,
        tool_ids=(FPING_TOOL_ID, NMAP_TOOL_ID),
        working_memory={"prior": "none"},
        previous_tool_summary={"summary": "no prior tools"},
    ) == scout_builder.build_user_prompt(
        assignment=assignment,
        tool_ids=(FPING_TOOL_ID, NMAP_TOOL_ID),
        working_memory={"prior": "none"},
        previous_tool_summary={"summary": "no prior tools"},
    )


@pytest.mark.asyncio
async def test_runtime_model_builder_matches_current_scout_action_node(
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
    generic_llm = _FakeBuilderLLM(calls)
    scout_llm = _FakeBuilderLLM(calls)

    generic_update = await choose_subagent_action(
        _pathfinder_definition(),
        _generic_state(),
        llm_resolver=lambda *_args, **_kwargs: generic_llm,
    )

    monkeypatch.setattr(
        "agent.subagents.scout.nodes.choose_action.resolve_llm_client",
        lambda *_args, **_kwargs: scout_llm,
    )
    scout_update = await choose_scout_action(_legacy_state())

    assert _normalize_dynamic_batch_ids(generic_update) == _normalize_dynamic_batch_ids(
        scout_update
    )
    assert SCOUT_EXECUTION_STRATEGY_KEY == SUBAGENT_EXECUTION_STRATEGY_KEY
    assert _request_projection(generic_llm.requests[0]) == _request_projection(
        scout_llm.requests[0]
    )


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
    generic_llm = _FakeBuilderLLM(calls)
    scout_llm = _FakeBuilderLLM(calls)

    def _poison_resolver(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("global runtime resolver leaked into injected run")

    monkeypatch.setattr(runtime_model, "resolve_llm_client", _poison_resolver)
    monkeypatch.setattr(
        scout_choose_action,
        "resolve_llm_client",
        lambda *_args, **_kwargs: scout_llm,
    )

    generic_update, scout_update = await asyncio.gather(
        choose_subagent_action(
            _pathfinder_definition(),
            _generic_state(),
            llm_resolver=lambda *_args, **_kwargs: generic_llm,
        ),
        choose_scout_action(_legacy_state()),
    )

    assert runtime_model.resolve_llm_client is _poison_resolver
    assert len(generic_llm.requests) == 1
    assert len(scout_llm.requests) == 1
    assert _normalize_dynamic_batch_ids(generic_update) == _normalize_dynamic_batch_ids(
        scout_update
    )


def test_runtime_completion_matches_current_scout_completion() -> None:
    generic_interactive = InteractiveState.from_mapping(_generic_state())
    scout_interactive = InteractiveState.from_mapping(_legacy_state())
    for interactive in (generic_interactive, scout_interactive):
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
        interactive.facts.metadata["router_outcome"] = {"action": "finalize"}
        interactive.trace.executed_tools.append(
            ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success")
        )

    assert complete_subagent_result(
        _pathfinder_definition(),
        generic_interactive.as_graph_state(),
    ) == complete_scout_result(scout_interactive.as_graph_state())


def test_current_scout_baseline_does_not_delegate_to_generic_runtime() -> None:
    scout_paths = [
        Path("agent/subagents/scout/state.py"),
        Path("agent/subagents/scout/profile.py"),
        Path("agent/subagents/scout/nodes/choose_action.py"),
        Path("agent/subagents/scout/nodes/complete.py"),
    ]
    for path in scout_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("agent.subagents.runtime")
                    for alias in node.names
                )
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith(
                    "agent.subagents.runtime"
                )


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
        "required": [
            tool.parameters_schema["required"]
            for tool in tools
        ],
    }


def _normalize_dynamic_batch_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_dynamic_batch_ids(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_dynamic_batch_ids(item) for item in value]
    if isinstance(value, str) and value.startswith("scout-call-"):
        return "<scout-call-id>"
    if isinstance(value, str) and value.startswith("scout-batch-"):
        return "<scout-batch-id>"
    return value
