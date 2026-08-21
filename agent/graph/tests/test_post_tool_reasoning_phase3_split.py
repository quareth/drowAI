"""Tests for one-turn post-tool observation and route orchestration.

These tests verify visible text and graph routing come from one model turn."""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import patch

import pytest

from agent.graph.nodes.post_tool_reasoning import (
    POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
    PostToolReasoningError,
    post_tool_reasoning,
)
from agent.graph.nodes.decision_router.router import decision_router
from agent.graph.nodes.post_tool_reasoning.models import ToolIntent
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from agent.providers.llm.core.base import LLMResponseOutcome, ToolCall
from backend.services.usage_tracking.models import UsageData
from core.llm import ROLE_POST_TOOL_OBSERVATION


class Phase3MockLLM:
    """Mock LLM used to validate combined text plus route-tool calls."""

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
        self.stream_route_calls = 0
        self.non_stream_route_calls = 0
        self.system_prompts: list[str] = []

    def _tool_call(self) -> ToolCall:
        action = self.decision_payload["next_action"]
        arguments = dict(self.decision_payload)
        if action != "call_tool":
            arguments.pop("tool_intent", None)
        if action != "delegate_subagent":
            arguments.pop("agent_handoff", None)
        return ToolCall(
            id="route-1",
            name="ptr_commit",
            arguments=json.dumps(arguments),
        )

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> "Phase3MockLLM.Response":
        self.non_stream_route_calls += 1
        self.system_prompts.append(system_prompt)
        response = self.Response(
            "".join(self.observation_chunks)
            or "I reviewed the completed evidence and selected the next step."
        )
        response.tool_calls = [self._tool_call()]
        return response

    async def stream_chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        tool_choice: Any = "auto",
        **_kwargs: Any,
    ) -> object:
        self.stream_route_calls += 1
        self.system_prompts.append(system_prompt)

        async def iterator():
            for chunk in self.observation_chunks:
                yield chunk

        class StreamResponse:
            content_iterator = iterator()

            @staticmethod
            def get_final_usage() -> dict[str, Any]:
                return {
                    "prompt_tokens": 10,
                    "completion_tokens": 6,
                    "total_tokens": 16,
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                }

            def get_final_tool_calls(inner_self) -> list[ToolCall]:
                return [self._tool_call()]

            @staticmethod
            def get_final_outcome() -> LLMResponseOutcome:
                return LLMResponseOutcome(status="completed")

        return StreamResponse()


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


def _sample_handoff_batch_state() -> InteractiveState:
    """Build minimal PAR state with completed child results and no tool output."""
    facts = FactsState(
        task_id=123,
        message="Use delegated evidence to decide the next step",
        conversation_id="conv-123",
        capability="deep_reasoning",
        current_goal="Evaluate delegated reconnaissance evidence",
        iterations=1,
        metadata={
            "api_key": "test-api-key",
            "model": "gpt-4o-mini",
            "turn_sequence": 7,
            "phase_sequence": 3,
            POST_ACTION_OUTCOME_SOURCE_METADATA_KEY: (
                SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
            ),
            "completed_agent_results": [
                {
                    "agent_run_id": "run-1",
                    "agent_id": "pathfinder",
                    "agent_kind": "pathfinder",
                    "agent_display_name": "Pathfinder",
                    "outcome": "completed",
                    "summary": "Found TCP/22 and TCP/80 open on the approved host.",
                    "key_findings": ["TCP/22 open", "TCP/80 open"],
                    "evidence_refs": [],
                    "tools_used": ["nmap"],
                    "limitations": [],
                    "recommended_next_steps": ["Enumerate HTTP service"],
                    "final_checkpoint_id": None,
                }
            ],
        },
    )
    trace = TraceState(reasoning=[], observations=[], decision_log=[])
    return InteractiveState(facts=facts, trace=trace)


@pytest.mark.asyncio
async def test_phase3_node_streams_observation_and_route_from_one_model_turn() -> None:
    """Node should stream visible text and consume one completed route tool."""
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
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.safe_inc"
    ) as mock_inc, patch(
        "agent.graph.nodes.post_tool_reasoning.node.safe_gauge"
    ) as mock_gauge:
        result = await post_tool_reasoning(
            state,
            writer=captured_events.append,
        )

    assert mock_llm.stream_route_calls == 1
    assert mock_llm.non_stream_route_calls == 0
    assert result["facts"]["metadata"]["observation_streamed"] is True
    assert result["facts"]["metadata"]["post_tool_reasoning_completed"] is True
    assert result["facts"]["metadata"]["last_post_tool_action"] == "call_tool"
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    recorded_gauges = {
        call.args[0]: call.args[1] for call in mock_gauge.call_args_list
    }
    assert "post_action_reasoning_cycle_source_direct_tool" in recorded_counters
    assert "post_action_reasoning_decision_route_call_tool" in recorded_counters
    assert recorded_gauges["post_action_reasoning_handoff_batch_size"] == 0
    assert recorded_gauges["post_action_reasoning_active_run_count"] == 0


