"""Shared deterministic harness used by LangGraph regression tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Sequence

from agent.graph.builders.deep_reasoning_builder import _route_decision
from agent.graph.builders.simple_tool_builder import _route_after_router
from agent.graph.nodes.decision_router.helpers import extract_action_label
from agent.graph.nodes.planner_prompting import build_planning_prompt
from agent.graph.state import InteractiveInput
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
    PersistenceContext,
)
from backend.services.langgraph_chat.facade_helpers import build_metadata, build_thread_config
from backend.services.langgraph_chat.routing.selectors import select_branch

GRAPH_THREAD_ID = "a" * 32


@dataclass(frozen=True)
class SignatureResult:
    """Compact behavior signature for replay-style regression checks."""

    branch: str
    node_path_signature: str
    key_decisions: Sequence[str]
    interrupt_flags: Mapping[str, bool]
    terminal_status: str
    event_type_sequence: Sequence[str]


class RegressionHarness:
    """Utilities that run deterministic slices of the LangGraph execution path."""

    def make_chat_inputs(
        self,
        *,
        task_id: int,
        user_id: int,
        message: str,
        history: Sequence[Dict[str, Any]],
        api_key: str | None = "test-key",
        model: str | None = "gpt-5.2",
        conversation_id: str | None = None,
    ) -> ChatInputs:
        return ChatInputs(
            task_id=task_id,
            user_id=user_id,
            message=message,
            conversation_id=conversation_id,
            history=history,
            api_key=api_key,
            model=model,
        )

    def make_runtime_config(
        self,
        *,
        chat_inputs: ChatInputs,
        execution_mode: ExecutionMode = ExecutionMode.NORMAL_CHAT,
        metadata: Mapping[str, Any] | None = None,
    ) -> LangGraphRuntimeConfig:
        return LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            execution_mode=execution_mode,
            metadata=dict(metadata or {}),
        )

    def resolve_branch(self, execution_mode: ExecutionMode) -> str:
        runtime = self.make_runtime_config(
            chat_inputs=self.make_chat_inputs(
                task_id=1,
                user_id=1,
                message="branch-check",
                history=[],
            ),
            execution_mode=execution_mode,
        )
        return select_branch(runtime).value

    def build_history_metadata(
        self,
        *,
        message: str,
        history: Sequence[Dict[str, Any]],
        metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        chat_inputs = self.make_chat_inputs(
            task_id=99,
            user_id=7,
            message=message,
            history=history,
        )
        metadata_dict = dict(metadata or {})
        metadata_dict.setdefault(
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle(
                conversation_id="conv-history",
                turn_id="turn-history",
                turn_sequence=0,
                messages=list(history),
                current_message=message,
            ),
        )
        runtime = self.make_runtime_config(
            chat_inputs=chat_inputs,
            execution_mode=ExecutionMode.NORMAL_CHAT,
            metadata=metadata_dict,
        )
        return build_metadata(chat_inputs, runtime)

    def build_planner_prompt(
        self,
        *,
        user_message: str,
        history: Sequence[Dict[str, Any]],
    ) -> str:
        # Phase 5/6 cutover: the DR planner prompt reads cross-turn
        # continuity from ``metadata["context_bundle"]`` via the shared
        # ConversationContextBundle, not from a raw
        # ``metadata["conversation_history"]`` key.
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        metadata = {
            "conversation_history": list(history),
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-regression",
                turn_id="turn-regression",
                turn_sequence=0,
                messages=list(history),
                current_message=user_message,
            ),
        }
        return build_planning_prompt(
            targets=[],
            metadata=metadata,
            available_tools=["shell.exec", "information_gathering.network_discovery.nmap"],
        )

    def route_simple_tool_decision(
        self,
        *,
        decision_history: Sequence[str],
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        state = InteractiveInput(
            task_id=111,
            message="route simple tool",
            metadata={
                **dict(metadata or {}),
                "initial_capability": "simple_tool_execution",
            },
        ).to_state()
        state.facts.capability = "simple_tool_execution"
        state.facts.decision_history = list(decision_history)
        action = (
            extract_action_label(decision_history[-1]).strip().lower()
            if decision_history
            else ""
        )
        state.facts.metadata["router_outcome"] = {"action": action}
        return _route_after_router(state)

    def route_deep_reasoning_decision(
        self,
        *,
        decision_history: Sequence[str],
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        interactive = InteractiveInput(
            task_id=222,
            message="route deep reasoning",
            metadata=dict(metadata or {}),
        ).to_state()
        interactive.facts.capability = "deep_reasoning"
        interactive.facts.decision_history = list(decision_history)
        action = (
            extract_action_label(decision_history[-1]).strip().lower()
            if decision_history
            else ""
        )
        interactive.facts.metadata["router_outcome"] = {"action": action}
        return _route_decision(interactive)

    def make_thread_config(
        self,
        *,
        conversation_id: str | None,
        anchor_sequence: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        chat_inputs = ChatInputs(
            task_id=77,
            user_id=5,
            message="thread-config",
            conversation_id=conversation_id,
            history=[],
            api_key=None,
            model=None,
            anchor_sequence=anchor_sequence,
        )
        runtime_metadata = dict(metadata or {})
        runtime_metadata.setdefault("graph_thread_id", GRAPH_THREAD_ID)
        runtime = LangGraphRuntimeConfig(
            chat_inputs=chat_inputs,
            persistence=PersistenceContext(anchor_sequence=anchor_sequence),
            execution_mode=ExecutionMode.SIMPLE_TOOL,
            metadata=runtime_metadata,
        )
        return build_thread_config(runtime, task_id=chat_inputs.task_id)

    def signature(
        self,
        *,
        branch: str,
        node_path_signature: str,
        key_decisions: Sequence[str],
        terminal_status: str,
        event_type_sequence: Sequence[str],
        interrupted: bool = False,
    ) -> SignatureResult:
        return SignatureResult(
            branch=branch,
            node_path_signature=node_path_signature,
            key_decisions=list(key_decisions),
            interrupt_flags={"interrupted": interrupted},
            terminal_status=terminal_status,
            event_type_sequence=list(event_type_sequence),
        )
