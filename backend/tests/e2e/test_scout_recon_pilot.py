"""End-to-end pilot checks for process-local Scout recon orchestration.

These tests prove the migration-free Scout pilot through deterministic doubles
at the active service seams. They keep external runtime execution out of scope
while exercising facade routing, async launch, process-local lifecycle,
attribution events, cancellation/restart limitations, and same-process result
handoff.
"""

from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.services.langgraph_chat.routing.selectors import ChatBranch


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
        merged = {
            "tenant_id": 7,
            "graph_thread_id": "00000000000040008000000000000042",
            "runtime_placement_mode": "runner",
            "workspace_id": "task-42",
            "workspace_path": "/workspace/task-42",
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

    async def enrich_runtime_config(self, runtime_config: LangGraphRuntimeConfig, **_: Any):
        message = runtime_config.chat_inputs.message
        self.messages.append(message)
        if "service discovery" in message.lower():
            runtime_config.execution_mode = ExecutionMode.SIMPLE_TOOL
            runtime_config.metadata.update(
                {
                    "intent_classifier_label": "direct_executor",
                    "intent_classifier_raw_label": "direct_executor",
                    "intent_classifier_raw_response": {
                        "suggested_capabilities": ["service discovery"],
                        "agent_handoffs": [
                            {
                                "agent_handoff": "required",
                                "subagent": "pathfinder",
                                "objective": (
                                    "Run service discovery against 10.0.0.10."
                                ),
                            }
                        ],
                    },
                    "intent_hints": {
                        "classifier_label": "direct_executor",
                        "targets": ["10.0.0.10"],
                        "suggested_capabilities": ["service discovery"],
                        "agent_handoffs": [
                            {
                                "agent_handoff": "required",
                                "subagent": "pathfinder",
                                "objective": (
                                    "Run service discovery against 10.0.0.10."
                                ),
                            }
                        ],
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


class _DelayedScoutWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        assignment: AgentAssignment,
        runtime_config: dict[str, Any],
        graph_thread_id: str,
        is_cancel_requested: Any,
    ) -> AgentResult:
        self.calls.append(
            {
                "assignment": assignment,
                "runtime_config": runtime_config,
                "graph_thread_id": graph_thread_id,
            }
        )
        self.started.set()
        await self.release.wait()
        assert not await is_cancel_requested()
        return AgentResult(
            agent_run_id=assignment.agent_run_id,
            agent_id="pathfinder",
            agent_kind="recon",
            outcome="completed",
            summary="Scout found HTTPS on 443.",
            key_findings=["HTTPS exposed on 443"],
            evidence_refs=[
                {
                    "kind": "artifact",
                    "path": "/workspace/task-42/nmap.xml",
                    "summary": "Compact Nmap XML artifact",
                }
            ],
            tools_used=["information_gathering.network_discovery.nmap"],
            limitations=["Single approved target only."],
            recommended_next_steps=["Review the HTTPS service banner."],
            final_checkpoint_id="cp-scout-final",
        )


class _ParentFinalizerExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: dict[str, Any],
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        self.calls.append(
            {
                "compiled_graph": compiled_graph,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "kwargs": kwargs,
            }
        )
        final_state = copy.deepcopy(graph_input)
        final_state["trace"]["final_text"] = (
            "The Scout scan found HTTPS exposed on port 443."
        )
        return GraphExecutionResult(final_state=final_state)


def _chat_inputs(message: str) -> ChatInputs:
    return ChatInputs(
        task_id=42,
        user_id=3,
        message=message,
        conversation_id="conversation-42",
        history=[{"role": "user", "content": message}],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        agent_mode=AgentMode.AGENT,
    )


@pytest.mark.asyncio
async def test_scout_recon_pilot_hands_result_back_to_original_parent_turn(
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    worker = _DelayedScoutWorker()
    parent_executor = _ParentFinalizerExecutor()
    lifecycle_events: list[dict[str, Any]] = []

    async def _publish_lifecycle(task_id: int, event: dict[str, Any]) -> None:
        lifecycle_events.append({"task_id": task_id, "event": event})

    facade = LangGraphChatFacade(
        context_builder=_PilotContextBuilder(),
        executor=parent_executor,
        intent_classifier=_PilotIntentClassifier(),
        prior_turn_reference_materializer=_NoopPriorTurnReferenceMaterializer(),
        agent_run_registry=registry,
        scout_launcher=None,
        agent_run_lifecycle_publisher=_publish_lifecycle,
    )
    facade._handlers[ChatBranch.SUBAGENT]._launcher._worker = worker

    parent_turn = asyncio.create_task(
        facade.handle_turn(_chat_inputs("Run service discovery against 10.0.0.10"))
    )
    await asyncio.wait_for(worker.started.wait(), timeout=1)

    assert parent_turn.done() is False
    assert len(worker.calls) == 1
    worker_call = worker.calls[0]
    assignment = worker_call["assignment"]
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("service_enumeration",)
    child_config = worker_call["runtime_config"]["configurable"]
    assert child_config["graph_name"] == "scout_recon"
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

    worker.release.set()
    result = await asyncio.wait_for(parent_turn, timeout=1)

    assert result.final_text == "The Scout scan found HTTPS exposed on port 443."
    assert result.metadata["branch"] == "subagent"
    assert result.metadata["status"] == "completed"
    assert result.metadata["handoff_agent_run_id"] == assignment.agent_run_id
    assert len(parent_executor.calls) == 1
    parent_call = parent_executor.calls[0]
    parent_config = parent_call["config"]["configurable"]
    assert "producer_type" not in parent_config
    assert "agent_run_id" not in parent_config
    parent_input = parent_call["graph_input"]
    parent_results = parent_input["facts"]["metadata"][
        METADATA_CONTEXT_BUNDLE_KEY
    ]["completed_agent_results"]
    assert parent_results[0]["agent_run_id"] == assignment.agent_run_id
    assert parent_results[0]["summary"] == "Scout found HTTPS on 443."
    assert parent_results[0]["tools_used"] == [
        "information_gathering.network_discovery.nmap"
    ]

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "completed"
    assert entries[0].result is not None
    assert entries[0].result.tools_used == (
        "information_gathering.network_discovery.nmap",
    )
    assert lifecycle_events[-1]["event"]["agent_run"]["status"] == "completed"
    assert lifecycle_events[-1]["event"]["metadata"]["agent_run_id"] == (
        assignment.agent_run_id
    )
    assert await registry.consume_result(
        tenant_id=7,
        task_id=42,
        agent_run_id=assignment.agent_run_id,
    ) is None


def test_scout_pilot_does_not_add_durable_schema_paths() -> None:
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
        assert not any("scout" in name or "agent_run" in name for name in migration_names)