@pytest.mark.asyncio
async def test_repeated_post_action_phases_emit_distinct_observation_cards() -> None:
    """Every PTR phase in one canonical turn reserves a fresh stream identity."""
    state = _sample_handoff_batch_state()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "finalize",
            "action_reasoning": "The delegated evidence resolves the request.",
            "user_goal_achieved": True,
        },
        observation_chunks=["Pathfinder completed the requested check."],
    )
    captured_events: list[dict[str, Any]] = []

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        first = await post_tool_reasoning(state, writer=captured_events.append)
        await post_tool_reasoning(first, writer=captured_events.append)

    starts = [
        event
        for event in captured_events
        if event.get("type") == "observation_start"
    ]
    assert [event.get("sub_turn_index") for event in starts] == [0, 1]


@pytest.mark.asyncio
async def test_handoff_batch_source_enters_par_without_synthesized_output() -> None:
    """Explicit handoff source should not require synthesized direct-tool output."""
    state = _sample_handoff_batch_state()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "finalize",
            "action_reasoning": "The delegated evidence is enough to answer.",
            "user_goal_achieved": True,
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.safe_inc"
    ) as mock_inc, patch(
        "agent.graph.nodes.post_tool_reasoning.node.safe_gauge"
    ) as mock_gauge:
        result = await post_tool_reasoning(state)

    metadata = result["facts"]["metadata"]
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    recorded_gauges = {
        call.args[0]: call.args[1] for call in mock_gauge.call_args_list
    }
    assert mock_llm.non_stream_route_calls == 1
    assert mock_llm.stream_route_calls == 0
    assert metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] == (
        SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
    )
    assert metadata["post_tool_reasoning_completed"] is True
    assert metadata["last_post_tool_action"] == "finalize"
    assert "synthesized_output" not in metadata
    assert (
        "post_action_reasoning_cycle_source_subagent_handoff_batch"
        in recorded_counters
    )
    assert "post_action_reasoning_decision_route_finalize" in recorded_counters
    assert "post_action_reasoning_finalization_decisions" in recorded_counters
    assert recorded_gauges["post_action_reasoning_handoff_batch_size"] == 1
    assert recorded_gauges["post_action_reasoning_active_run_count"] == 0


@pytest.mark.asyncio
async def test_handoff_batch_wait_without_active_run_is_coerced_safely() -> None:
    """A wait decision is only valid when deterministic active-run context exists."""
    state = _sample_handoff_batch_state()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "wait_for_subagents",
            "action_reasoning": "Another child result may still arrive.",
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        result = await post_tool_reasoning(state)

    metadata = result["facts"]["metadata"]
    assert metadata["last_post_tool_action"] == "think_more"
    assert metadata["candidate_decision"]["next_action"] == "think_more"
    assert "no active subagent runs" in metadata["candidate_decision"]["action_reasoning"]
    assert "synthesized_output" not in metadata


@pytest.mark.asyncio
async def test_handoff_batch_wait_with_active_run_is_preserved() -> None:
    """A wait decision remains routeable when active child work is visible."""
    state = _sample_handoff_batch_state()
    state.facts.metadata["active_agent_runs"] = [
        {
            "agent_run_id": "run-2",
            "assignment_id": "assignment-2",
            "agent_id": "enumerator",
            "agent_kind": "enumerator",
            "agent_display_name": "Enumerator",
            "objective": "Enumerate HTTP service.",
            "status": "running",
        }
    ]
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "wait_for_subagents",
            "action_reasoning": "Wait for the HTTP enumeration assignment.",
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        result = await post_tool_reasoning(state)

    metadata = result["facts"]["metadata"]
    assert metadata["last_post_tool_action"] == "wait_for_subagents"
    assert metadata["candidate_decision"]["next_action"] == "wait_for_subagents"


@pytest.mark.asyncio
async def test_handoff_batch_delegate_emits_one_shared_handoff_entry() -> None:
    """PAR delegation keeps the classifier-compatible handoff entry intact."""
    state = _sample_handoff_batch_state()
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "delegate_subagent",
            "action_reasoning": "Need bounded HTTP enumeration.",
            "agent_handoff": {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "Enumerate HTTP service on the approved target.",
                "skill_ids": ["network-reconnaissance"],
            },
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        result = await post_tool_reasoning(state)

    candidate = result["facts"]["metadata"]["candidate_decision"]
    assert candidate["next_action"] == "delegate_subagent"
    assert candidate["agent_handoff"] == {
        "agent_handoff": "required",
        "subagent": "pathfinder",
        "objective": "Enumerate HTTP service on the approved target.",
        "skill_ids": ["network-reconnaissance"],
    }


