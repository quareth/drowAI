"""End-to-end pilot checks for process-local subagent orchestration.

These tests prove the generic subagent path through deterministic doubles at
the active service seams. They keep external runtime execution out of scope
while exercising facade routing, async launch, process-local lifecycle,
attribution events, cancellation/restart limitations, and same-process result
handoff.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

import agent.graph.builders.parent_handoff_builder as parent_handoff_builder
from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes.post_tool_reasoning.models import (
    DIRECT_TOOL_OUTCOME_SOURCE,
    POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
)
from agent.graph.state import InteractiveState
from agent.subagents.registry import (
    SubagentRegistry,
    get_subagent_registry,
)
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
)
from backend.services.agent_runs.launcher import AgentRunLauncher
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.execution.graph_executor import (
    GraphExecutionResult,
)


class _PilotContextBuilder:
    def __init__(self) -> None:
        self.turn_sequence = 0

    def build_runtime_config(
        self,
        *,
        chat_inputs: ChatInputs,
        metadata: dict[str, Any] | None = None,
    ) -> LangGraphRuntimeConfig:
        self.turn_sequence += 1
        turn_id = f"task-{chat_inputs.task_id}-turn-{self.turn_sequence}"
        task_workspace = f"task-{chat_inputs.task_id}"
        merged = {
            "tenant_id": 7,
            "graph_thread_id": f"{chat_inputs.task_id:032x}",
            "runtime_placement_mode": "runner",
            "workspace_id": task_workspace,
            "workspace_path": f"/workspace/{task_workspace}",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "runner_id": "runner-1",
            "execution_site_id": "site-1",
            "turn_id": turn_id,
            "turn_number": self.turn_sequence,
            "turn_sequence": self.turn_sequence,
            "feature_flags": {
                "simple_tool_enabled": True,
            },
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id=chat_inputs.conversation_id or "",
                turn_id=turn_id,
                turn_sequence=self.turn_sequence,
                messages=list(chat_inputs.history),
                current_message=chat_inputs.message,
            ),
        }
        merged.update(metadata or {})
        return LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            execution_mode=ExecutionMode.NORMAL_CHAT,
            metadata=merged,
        )


class _PilotIntentClassifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def enrich_runtime_config(
        self,
        runtime_config: LangGraphRuntimeConfig,
        **_: Any,
    ):
        message = runtime_config.chat_inputs.message
        self.messages.append(message)
        if "service discovery" in message.lower():
            targets = ["10.0.0.10"]
            if "10.0.0.11" in message:
                targets = ["10.0.0.11"]
            if "two" in message.lower():
                targets = ["10.0.0.10", "10.0.0.11"]
            handoffs = [
                {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": (
                        f"Run service discovery against {target}. Complete when "
                        "open services and evidence limits are summarized."
                    ),
                }
                for target in targets
            ]
            runtime_config.execution_mode = ExecutionMode.SIMPLE_TOOL
            runtime_config.metadata.update(
                {
                    "intent_classifier_label": "direct_executor",
                    "intent_classifier_raw_label": "direct_executor",
                    "intent_classifier_raw_response": {
                        "suggested_capabilities": ["service discovery"],
                        "agent_handoffs": handoffs,
                    },
                    "intent_hints": {
                        "classifier_label": "direct_executor",
                        "targets": targets,
                        "suggested_capabilities": ["service discovery"],
                        "agent_handoffs": handoffs,
                    },
                }
            )
        else:
            runtime_config.execution_mode = ExecutionMode.NORMAL_CHAT
            runtime_config.metadata.update(
                {
                    "intent_classifier_label": "simple_chat",
                    "intent_classifier_raw_label": "simple_chat",
                    "intent_hints": {"classifier_label": "simple_chat"},
                }
            )
        return SimpleNamespace(usage=None)


class _NoopPriorTurnReferenceMaterializer:
    def materialize_for_runtime_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None


async def _ignore_lifecycle_event(_task_id: int, _event: dict[str, Any]) -> None:
    return None


class _ScriptedSubagentWorker:
    def __init__(
        self,
        *,
        outcomes: tuple[str, ...] = ("completed",),
        auto_release: bool = False,
    ) -> None:
        self.started = asyncio.Event()
        self.outcomes = outcomes
        self.auto_release = auto_release
        self.calls: list[dict[str, Any]] = []
        self._releases: list[asyncio.Event] = []

    async def __call__(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: dict[str, Any],
        graph_thread_id: str,
        is_cancel_requested: Any,
    ) -> AgentRunCompletion:
        release = asyncio.Event()
        self._releases.append(release)
        call_index = len(self.calls)
        self.calls.append(
            {
                "assignment": assignment,
                "runtime_config": runtime_config,
                "graph_thread_id": graph_thread_id,
                "release": release,
            }
        )
        self.started.set()
        if not self.auto_release:
            await release.wait()
        assert not await is_cancel_requested()
        outcome = self.outcomes[min(call_index, len(self.outcomes) - 1)]
        result = AgentResult(
            agent_run_id=assignment.agent_run_id,
            agent_id="pathfinder",
            agent_kind="recon",
            outcome=outcome,  # type: ignore[arg-type]
            summary=f"Pathfinder {outcome} for {assignment.targets[0]}.",
            key_findings=[f"{assignment.targets[0]} exposes HTTPS on 443"],
            evidence_refs=[
                {
                    "kind": "artifact",
                    "path": f"/workspace/task-{assignment.task_id}/nmap.xml",
                    "summary": "Compact Nmap XML artifact",
                }
            ],
            tools_used=[
                "information_gathering.network_discovery.fping",
                "information_gathering.network_discovery.nmap",
            ],
            limitations=["Single approved target only."],
            recommended_next_steps=["Review the HTTPS service banner."],
            final_checkpoint_id="cp-pathfinder-final",
        )
        return build_agent_run_completion(
            result=result,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
        )


class _ScriptedParentParExecutor:
    def __init__(
        self,
        decide: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._decide = decide or _finalize_parent_decision

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: dict[str, Any],
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        metadata = graph_input["facts"]["metadata"]
        bundle = metadata[METADATA_CONTEXT_BUNDLE_KEY]
        completed_results = tuple(bundle.get("completed_agent_results") or ())
        active_runs = tuple(bundle.get("active_agent_runs") or ())
        call = {
            "compiled_graph": compiled_graph,
            "graph_input": graph_input,
            "config": config,
            "task_id": task_id,
            "kwargs": kwargs,
            "completed_results": completed_results,
            "active_runs": active_runs,
            "input_todo_list": tuple(graph_input["facts"].get("todo_list") or ()),
        }
        decision = self._decide(call)
        self.calls.append(
            {
                **call,
                "decision": decision,
            }
        )
        final_state = copy.deepcopy(graph_input)
        decision_metadata = dict(decision.get("metadata") or {})
        final_state["facts"]["todo_list"] = decision.get("todo_list", [])
        final_state["facts"]["metadata"]["router_outcome"] = decision[
            "router_outcome"
        ]
        final_state["facts"]["metadata"].update(decision_metadata)
        final_state["trace"]["final_text"] = decision["final_text"]
        return GraphExecutionResult(
            final_state=final_state,
            metadata={
                "router_outcome": decision["router_outcome"],
                "parent_graph_routes": decision["parent_graph_routes"],
                **decision_metadata,
            },
        )


def _finalize_parent_decision(_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_text": "The parent PAR cycle finalized the Pathfinder evidence.",
        "todo_list": ["Pathfinder evidence accepted after PAR"],
        "router_outcome": {
            "action": "finalize",
            "candidate_id": "par-finalize",
        },
        "parent_graph_routes": [
            "post_action_reasoning",
            "decision_router",
            "finalize",
        ],
    }


class _CompiledParentHandoffExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.node_calls: list[str] = []
        self.streamed_nodes: list[str] = []

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: dict[str, Any],
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        metadata = graph_input["facts"]["metadata"]
        bundle = metadata[METADATA_CONTEXT_BUNDLE_KEY]
        completed_results = tuple(bundle.get("completed_agent_results") or ())
        active_runs = tuple(bundle.get("active_agent_runs") or ())
        call = {
            "compiled_graph": compiled_graph,
            "graph_input": graph_input,
            "config": config,
            "task_id": task_id,
            "kwargs": kwargs,
            "completed_results": completed_results,
            "active_runs": active_runs,
            "input_todo_list": tuple(graph_input["facts"].get("todo_list") or ()),
        }
        self.calls.append(call)

        final_state: dict[str, Any] | None = None
        async for event in compiled_graph.astream(
            graph_input,
            config=config,
            stream_mode=["updates", "values"],
        ):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, chunk = event
            if mode == "updates" and isinstance(chunk, dict):
                self.streamed_nodes.extend(str(key) for key in chunk)
            if mode == "values" and isinstance(chunk, dict):
                final_state = chunk

        final_metadata = (
            final_state.get("facts", {}).get("metadata", {})
            if isinstance(final_state, dict)
            else {}
        )
        return GraphExecutionResult(
            final_state=final_state,
            metadata={
                **(dict(final_metadata) if isinstance(final_metadata, dict) else {}),
                "parent_graph_routes": list(self.streamed_nodes),
            },
        )


def _write_parent_candidate(
    interactive: InteractiveState,
    *,
    action: str,
    reasoning: str,
) -> None:
    metadata = interactive.facts.ensure_metadata()
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        turn_sequence = 1
        metadata["turn_sequence"] = turn_sequence
    phase_sequence = int(metadata.get("parent_par_invocation_count") or 0)
    metadata["phase_sequence"] = phase_sequence
    metadata["current_ptr_phase_sequence"] = phase_sequence
    decision_history = interactive.facts.ensure_decision_history()
    decision_history.append(f"{action}: {reasoning}")
    interactive.facts.set_candidate_decision(
        {
            "next_action": action,
            "action_reasoning": reasoning,
            "decision_source": "ptr",
            "candidate_id": (
                f"ptr-{turn_sequence}-{phase_sequence}-"
                f"{interactive.facts.iterations}-{len(decision_history)}"
            ),
            "producer_node": "post_tool_reasoning",
            "turn_sequence": turn_sequence,
            "phase_sequence": phase_sequence,
        }
    )


def _install_compiled_parent_handoff_deterministic_seams(
    monkeypatch: pytest.MonkeyPatch,
    executor: _CompiledParentHandoffExecutor,
) -> None:
    async def _post_tool_reasoning(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("post_action_reasoning")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        source = metadata.get(POST_ACTION_OUTCOME_SOURCE_METADATA_KEY)
        sources = metadata.setdefault("parent_par_sources", [])
        if isinstance(sources, list):
            sources.append(source)
        metadata["parent_par_invocation_count"] = (
            int(metadata.get("parent_par_invocation_count") or 0) + 1
        )

        if source == SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE:
            metadata["runtime_budgets"] = {
                "remaining_iterations": 3,
                "remaining_tool_calls": 1,
            }
            interactive.facts.next_tool_hint = "Confirm HTTPS details on 10.0.0.10."
            metadata["next_tool_hint"] = interactive.facts.next_tool_hint
            _write_parent_candidate(
                interactive,
                action="call_tool",
                reasoning="Child handoff needs one parent-owned direct check.",
            )
        else:
            metadata.pop("next_tool_hint", None)
            interactive.facts.next_tool_hint = None
            metadata["user_goal_achieved"] = True
            _write_parent_candidate(
                interactive,
                action="finalize",
                reasoning="Direct tool evidence completed the parent goal.",
            )
        metadata["post_tool_reasoning_completed"] = True
        metadata["last_post_tool_action"] = (
            "call_tool"
            if source == SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
            else "finalize"
        )
        return interactive.as_graph_update()

    async def _select_tool_categories(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("select_tool_categories")
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.ensure_metadata()["selected_categories"] = [
            "information_gathering"
        ]
        return interactive.as_graph_update()

    async def _prepare_tool_plan(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("prepare_tool_plan")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        metadata["prepared_tool_plan"] = {
            "tool": "information_gathering.network_discovery.nmap",
            "parameters": {"target": "10.0.0.10", "ports": "443"},
        }
        interactive.facts.selected_tool = "information_gathering.network_discovery.nmap"
        return interactive.as_graph_update()

    async def _articulate_tool_intent(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("articulation")
        return InteractiveState.from_mapping(state).as_graph_update()

    async def _approval_gate(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("approval_gate")
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.ensure_metadata()["tool_approval_gate_completed"] = True
        return interactive.as_graph_update()

    async def _dispatch_tool(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("dispatch_tool")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        metadata["direct_tool_execution_count"] = (
            int(metadata.get("direct_tool_execution_count") or 0) + 1
        )
        metadata["last_tool_result_compact"] = {
            "success": True,
            "status": "success",
            "summary": "Direct tool confirmed HTTPS service details.",
        }
        return interactive.as_graph_update()

    async def _synthesize_tool_output(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("tool_synthesizer")
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.ensure_metadata()["synthesized_output"] = {
            "success": True,
            "status": "success",
            "summary": "Direct tool confirmed HTTPS service details.",
            "key_findings": ["HTTPS service remained reachable on 443"],
        }
        return interactive.as_graph_update()

    async def _finalize_results(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("format_results")
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        metadata["parent_final_answer_count"] = (
            int(metadata.get("parent_final_answer_count") or 0) + 1
        )
        interactive.trace.final_text = (
            "The parent PAR cycle used a direct tool and finalized once."
        )
        return interactive.as_graph_update()

    def _finalize_turn(
        state: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        executor.node_calls.append("finalize")
        return InteractiveState.from_mapping(state).as_graph_update()

    monkeypatch.setattr(parent_handoff_builder, "post_tool_reasoning", _post_tool_reasoning)
    monkeypatch.setattr(
        parent_handoff_builder,
        "select_tool_categories_node",
        _select_tool_categories,
    )
    monkeypatch.setattr(
        parent_handoff_builder,
        "prepare_tool_execution_plan",
        _prepare_tool_plan,
    )
    monkeypatch.setattr(
        parent_handoff_builder,
        "articulate_tool_intent",
        _articulate_tool_intent,
    )
    monkeypatch.setattr(parent_handoff_builder, "approval_gate_node", _approval_gate)
    monkeypatch.setattr(
        parent_handoff_builder,
        "dispatch_tool_execution_node",
        _dispatch_tool,
    )
    monkeypatch.setattr(
        parent_handoff_builder,
        "synthesize_tool_output",
        _synthesize_tool_output,
    )
    monkeypatch.setattr(parent_handoff_builder, "finalize_results", _finalize_results)
    monkeypatch.setattr(parent_handoff_builder, "finalize_turn", _finalize_turn)


async def _wait_for_call_count(
    worker: _ScriptedSubagentWorker,
    count: int,
) -> None:
    async def _poll() -> None:
        while len(worker.calls) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=1)


async def _wait_for_parent_call_count(
    executor: _ScriptedParentParExecutor,
    count: int,
) -> None:
    async def _poll() -> None:
        while len(executor.calls) < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=1)


def _release_call(worker: _ScriptedSubagentWorker, index: int) -> None:
    worker.calls[index]["release"].set()


def _concurrent_pathfinder_registry() -> SubagentRegistry:
    pathfinder = get_subagent_registry().require("pathfinder")
    return SubagentRegistry(
        [replace(pathfinder, max_active_runs_per_task=2)]
    )


def _chat_inputs(
    message: str,
    *,
    task_id: int = 42,
    conversation_id: str | None = "conversation-42",
) -> ChatInputs:
    return ChatInputs(
        task_id=task_id,
        user_id=3,
        message=message,
        conversation_id=conversation_id,
        history=[{"role": "user", "content": message}],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        agent_mode=AgentMode.AGENT,
    )


@pytest.mark.asyncio
async def test_subagent_pilot_routes_terminal_handoff_through_parent_par(
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _ScriptedSubagentWorker()
    parent_executor = _ScriptedParentParExecutor()
    lifecycle_events: list[dict[str, Any]] = []

    async def _publish_lifecycle(task_id: int, event: dict[str, Any]) -> None:
        lifecycle_events.append({"task_id": task_id, "event": event})

    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        agent_run_launcher=AgentRunLauncher(
            registry=registry,
            subagent_registry=get_subagent_registry(),
            worker=worker,
            lifecycle_publisher=_publish_lifecycle,
        ),
        agent_run_lifecycle_publisher=_publish_lifecycle,
    )

    parent_turn = asyncio.create_task(
        facade.handle_turn(_chat_inputs("Run service discovery against 10.0.0.10"))
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)

    assert parent_turn.done() is False
    assert parent_executor.calls == []
    assert len(worker.calls) == 1
    worker_call = worker.calls[0]
    assignment = worker_call["assignment"]
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("service_enumeration",)
    child_config = worker_call["runtime_config"]["configurable"]
    assert child_config["graph_name"] == GRAPH_NAME_SUBAGENT
    assert child_config["thread_id"] != (
        "graph-00000000000040008000000000000042"
    )
    assert child_config["runtime_projection"]["runtime_placement_mode"] == "runner"
    assert child_config["runtime_projection"]["runner_id"] == "runner-1"
    assert "runtime_services" not in child_config
    assert [item["event"]["agent_run"]["status"] for item in lifecycle_events] == [
        "queued",
        "running",
    ]
    first_metadata = lifecycle_events[0]["event"]["metadata"]
    assert first_metadata["producer_type"] == "subagent"
    assert first_metadata["agent_kind"] == "recon"
    assert first_metadata["agent_display_name"] == "Pathfinder"
    assert first_metadata["internal_only"] is False

    _release_call(worker, 0)
    result = await asyncio.wait_for(parent_turn, timeout=1)

    assert (
        result.final_text
        == "The parent PAR cycle finalized the Pathfinder evidence."
    )
    assert result.metadata["branch"] == "subagent"
    assert result.metadata["status"] == "completed"
    assert result.metadata["handoff_agent_run_id"] == assignment.agent_run_id
    assert len(parent_executor.calls) == 1
    parent_call = parent_executor.calls[0]
    assert parent_call["decision"]["parent_graph_routes"] == [
        "post_action_reasoning",
        "decision_router",
        "finalize",
    ]
    assert parent_call["input_todo_list"] == ()
    parent_config = parent_call["config"]["configurable"]
    assert "producer_type" not in parent_config
    assert "agent_run_id" not in parent_config
    parent_results = list(parent_call["completed_results"])
    assert parent_results[0]["agent_run_id"] == assignment.agent_run_id
    assert parent_results[0]["outcome"] == "completed"
    assert parent_results[0]["summary"] == "Pathfinder completed for 10.0.0.10."
    assert parent_results[0]["tools_used"] == [
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap"
    ]

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "completed"
    assert entries[0].result is not None
    assert entries[0].result.tools_used == (
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    )
    terminal_agent_events = [
        item for item in lifecycle_events if "agent_run" in item["event"]
    ]
    assert terminal_agent_events[-1]["event"]["agent_run"]["status"] == "completed"
    assert terminal_agent_events[-1]["event"]["metadata"]["agent_run_id"] == (
        assignment.agent_run_id
    )
    assert await registry.consume_result(
        tenant_id=7,
        task_id=42,
        agent_run_id=assignment.agent_run_id,
    ) is None


@pytest.mark.asyncio
async def test_subagent_pilot_par_direct_tool_after_handoff_finalizes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _ScriptedSubagentWorker(auto_release=True)
    parent_executor = _CompiledParentHandoffExecutor()
    _install_compiled_parent_handoff_deterministic_seams(monkeypatch, parent_executor)

    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        agent_run_launcher=AgentRunLauncher(
            registry=registry,
            subagent_registry=get_subagent_registry(),
            worker=worker,
            lifecycle_publisher=_ignore_lifecycle_event,
        ),
        agent_run_lifecycle_publisher=_ignore_lifecycle_event,
    )

    result = await asyncio.wait_for(
        facade.handle_turn(
            _chat_inputs("Run service discovery against 10.0.0.10")
        ),
        timeout=1,
    )

    assert result.final_text == (
        "The parent PAR cycle used a direct tool and finalized once."
    )
    assert result.metadata["status"] == "completed"
    assert result.metadata["parent_final_answer_count"] == 1
    assert result.metadata["direct_tool_execution_count"] == 1
    assert len(parent_executor.calls) == 1
    parent_call = parent_executor.calls[0]
    assert parent_call["completed_results"][0]["agent_run_id"] == (
        worker.calls[0]["assignment"].agent_run_id
    )
    assert result.metadata["parent_par_sources"] == [
        SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
        DIRECT_TOOL_OUTCOME_SOURCE,
    ]
    assert result.metadata["parent_graph_routes"] == [
        "prepare_handoff_context",
        "post_action_reasoning",
        "decision_router",
        "select_tool_categories",
        "prepare_tool_plan",
        "articulation",
        "approval_gate",
        "dispatch_tool",
        "tool_synthesizer",
        "prepare_direct_tool_context",
        "post_action_reasoning",
        "decision_router",
        "format_results",
        "finalize",
    ]
    assert parent_executor.node_calls == [
        "post_action_reasoning",
        "select_tool_categories",
        "prepare_tool_plan",
        "articulation",
        "approval_gate",
        "dispatch_tool",
        "tool_synthesizer",
        "post_action_reasoning",
        "format_results",
        "finalize",
    ]
    assert result.metadata["router_outcome"]["action"] == "finalize"

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].result_consumed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["completed", "partial", "blocked", "failed", "cancelled"],
)
async def test_subagent_pilot_projects_every_terminal_child_outcome_to_par(
    outcome: str,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _ScriptedSubagentWorker(outcomes=(outcome,), auto_release=True)
    parent_executor = _ScriptedParentParExecutor()

    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        agent_run_launcher=AgentRunLauncher(
            registry=registry,
            subagent_registry=get_subagent_registry(),
            worker=worker,
            lifecycle_publisher=_ignore_lifecycle_event,
        ),
        agent_run_lifecycle_publisher=_ignore_lifecycle_event,
    )

    result = await asyncio.wait_for(
        facade.handle_turn(
            _chat_inputs(
                f"Run service discovery against 10.0.0.10 for {outcome} evidence"
            )
        ),
        timeout=1,
    )

    assert result.metadata["status"] == "completed"
    assert len(parent_executor.calls) == 1
    parent_results = parent_executor.calls[0]["completed_results"]
    assert parent_results[0]["outcome"] == outcome
    assert parent_executor.calls[0]["decision"]["router_outcome"]["action"] == (
        "finalize"
    )


@pytest.mark.asyncio
async def test_subagent_pilot_par_followup_delegation_uses_bounded_objective_once(
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _ScriptedSubagentWorker(outcomes=("partial", "completed"))
    followup_objective = (
        "Why: Pathfinder returned partial HTTPS evidence with an unresolved "
        "banner contradiction. Bounded work: inspect only the approved "
        "10.0.0.10 HTTPS service evidence. Complete when the banner finding is "
        "accepted, contradicted, or explicitly limited."
    )

    def _decide(call: dict[str, Any]) -> dict[str, Any]:
        run_ids = tuple(result["agent_run_id"] for result in call["completed_results"])
        if len(run_ids) == 1 and call["completed_results"][0]["outcome"] == "partial":
            return {
                "final_text": "PAR delegated a bounded follow-up.",
                "todo_list": ["Parent todo waits for follow-up evidence"],
                "router_outcome": {
                    "action": "delegate_subagent",
                    "candidate_id": "par-followup-1",
                    "agent_handoff": {
                        "agent_handoff": "required",
                        "subagent": "pathfinder",
                        "objective": followup_objective,
                    },
                },
                "parent_graph_routes": [
                    "post_action_reasoning",
                    "decision_router",
                    "delegate_subagent",
                ],
            }
        return _finalize_parent_decision(call)

    parent_executor = _ScriptedParentParExecutor(_decide)
    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        agent_run_launcher=AgentRunLauncher(
            registry=registry,
            subagent_registry=get_subagent_registry(),
            worker=worker,
            lifecycle_publisher=_ignore_lifecycle_event,
        ),
        agent_run_lifecycle_publisher=_ignore_lifecycle_event,
    )

    parent_turn = asyncio.create_task(
        facade.handle_turn(_chat_inputs("Run service discovery against 10.0.0.10"))
    )
    await _wait_for_call_count(worker, 1)
    _release_call(worker, 0)
    await _wait_for_call_count(worker, 2)

    followup_assignment = worker.calls[1]["assignment"]
    assert followup_assignment.objective == followup_objective
    assert "Why:" in followup_assignment.objective
    assert "Bounded work:" in followup_assignment.objective
    assert "Complete when" in followup_assignment.objective

    _release_call(worker, 1)
    result = await asyncio.wait_for(parent_turn, timeout=1)

    assert (
        result.final_text
        == "The parent PAR cycle finalized the Pathfinder evidence."
    )
    assert [
        call["decision"]["router_outcome"]["action"]
        for call in parent_executor.calls
    ] == [
        "delegate_subagent",
        "finalize",
    ]
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    consumed = {entry.agent_run_id: entry.result_consumed for entry in entries}
    assert consumed == {
        worker.calls[0]["assignment"].agent_run_id: True,
        followup_assignment.agent_run_id: True,
    }
    assert len(parent_executor.calls) == 2


@pytest.mark.asyncio
async def test_subagent_pilot_waits_for_active_work_and_keeps_tasks_isolated(
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _ScriptedSubagentWorker()

    def _decide(call: dict[str, Any]) -> dict[str, Any]:
        if call["active_runs"]:
            return {
                "final_text": "PAR is waiting for active Pathfinder work.",
                "todo_list": ["Parent todo remains pending until active work returns"],
                "router_outcome": {
                    "action": "wait_for_subagents",
                    "candidate_id": f"par-wait-task-{call['task_id']}",
                },
                "parent_graph_routes": [
                    "post_action_reasoning",
                    "decision_router",
                    "wait_for_subagents",
                ],
            }
        final = _finalize_parent_decision(call)
        final["final_text"] = f"Task {call['task_id']} finalized after PAR."
        return final

    parent_executor = _ScriptedParentParExecutor(_decide)
    definitions = _concurrent_pathfinder_registry()
    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        agent_run_launcher=AgentRunLauncher(
            registry=registry,
            subagent_registry=definitions,
            worker=worker,
            lifecycle_publisher=_ignore_lifecycle_event,
        ),
        agent_run_lifecycle_publisher=_ignore_lifecycle_event,
        subagent_registry=definitions,
    )

    task_42_turn = asyncio.create_task(
        facade.handle_turn(
            _chat_inputs(
                "Run two service discovery scans against 10.0.0.10 and 10.0.0.11",
                task_id=42,
                conversation_id="conversation-42",
            )
        )
    )
    await _wait_for_call_count(worker, 2)
    _release_call(worker, 0)
    await _wait_for_parent_call_count(parent_executor, 1)

    first_parent_call = parent_executor.calls[0]
    assert first_parent_call["task_id"] == 42
    assert first_parent_call["decision"]["router_outcome"]["action"] == (
        "wait_for_subagents"
    )
    assert [run["agent_run_id"] for run in first_parent_call["active_runs"]] == [
        worker.calls[1]["assignment"].agent_run_id
    ]
    assert task_42_turn.done() is False

    task_43_turn = asyncio.create_task(
        facade.handle_turn(
            _chat_inputs(
                "Run service discovery against 10.0.0.10",
                task_id=43,
                conversation_id="conversation-43",
            )
        )
    )
    await _wait_for_call_count(worker, 3)
    _release_call(worker, 2)
    task_43_result = await asyncio.wait_for(task_43_turn, timeout=1)

    assert task_43_result.final_text == "Task 43 finalized after PAR."
    assert task_42_turn.done() is False

    _release_call(worker, 1)
    task_42_result = await asyncio.wait_for(task_42_turn, timeout=1)

    assert task_42_result.final_text == "Task 42 finalized after PAR."
    task_42_calls = [
        call for call in parent_executor.calls if call["task_id"] == 42
    ]
    assert [call["decision"]["router_outcome"]["action"] for call in task_42_calls] == [
        "wait_for_subagents",
        "finalize",
    ]
    entries_42 = await registry.list_task_runs(tenant_id=7, task_id=42)
    entries_43 = await registry.list_task_runs(tenant_id=7, task_id=43)
    assert all(entry.result_consumed for entry in entries_42)
    assert all(entry.result_consumed for entry in entries_43)


def test_subagent_pilot_does_not_add_durable_schema_paths() -> None:
    repo_root = os.getcwd()
    absent_paths = [
        "backend/models/agent_run.py",
        "backend/repositories/agent_runs",
        "backend/migrations/versions",
    ]

    for path in absent_paths[:2]:
        assert not os.path.exists(os.path.join(repo_root, path))

    versions_dir = os.path.join(repo_root, "backend/migrations/versions")
    if os.path.isdir(versions_dir):
        migration_names = os.listdir(versions_dir)
        assert not any(
            "subagent" in name or "agent_run" in name for name in migration_names
        )
