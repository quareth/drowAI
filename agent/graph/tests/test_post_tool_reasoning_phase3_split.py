""" tests for post-tool reasoning two-call orchestration.

These tests verify decision and articulation are produced in separate LLM calls."""

from __future__ import annotations

from typing import Any, List
from unittest.mock import patch

import pytest

from agent.graph.nodes.post_tool_reasoning import post_tool_reasoning
from agent.graph.nodes.post_tool_reasoning.models import ToolIntent
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from core.llm import ROLE_POST_TOOL_ARTICULATOR, ROLE_POST_TOOL_OBSERVATION


class Phase3MockLLM:
    """Mock LLM used to validate split decision + articulation calls."""

    class Response:
        def __init__(
            self,
            content: str,
            structured_output: dict[str, Any] | None = None,
            usage: Any | None = None,
        ):
            self.content = content
            self.structured_output = structured_output
            self.usage = usage

    def __init__(self, decision_payload: dict[str, Any], observation_chunks: List[str]):
        self.decision_payload = decision_payload
        self.observation_chunks = observation_chunks
        self.chat_with_usage_calls = 0
        self.stream_chat_messages_calls = 0
        self.chat_calls = 0

    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> "Phase3MockLLM.Response":
        self.chat_with_usage_calls += 1
        if kwargs.get("structured_output") is not None and self.chat_with_usage_calls == 1:
            return self.Response("ignored", structured_output=self.decision_payload)

        return self.Response("Observation generated from non-streaming path.")

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        self.chat_calls += 1
        return "Observation generated from fallback chat path."

    def stream_chat_messages(self, messages: list[dict[str, str]], **_kwargs: Any):
        self.stream_chat_messages_calls += 1

        async def iterator():
            for chunk in self.observation_chunks:
                yield chunk

        return iterator()


def _sample_state_for_phase3() -> InteractiveState:
    """Build minimal interactive state with synthesized tool output."""
    facts = FactsState(
        task_id=123,
        message="Perform reconnaissance and summarize open services",
        conversation_id="conv-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        tool_parameters={"target": "127.0.0.1"},
        current_goal="Discover open services",
        iterations=1,
        metadata={
            "api_key": "test-api-key",
            "model": "gpt-4o-mini",
            "synthesized_output": {
                "tool": "nmap",
                "summary": "Scan completed",
                "key_findings": ["Port 22 open", "Port 80 open"],
            },
        },
        decision_history=["start: initialized"],
    )
    trace = TraceState(reasoning=[], observations=[], decision_log=[])
    return InteractiveState(facts=facts, trace=trace)


@pytest.mark.asyncio
async def test_phase3_node_uses_separate_decision_and_articulation_streaming() -> None:
    """Node should call decision first and articulation through stream path second."""
    state = _sample_state_for_phase3()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "call_tool",
            "action_reasoning": "Need to inspect discovered services",
            "tool_intent": ToolIntent(description="Enumerate discovered services").model_dump(),
        },
        observation_chunks=[
            "Tool output indicates host is reachable. ",
            "I will continue with enumeration.",
        ],
    )
    captured_events = []

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.derive_dr_stream_identifiers",
        return_value=("conv-123", "turn-456", None),
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_turn_sequence",
        return_value=1,
    ):
        result = await post_tool_reasoning(
            state,
            writer=captured_events.append,
        )

    assert mock_llm.chat_with_usage_calls == 1
    assert mock_llm.stream_chat_messages_calls == 1
    assert mock_llm.chat_calls == 0
    assert result["facts"]["metadata"]["observation_streamed"] is True
    assert result["facts"]["metadata"]["post_tool_reasoning_completed"] is True
    assert result["facts"]["metadata"]["last_post_tool_action"] == "call_tool"


@pytest.mark.asyncio
async def test_phase3_node_uses_separate_non_streaming_articulation() -> None:
    """Node should still do two calls in non-writer mode: decision + articulation."""
    state = _sample_state_for_phase3()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "think_more",
            "action_reasoning": "Need more analysis before tool call",
            "tool_intent": ToolIntent(description="Re-evaluate findings").model_dump(),
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        result = await post_tool_reasoning(state)

    # First call: structured decision. Second call: articulation fallback path.
    assert mock_llm.chat_with_usage_calls == 2
    assert result["facts"]["metadata"]["observation_streamed"] is False
    assert result["facts"]["metadata"]["last_post_tool_action"] == "think_more"


@pytest.mark.asyncio
async def test_non_streaming_articulation_propagates_provider_refusal() -> None:
    """A refusal from the articulation call must not become fallback progress."""
    state = _sample_state_for_phase3()
    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(
            provider="openai",
            model="gpt-4o-mini",
            category="content_filter",
        ),
    )

    class RefusingArticulationLLM(Phase3MockLLM):
        async def chat_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            **kwargs: Any,
        ) -> "Phase3MockLLM.Response":
            self.chat_with_usage_calls += 1
            if kwargs.get("structured_output") is not None:
                return self.Response("ignored", structured_output=self.decision_payload)
            raise refusal

    mock_llm = RefusingArticulationLLM(
        decision_payload={
            "next_action": "think_more",
            "action_reasoning": "Need more analysis before a tool call",
            "tool_intent": ToolIntent(description="Re-evaluate findings").model_dump(),
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node._make_fallback_observation",
        side_effect=AssertionError("refusal must not create a fallback observation"),
    ):
        with pytest.raises(LLMRefusalError) as exc_info:
            await post_tool_reasoning(state)

    assert exc_info.value is refusal
    assert state.trace.observations == []


@pytest.mark.asyncio
async def test_phase4_articulation_uses_dedicated_role() -> None:
    """Decision and articulation should resolve different LLM roles."""
    state = _sample_state_for_phase3()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "call_tool",
            "action_reasoning": "Need to inspect discovered services",
            "tool_intent": ToolIntent(description="Enumerate discovered services").model_dump(),
        },
        observation_chunks=["Streaming observation text."],
    )

    roles: list[str] = []

    def resolve_llm_client_factory(_metadata, _context, role: str | None = None) -> object:
        roles.append(role)
        return mock_llm

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        side_effect=resolve_llm_client_factory,
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.derive_dr_stream_identifiers",
        return_value=("conv-123", "turn-456", None),
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_turn_sequence",
        return_value=1,
    ):
        events: list[object] = []
        await post_tool_reasoning(
            state,
            writer=events.append,
        )

    assert roles[0] == ROLE_POST_TOOL_OBSERVATION
    assert roles[1] == ROLE_POST_TOOL_ARTICULATOR