@pytest.mark.asyncio
async def test_handoff_batch_finalize_records_irrelevant_active_run_for_router() -> None:
    """PAR output can mark active child runs irrelevant before router finalizes."""
    state = _sample_handoff_batch_state()
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 8,
        "remaining_tool_calls": 4,
    }
    state.facts.metadata["active_agent_runs"] = [
        {
            "agent_run_id": "run-irrelevant",
            "assignment_id": "assignment-irrelevant",
            "agent_id": "pathfinder",
            "agent_kind": "pathfinder",
            "agent_display_name": "Pathfinder",
            "objective": "Continue optional enrichment.",
            "status": "running",
        }
    ]
    mock_llm = Phase3MockLLM(
        decision_payload={
            "next_action": "finalize",
            "action_reasoning": "The remaining active assignment is optional.",
            "par_irrelevant_active_agent_run_ids": ["run-irrelevant"],
        },
        observation_chunks=[],
    )

    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        par_result = await post_tool_reasoning(state)

    candidate = par_result["facts"]["metadata"]["candidate_decision"]
    assert candidate["par_irrelevant_active_agent_run_ids"] == ["run-irrelevant"]

    router_result = await decision_router(par_result)
    outcome = router_result["facts"]["metadata"]["router_outcome"]
    assert outcome["action"] == "finalize"
    assert outcome["reason"] == "candidate_decision_accepted"


@pytest.mark.asyncio
async def test_unknown_outcome_source_fails_explicitly() -> None:
    """Unknown source strings should fail before LLM calls."""
    state = _sample_handoff_batch_state()
    state.facts.metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] = "mystery"

    with pytest.raises(PostToolReasoningError) as exc_info:
        await post_tool_reasoning(state)

    assert exc_info.value.error_code == "invalid_post_action_outcome_source"


@pytest.mark.asyncio
async def test_handoff_batch_source_rejects_synthesized_tool_output() -> None:
    """Handoff batches must not also claim a synthesized direct-tool result."""
    state = _sample_handoff_batch_state()
    state.facts.metadata["synthesized_output"] = {
        "tool": "nmap",
        "summary": "Direct tool output belongs to the direct-tool source.",
    }

    with pytest.raises(PostToolReasoningError) as exc_info:
        await post_tool_reasoning(state)

    assert exc_info.value.error_code == "contradictory_post_action_outcome_source"


@pytest.mark.asyncio
async def test_handoff_batch_source_requires_completed_results() -> None:
    """A handoff-batch source is contradictory without bounded completed results."""
    state = _sample_handoff_batch_state()
    state.facts.metadata.pop("completed_agent_results")

    with pytest.raises(PostToolReasoningError) as exc_info:
        await post_tool_reasoning(state)

    assert exc_info.value.error_code == "contradictory_post_action_outcome_source"


@pytest.mark.asyncio
async def test_phase3_node_uses_one_non_streaming_route_turn() -> None:
    """Non-writer mode should receive visible text and route in one call."""
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

    assert mock_llm.non_stream_route_calls == 1
    assert len(mock_llm.system_prompts) == 1
    assert "Call the provided `ptr_commit` function exactly once" in mock_llm.system_prompts[0]
    assert "## Progress Tracking (CRITICAL)" in mock_llm.system_prompts[0]
    assert "After 3 unsuccessful attempts" in mock_llm.system_prompts[0]
    assert result["facts"]["metadata"]["observation_streamed"] is False
    assert result["facts"]["metadata"]["last_post_tool_action"] == "think_more"


@pytest.mark.asyncio
async def test_non_streaming_route_turn_propagates_provider_refusal() -> None:
    """A refusal from the combined route turn must not become fallback progress."""
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
        async def chat_with_tools_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            tools: list[Any],
            tool_choice: Any = "auto",
            **kwargs: Any,
        ) -> "Phase3MockLLM.Response":
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
async def test_route_turn_uses_only_user_selected_post_tool_role() -> None:
    """The combined turn should resolve one model role only."""
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

    def resolve_llm_client_factory(
        _metadata,
        _context,
        *,
        config=None,
        role: str | None = None,
    ) -> object:
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
    assert roles == [ROLE_POST_TOOL_OBSERVATION]


