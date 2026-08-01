"""Facade tests for same-process subagent result projection acceptance."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)


def _runtime_identity(*, tenant_id: int = 7, task_id: int = 42) -> AgentRuntimeIdentity:
    return build_runtime_identity(tenant_id=tenant_id, task_id=task_id)


def _assignment() -> AgentAssignment:
    return build_agent_assignment(
        assignment_id="assign-run-1",
        agent_run_id="run-1",
        runtime_identity=_runtime_identity(),
    )


def _result() -> AgentResult:
    return build_agent_result(
        _assignment(),
        summary="Pathfinder found HTTP.",
        key_findings=["HTTP on 80"],
        evidence_refs=[
            {
                "kind": "artifact",
                "evidence_id": "nmap-xml",
                "summary": "Nmap XML artifact",
            }
        ],
        tools_used=["nmap"],
        limitations=[],
        recommended_next_steps=["Review HTTP headers"],
    )


def _forbidden_child_payload() -> dict[str, Any]:
    return {
        "working_memory": {"available_findings": ["private child memory"]},
        "current_turn_phases": [{"source": "tool", "body": "private phase"}],
        "subagent_observation_transcript": [{"summary": "legacy transcript"}],
        "checkpoint_state": {"messages": ["private checkpoint payload"]},
        "raw_tool_output": "RAW_STDOUT should not reach the parent",
    }


class _ContextBuilder:
    def build_runtime_config(
        self,
        *,
        chat_inputs: ChatInputs,
        metadata: dict[str, Any] | None = None,
    ) -> LangGraphRuntimeConfig:
        merged = dict(metadata or {})
        merged.update(
            {
                "tenant_id": 7,
                "deterministic_mode": True,
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id=chat_inputs.conversation_id or "",
                    turn_id="turn-2",
                    turn_sequence=2,
                    messages=list(chat_inputs.history),
                    current_message=chat_inputs.message,
                ),
            }
        )
        return LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            execution_mode=ExecutionMode.SIMPLE_TOOL,
            metadata=merged,
        )


class _PriorTurnReferenceMaterializer:
    def materialize_for_runtime_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_facade_marks_projected_result_consumed_after_handler_accepts() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result(),
    )
    capture: dict[str, Any] = {}
    facade = LangGraphChatFacade(
        context_builder=_ContextBuilder(),
        agent_run_registry=registry,
        prior_turn_reference_materializer=_PriorTurnReferenceMaterializer(),
    )

    async def _handle(config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        capture["metadata"] = dict(config.metadata)
        capture["bundle"] = dict(config.metadata[METADATA_CONTEXT_BUNDLE_KEY])
        return LangGraphChatResult(
            final_text="accepted",
            conversation_id=config.chat_inputs.conversation_id,
        )

    facade._handlers = {
        branch: SimpleNamespace(handle=_handle) for branch in facade._handlers
    }
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=3,
        message="Use Pathfinder results",
        conversation_id="conversation-1",
        history=[],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
    )

    await facade.handle_turn(chat_inputs)

    metadata_result = capture["metadata"]["completed_agent_results"][0]
    bundle_result = capture["bundle"]["completed_agent_results"][0]
    assert metadata_result == bundle_result
    assert metadata_result["summary"] == "Pathfinder found HTTP."
    assert metadata_result["evidence_refs"] == [
        {
            "kind": "artifact",
            "evidence_id": "nmap-xml",
            "summary": "Nmap XML artifact",
        }
    ]
    assert metadata_result["final_checkpoint_id"] == "checkpoint-1"
    assert bundle_result["agent_run_id"] == "run-1"
    for forbidden_key in _forbidden_child_payload():
        assert forbidden_key not in metadata_result
        assert forbidden_key not in bundle_result
    assert await registry.consume_result(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    ) is None


@pytest.mark.asyncio
async def test_facade_does_not_consume_projected_result_when_handler_fails() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result(),
    )
    facade = LangGraphChatFacade(
        context_builder=_ContextBuilder(),
        agent_run_registry=registry,
        prior_turn_reference_materializer=_PriorTurnReferenceMaterializer(),
    )

    async def _handle(_config: LangGraphRuntimeConfig) -> LangGraphChatResult:
        raise RuntimeError("handler failed")

    facade._handlers = {
        branch: SimpleNamespace(handle=_handle) for branch in facade._handlers
    }
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=3,
        message="Use Pathfinder results",
        conversation_id="conversation-1",
        history=[],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
    )

    with pytest.raises(RuntimeError, match="handler failed"):
        await facade.handle_turn(chat_inputs)

    assert await registry.consume_result(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    ) == _result()