@pytest.mark.asyncio
async def test_missing_non_stream_commit_recovers_only_internal_commit() -> None:
    """Recovery should request only ptr_commit over already completed evidence."""
    state = _sample_state_for_phase3()

    class RecoveringCommitLLM(Phase3MockLLM):
        def __init__(self) -> None:
            super().__init__(
                decision_payload={
                    "next_action": "finalize",
                    "action_reasoning": "The completed scan resolved the request.",
                },
                observation_chunks=["The scan completed and resolved the request."],
            )
            self.choices: list[Any] = []
            self.tool_names: list[list[str]] = []

        async def chat_with_tools_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            tools: list[Any],
            tool_choice: Any = "auto",
            **kwargs: Any,
        ) -> "Phase3MockLLM.Response":
            self.non_stream_route_calls += 1
            self.choices.append(tool_choice)
            self.tool_names.append([tool.name for tool in tools])
            response = self.Response(
                "The scan completed and resolved the request."
                if self.non_stream_route_calls == 1
                else ""
            )
            if self.non_stream_route_calls == 1:
                response.tool_calls = None
                response.outcome = LLMResponseOutcome(
                    status="incomplete",
                    reason="output_limit",
                )
            else:
                response.tool_calls = [self._tool_call()]
                response.outcome = LLMResponseOutcome(status="completed")
            response.usage = UsageData(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                model="gpt-4o-mini",
                provider="openai",
            )
            return response

    mock_llm = RecoveringCommitLLM()
    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        result = await post_tool_reasoning(state)

    assert mock_llm.non_stream_route_calls == 2
    assert mock_llm.choices[0].mode == "required"
    assert mock_llm.choices[1].mode == "required"
    assert mock_llm.tool_names[1] == ["ptr_commit"]
    assert result["facts"]["metadata"]["last_post_tool_action"] == "finalize"
    assert len(state.trace.usage_records) == 2
    assert sum(record["total_tokens"] for record in state.trace.usage_records) == 30


@pytest.mark.asyncio
async def test_narrative_is_optional_when_commit_is_valid() -> None:
    """A complete commit should supply an LLM-authored observation fallback."""
    state = _sample_state_for_phase3()

    class CommitOnlyLLM(Phase3MockLLM):
        async def chat_with_tools_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            tools: list[Any],
            tool_choice: Any = "auto",
            **kwargs: Any,
        ) -> "Phase3MockLLM.Response":
            self.non_stream_route_calls += 1
            response = self.Response(None)  # type: ignore[arg-type]
            response.tool_calls = [self._tool_call()]
            response.outcome = LLMResponseOutcome(status="completed")
            return response

    mock_llm = CommitOnlyLLM(
        decision_payload={
            "next_action": "finalize",
            "action_reasoning": "The completed scan resolved the requested check.",
        },
        observation_chunks=[],
    )
    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ):
        await post_tool_reasoning(state)

    assert mock_llm.non_stream_route_calls == 1
    assert state.trace.observations[-1] == (
        "The completed scan resolved the requested check."
    )


@pytest.mark.asyncio
async def test_truncated_stream_recovers_commit_without_restarting_observation() -> None:
    """A truncated route stream should preserve text and recover only control."""
    state = _sample_state_for_phase3()

    class TruncatedStreamLLM(Phase3MockLLM):
        async def stream_chat_with_tools_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            tools: list[Any],
            tool_choice: Any = "auto",
            **kwargs: Any,
        ) -> object:
            self.stream_route_calls += 1

            async def iterator():
                yield "The completed scan resolved the requested check."

            class StreamResponse:
                content_iterator = iterator()

                @staticmethod
                def get_final_usage() -> dict[str, Any]:
                    return {
                        "prompt_tokens": 10,
                        "completion_tokens": 6,
                        "total_tokens": 16,
                        "model": "gpt-4o-mini",
                        "provider": "openai",
                    }

                @staticmethod
                def get_final_tool_calls() -> None:
                    return None

                @staticmethod
                def get_final_outcome() -> LLMResponseOutcome:
                    return LLMResponseOutcome(
                        status="incomplete",
                        reason="output_limit",
                    )

            return StreamResponse()

    mock_llm = TruncatedStreamLLM(
        decision_payload={
            "next_action": "finalize",
            "action_reasoning": "The completed scan resolved the requested check.",
        },
        observation_chunks=[],
    )
    events: list[object] = []
    with patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
        return_value=mock_llm,
    ), patch(
        "agent.graph.nodes.post_tool_reasoning.node.resolve_turn_sequence",
        return_value=1,
    ):
        result = await post_tool_reasoning(state, writer=events.append)

    assert mock_llm.stream_route_calls == 1
    assert mock_llm.non_stream_route_calls == 1
    assert result["facts"]["metadata"]["last_post_tool_action"] == "finalize"
    observation_starts = [
        event for event in events if event.get("type") == "observation_start"
    ]
    assert len(observation_starts) == 1
