"""Tests for post_tool_reasoning: Core LLM Integration.

Tests cover:
- Response parser (_parse_reasoning_response)
- Decision recorder (_record_decision)
- Observation recorder (_record_observation)
- Main node function (post_tool_reasoning)"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from agent.providers.llm.core.base import LLMResponse
from agent.graph.nodes.post_tool_reasoning import (
    PostToolReasoningError,
    PostToolReasoningOutput,
    VALID_POST_TOOL_ACTIONS,
    _extract_json_from_text,
    _parse_reasoning_response,
    _record_decision,
    _record_observation,
    _split_observation_and_decision,
    post_tool_reasoning,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.providers.llm.core.exceptions import LLMConfigurationError
from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def valid_delimiter_response() -> str:
    """Create a valid decision-only JSON response."""
    return (
        '{"next_action": "call_tool", '
        '"action_reasoning": "Need to discover open ports and services on the target host.", '
        '"tool_intent": {"description": "Run port scan", "target": "192.168.1.5", "focus": "open services"}}'
    )


@pytest.fixture
def valid_json_response() -> str:
    """Create a valid JSON response that includes an explicit observation field."""
    return json.dumps({
        "observation": (
            "I found two live hosts on the network: 192.168.1.1 and 192.168.1.5. "
            "The first appears to be the gateway based on its IP, while the second "
            "is likely our target machine. This confirms the network segment is active. "
            "I'll proceed with a port scan on 192.168.1.5 to identify open services."
        ),
        "next_action": "call_tool",
        "action_reasoning": "Need to discover open ports and services on the target host.",
        "tool_intent": {
            "description": "Run port scan",
            "target": "192.168.1.5",
            "focus": "open services",
        },
    })


@pytest.fixture
def valid_markdown_response() -> str:
    """Create a valid response with markdown-wrapped decision JSON."""
    return """
```json
{"next_action": "call_tool", "action_reasoning": "Web server detected - will run directory enumeration.", "tool_intent": {"description": "Run directory enumeration", "target": "target-web", "focus": "web content"}}
```
"""


@pytest.fixture
def sample_interactive_state() -> InteractiveState:
    """Create a sample InteractiveState for testing."""
    facts = FactsState(
        task_id=123,
        message="Scan the network to find online hosts",
        conversation_id="conv-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        tool_parameters={"target": "192.168.1.0/24"},
        current_goal="Discover live hosts on the network",
        plan=["Run host discovery", "Port scan live hosts"],
        todo_list=["Discover live hosts", "Scan for open ports"],
        iterations=2,
        metadata={
            "api_key": "test-api-key",
            "model": "gpt-4o-mini",
            "synthesized_output": {
                "tool": "nmap",
                "summary": "Found 2 live hosts",
                "key_findings": ["192.168.1.1 is up", "192.168.1.5 is up"],
            },
        },
        decision_history=["call_tool: Starting host discovery"],
    )
    trace = TraceState(
        reasoning=["Decision: call_tool - Starting host discovery"],
        observations=["Found 2 hosts responding to ping sweep"],
        decision_log=[],
    )
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def mock_llm_client():
    """Create a mock LLMClient that returns decision-only JSON."""
    client = AsyncMock()
    class Response:
        """Minimal chat_with_usage payload shape."""
        
        def __init__(self, content: str):
            self.content = content
            self.structured_output = None
            self.usage = None

    # Return decision-only JSON
    client.chat = AsyncMock(return_value=(
        '{"next_action": "call_tool", "action_reasoning": "Need more data to complete the analysis.", '
        '"tool_intent": {"description": "Collect follow-up evidence", "target": "target-1", "focus": "service details"}}'
    ))

    async def chat_with_usage(*args: Any, **kwargs: Any) -> Response:
        body = await client.chat()
        return Response(body)

    client.chat_with_usage = AsyncMock(side_effect=chat_with_usage)

    return client


# -----------------------------------------------------------------------------
# Tests: Observation/Decision Splitting
# -----------------------------------------------------------------------------


class TestSplitObservationAndDecision:
    """Tests for _split_observation_and_decision."""
    
    def test_decision_json_splits_correctly(self):
        """Decision-only JSON should split correctly."""
        response = '{"next_action": "call_tool", "action_reasoning": "Need more data."}'
        obs, decision = _split_observation_and_decision(response)
        assert "Decision: call_tool" in obs
        assert '"next_action"' in decision
    
    def test_markdown_wrapped_json(self):
        """Markdown-wrapped decision JSON should parse."""
        response = """```json
{"next_action": "finalize", "action_reasoning": "All done."}
```"""
        obs, decision = _split_observation_and_decision(response)
        assert "Decision: finalize" in obs
        parsed = json.loads(decision)
        assert parsed["next_action"] == "finalize"
    
    def test_json_with_observation_field_uses_current_contract(self):
        """Observation field no longer drives parsing; decision fields do."""
        legacy_response = json.dumps({
            "observation": "Legacy observation text",
            "next_action": "reflect",
            "action_reasoning": "reason"
        })
        obs, decision = _split_observation_and_decision(legacy_response)
        assert "Decision: reflect" in obs
    
    def test_fallback_to_decision_only_json(self):
        """Pure JSON without observation should still split using synthetic observation."""
        decision_only_response = json.dumps({
            "next_action": "finalize",
            "action_reasoning": "All findings are sufficient to answer the user.",
        })
        obs, decision = _split_observation_and_decision(decision_only_response)
        assert "Decision: finalize" in obs
        assert "Reasoning: All findings are sufficient to answer the user." in obs
        parsed = json.loads(decision)
        assert parsed["next_action"] == "finalize"
    
    def test_non_json_response_raises_error(self):
        """Response without valid decision JSON should raise error."""
        response = "This is just plain text without JSON."
        with pytest.raises(PostToolReasoningError) as exc_info:
            _split_observation_and_decision(response)
        assert "no json object found" in str(exc_info.value).lower()

    def test_truncated_json_returns_partial_decision_for_recovery(self):
        """Truncated JSON should keep partial payload for downstream recovery."""
        response = '{"next_action":"finalize","action_reasoning":"unfinished'
        obs, decision = _split_observation_and_decision(response)
        assert obs == ""
        assert decision == response


class TestExtractJsonFromText:
    """Tests for _extract_json_from_text helper."""
    
    def test_pure_json_extracted(self):
        """Pure JSON should be returned as-is."""
        json_str = '{"next_action": "call_tool", "action_reasoning": "reason"}'
        result = _extract_json_from_text(json_str)
        assert result == json_str
    
    def test_markdown_code_block_extracted(self):
        """JSON in markdown code block should be extracted."""
        text = """
```json
{"next_action": "finalize", "action_reasoning": "done"}
```
"""
        result = _extract_json_from_text(text)
        parsed = json.loads(result)
        assert parsed["next_action"] == "finalize"
    
    def test_embedded_json_extracted(self):
        """JSON embedded in text should be extracted."""
        text = 'Some text {"next_action": "think_more"} more text'
        result = _extract_json_from_text(text)
        parsed = json.loads(result)
        assert parsed["next_action"] == "think_more"
    
    def test_no_json_raises_error(self):
        """Text without JSON should raise PostToolReasoningError."""
        text = "This is just plain text."
        with pytest.raises(PostToolReasoningError) as exc_info:
            _extract_json_from_text(text)
        assert "No JSON object found" in str(exc_info.value)

    def test_truncated_json_raises_unbalanced_braces_error(self):
        """Truncated JSON should preserve unbalanced-braces signal."""
        text = '{"next_action":"finalize","action_reasoning":"unfinished'
        with pytest.raises(PostToolReasoningError) as exc_info:
            _extract_json_from_text(text)
        assert "Unbalanced braces" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Tests: Response Parser
# -----------------------------------------------------------------------------


class TestParseReasoningResponse:
    """Tests for _parse_reasoning_response."""
    
    def test_decision_json_parses(self, valid_delimiter_response):
        """Decision-only JSON response should parse correctly."""
        output = _parse_reasoning_response(valid_delimiter_response)
        
        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "call_tool"
        assert "Decision: call_tool" in output.observation
        assert "port" in output.action_reasoning.lower()
    
    def test_legacy_json_parses(self, valid_json_response):
        """Legacy pure JSON response should still parse (backwards compatibility)."""
        output = _parse_reasoning_response(valid_json_response)
        
        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "call_tool"
        assert "192.168.1.1" in output.observation
    
    def test_decision_only_json_parses(self):
        """Decision-only JSON response should parse with synthetic observation."""
        response = json.dumps({
            "next_action": "reflect",
            "action_reasoning": "Tool output suggests prior assumptions are incomplete.",
        })
        output = _parse_reasoning_response(response)
        
        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "reflect"
        assert output.action_reasoning == "Tool output suggests prior assumptions are incomplete."
        assert output.observation.startswith("Decision: reflect")
    
    def test_markdown_wrapped_json_parses(self, valid_markdown_response):
        """JSON in markdown code block should parse."""
        output = _parse_reasoning_response(valid_markdown_response)
        
        assert output.next_action == "call_tool"
        assert "port" in output.observation.lower() or "web" in output.observation.lower()
    
    def test_all_actions_parse(self):
        """All valid action types should parse."""
        for action in VALID_POST_TOOL_ACTIONS:
            decision_payload: Dict[str, Any] = {
                "next_action": action,
                "action_reasoning": "This is the reasoning.",
            }
            if action == "call_tool":
                decision_payload["tool_intent"] = {
                    "description": "Collect additional evidence",
                    "target": "target-1",
                    "focus": "validation",
                }
            response = json.dumps(decision_payload)
            output = _parse_reasoning_response(response)
            assert output.next_action == action
    
    def test_empty_response_raises_error(self):
        """Empty response should raise PostToolReasoningError."""
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response("")
        assert "Empty response" in str(exc_info.value)
    
    def test_whitespace_only_raises_error(self):
        """Whitespace-only response should raise PostToolReasoningError."""
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response("   \n\t  ")
        assert "Empty response" in str(exc_info.value)
    
    def test_invalid_json_in_decision_raises_error(self):
        """Invalid decision JSON should raise PostToolReasoningError."""
        response = "{next_action: call_tool}"  # Missing quotes
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response(response)
        assert "Invalid JSON" in str(exc_info.value) or "No JSON" in str(exc_info.value)

    def test_truncated_decision_json_recovers_required_fields(self):
        """Truncated decision JSON should recover when required fields are clear."""
        response = (
            '{"next_action":"finalize","action_reasoning":"The scan found an online host and '
            "conclusively reported TCP port 5432 as closed (connection refused), satisfying the user request and"
        )
        output = _parse_reasoning_response(response)
        assert output.next_action == "finalize"
        assert "scan found an online host" in output.action_reasoning.lower()

    def test_truncated_decision_without_reasoning_still_raises(self):
        """Truncated JSON without recoverable reasoning should still fail."""
        response = '{"next_action":"finalize","action_reasoning":'
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response(response)
        assert "Invalid JSON" in str(exc_info.value)
    
    def test_missing_next_action_raises_error(self):
        """Missing next_action field should raise PostToolReasoningError."""
        response = json.dumps({
            "action_reasoning": "reason",
        })
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response(response)
        assert "missing required 'next_action'" in str(exc_info.value).lower()
    
    def test_invalid_action_raises_error(self):
        """Invalid next_action value should raise PostToolReasoningError."""
        response = json.dumps({
            "next_action": "invalid_action",
            "action_reasoning": "reason",
        })
        with pytest.raises(PostToolReasoningError) as exc_info:
            _parse_reasoning_response(response)
        # Could fail at Pydantic validation or our additional check
        error_str = str(exc_info.value).lower()
        assert "invalid" in error_str or "validation" in error_str
    
    def test_uses_observation_field_when_provided(self):
        """Explicit observation field should be preserved when provided."""
        response = (
            '{"observation": "JSON embedded observation", '
            '"next_action": "finalize", "action_reasoning": "Task completed successfully"}'
        )
        output = _parse_reasoning_response(response)
        assert "JSON embedded observation" in output.observation


# -----------------------------------------------------------------------------
# Tests: Decision Recorder
# -----------------------------------------------------------------------------


class TestRecordDecision:
    """Tests for _record_decision."""
    
    def test_appends_to_decision_history(self, sample_interactive_state):
        """Should append formatted entry to decision_history."""
        output = PostToolReasoningOutput(
            observation="Found interesting data in the scan results.",
            next_action="call_tool",
            action_reasoning="Need to gather more information.",
        )
        
        initial_count = len(sample_interactive_state.facts.decision_history)
        _record_decision(sample_interactive_state, output)
        
        assert len(sample_interactive_state.facts.decision_history) == initial_count + 1
        last_entry = sample_interactive_state.facts.decision_history[-1]
        assert "call_tool" in last_entry
        assert "gather more" in last_entry.lower() or "information" in last_entry.lower()

    def test_normalizes_none_decision_history(self, sample_interactive_state):
        """Decision recorder should use the canonical decision-history ensure helper."""
        sample_interactive_state.facts.decision_history = None  # type: ignore[assignment]
        output = PostToolReasoningOutput(
            observation="Found interesting data in the scan results.",
            next_action="call_tool",
            action_reasoning="Need to gather more information.",
        )

        _record_decision(sample_interactive_state, output)

        assert sample_interactive_state.facts.decision_history == [
            "call_tool: Need to gather more information."
        ]
    
    def test_appends_to_decision_log(self, sample_interactive_state):
        """Should append structured record to decision_log."""
        output = PostToolReasoningOutput(
            observation="Analysis complete.",
            next_action="finalize",
            action_reasoning="All goals achieved.",
        )
        
        initial_count = len(sample_interactive_state.trace.decision_log)
        _record_decision(sample_interactive_state, output)
        
        assert len(sample_interactive_state.trace.decision_log) == initial_count + 1
        last_record = sample_interactive_state.trace.decision_log[-1]
        assert last_record["action"] == "finalize"
        assert last_record["reasoning"] == "All goals achieved."
        assert last_record["source"] == "post_tool_reasoning"
    
    def test_appends_to_reasoning_trace(self, sample_interactive_state):
        """Should append visibility entry to reasoning trace."""
        output = PostToolReasoningOutput(
            observation="Need to reflect on approach.",
            next_action="reflect",
            action_reasoning="Current strategy not working.",
        )
        
        initial_count = len(sample_interactive_state.trace.reasoning)
        _record_decision(sample_interactive_state, output)
        
        assert len(sample_interactive_state.trace.reasoning) == initial_count + 1
        last_entry = sample_interactive_state.trace.reasoning[-1]
        assert "[POST_TOOL_REASONING]" in last_entry
        assert "reflect" in last_entry
    
    def test_updates_stuck_counter_same_action(self, sample_interactive_state):
        """Should increment stuck_counter when same action repeated."""
        # First call_tool was already in decision_history
        output = PostToolReasoningOutput(
            observation="Continuing with tool execution.",
            next_action="call_tool",  # Same as last action
            action_reasoning="More data needed.",
        )
        
        _record_decision(sample_interactive_state, output)
        
        # Stuck counter should have been incremented
        assert sample_interactive_state.facts.stuck_counter >= 1
    
    def test_resets_stuck_counter_different_action(self, sample_interactive_state):
        """Should reset stuck_counter when different action taken."""
        output = PostToolReasoningOutput(
            observation="Switching to finalize.",
            next_action="finalize",  # Different from last action (call_tool)
            action_reasoning="Enough data gathered.",
        )
        
        _record_decision(sample_interactive_state, output)
        
        # Stuck counter should be reset to 0
        assert sample_interactive_state.facts.stuck_counter == 0


# -----------------------------------------------------------------------------
# Tests: Observation Recorder
# -----------------------------------------------------------------------------


class TestRecordObservation:
    """Tests for _record_observation."""
    
    def test_appends_to_trace_observations(self, sample_interactive_state):
        """Should append observation to trace.observations."""
        output = PostToolReasoningOutput(
            observation="I found critical vulnerabilities in the target system.",
            next_action="call_tool",
            action_reasoning="Will exploit the vulnerabilities.",
        )
        
        initial_count = len(sample_interactive_state.trace.observations)
        _record_observation(sample_interactive_state, output)
        
        assert len(sample_interactive_state.trace.observations) == initial_count + 1
        assert "critical vulnerabilities" in sample_interactive_state.trace.observations[-1]
    
    def test_updates_synthesized_output(self, sample_interactive_state):
        """Should update synthesized_output with observation_text."""
        output = PostToolReasoningOutput(
            observation="The scan completed successfully with useful findings.",
            next_action="finalize",
            action_reasoning="Task complete.",
        )
        
        _record_observation(sample_interactive_state, output)
        
        synthesized = sample_interactive_state.facts.metadata.get("synthesized_output", {})
        assert synthesized.get("observation_text") == output.observation
    
    def test_appends_to_trace_observations_only(self, sample_interactive_state):
        """Should persist observation on trace without writing metadata history."""
        output = PostToolReasoningOutput(
            observation="Found open port 22 for SSH access.",
            next_action="call_tool",
            action_reasoning="Will attempt SSH enumeration.",
        )
        
        _record_observation(sample_interactive_state, output)
        
        assert "history" not in (sample_interactive_state.facts.metadata or {})
        assert sample_interactive_state.trace.observations[-1] == output.observation
    
    def test_handles_empty_metadata(self):
        """Should handle state with empty metadata."""
        facts = FactsState(
            task_id=123,
            message="Test",
            metadata={},  # Empty metadata (default)
        )
        state = InteractiveState(facts=facts)
        
        output = PostToolReasoningOutput(
            observation="Test observation content here.",
            next_action="finalize",
            action_reasoning="Done.",
        )
        
        _record_observation(state, output)
        
        # Should have created synthesized_output in metadata
        assert state.facts.metadata is not None
        assert "observation content" in state.trace.observations[-1]
        assert "synthesized_output" in state.facts.metadata
        assert state.facts.metadata["synthesized_output"]["observation_text"] == output.observation

    def test_handles_none_metadata(self):
        """Observation recorder should use the canonical metadata ensure helper."""
        facts = FactsState(
            task_id=123,
            message="Test",
        )
        facts.metadata = None  # type: ignore[assignment]
        state = InteractiveState(facts=facts)

        output = PostToolReasoningOutput(
            observation="Test observation content here.",
            next_action="finalize",
            action_reasoning="Done.",
        )

        _record_observation(state, output)

        assert state.facts.metadata is not None
        assert state.facts.metadata["synthesized_output"]["observation_text"] == output.observation


# -----------------------------------------------------------------------------
# Tests: Main Node Function
# -----------------------------------------------------------------------------


class TestPostToolReasoning:
    """Tests for post_tool_reasoning main node function."""
    
    @pytest.mark.asyncio
    async def test_successful_reasoning_flow(
        self, sample_interactive_state, mock_llm_client
    ):
        """Full flow with mocked LLM should succeed."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state()
            )
        
        # Should return valid state update
        assert "facts" in result
        assert "trace" in result
        
        # Should have recorded observation
        trace = result["trace"]
        assert len(trace["observations"]) > 1  # Original + new
        
        # Should have recorded decision
        facts = result["facts"]
        decision_history = facts.get("decision_history", [])
        assert len(decision_history) > 1  # Original + new
    
    @pytest.mark.asyncio
    async def test_skips_non_deep_reasoning(self):
        """Should skip for non-deep_reasoning capability."""
        facts = FactsState(
            task_id=123,
            message="Test",
            capability="simple_tool",  # Not deep_reasoning
            metadata={"synthesized_output": {"tool": "nmap"}},
        )
        state = InteractiveState(facts=facts)
        
        result = await post_tool_reasoning(state.as_graph_state())
        
        # Should return state without changes
        assert result["facts"]["capability"] == "simple_tool"

    @pytest.mark.asyncio
    async def test_deep_reasoning_capability_uses_dr_streaming_adapter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Effective deep-reasoning capability must select DR PTR policy."""
        captured = {}
        facts = FactsState(
            task_id=123,
            message="Test",
            capability="deep_reasoning",
            metadata={},
        )
        state = InteractiveState(facts=facts)

        def _capture_create(capability: str):
            captured["capability"] = capability
            raise ValueError("stop before LLM setup")

        monkeypatch.setattr(
            post_tool_reasoning.__globals__["StreamingAdapterFactory"],
            "create",
            _capture_create,
        )

        result = await post_tool_reasoning(state.as_graph_state())

        assert captured["capability"] == "deep_reasoning"
        assert result["facts"]["capability"] == "deep_reasoning"
    
    @pytest.mark.asyncio
    async def test_handles_missing_synthesized_output(self):
        """Should handle missing synthesized_output gracefully."""
        facts = FactsState(
            task_id=123,
            message="Test",
            capability="deep_reasoning",
            metadata={},  # No synthesized_output
        )
        state = InteractiveState(facts=facts)
        
        result = await post_tool_reasoning(state.as_graph_state())
        
        # Should log warning and return
        trace = result["trace"]
        reasoning = trace.get("reasoning", [])
        assert any("Missing synthesized_output" in r for r in reasoning)
    
    @pytest.mark.asyncio
    async def test_raises_on_missing_api_key(self, sample_interactive_state):
        """Should raise LLMConfigurationError when no API key available."""
        # Remove API key from metadata
        sample_interactive_state.facts.metadata.pop("api_key", None)
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            side_effect=LLMConfigurationError("No API key", provider=None),
        ):
            with pytest.raises(LLMConfigurationError):
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )
    
    @pytest.mark.asyncio
    async def test_raises_on_llm_error(
        self, sample_interactive_state, mock_llm_client
    ):
        """Should raise PostToolReasoningError when LLM call fails."""
        mock_llm_client.chat.side_effect = Exception("API timeout")
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            with pytest.raises(PostToolReasoningError) as exc_info:
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )
        
        assert "LLM call failed" in str(exc_info.value)
        assert "timeout" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_raises_on_invalid_response(
        self, sample_interactive_state, mock_llm_client
    ):
        """Should raise PostToolReasoningError when response is invalid."""
        mock_llm_client.chat.return_value = "This is not valid JSON at all"
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            with pytest.raises(PostToolReasoningError) as exc_info:
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )

        error_message = str(exc_info.value).lower()
        assert "parse" in error_message or "json" in error_message
    
    @pytest.mark.asyncio
    async def test_observation_added_to_trace(
        self, sample_interactive_state, mock_llm_client
    ):
        """Observation should be added to trace.observations."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state()
            )
        
        observations = result["trace"]["observations"]
        # Should have original + new observation
        assert len(observations) == 2
        assert observations[-1]
    
    @pytest.mark.asyncio
    async def test_decision_recorded_correctly(
        self, sample_interactive_state, mock_llm_client
    ):
        """Decision should be recorded in decision_history with correct format."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state()
            )
        
        decision_history = result["facts"]["decision_history"]
        last_decision = decision_history[-1]
        
        # Format should be "action: reasoning"
        assert ":" in last_decision
        action = last_decision.split(":")[0].strip()
        assert action == "call_tool"
    
    @pytest.mark.asyncio
    async def test_history_updated_for_next_iteration(
        self, sample_interactive_state, mock_llm_client
    ):
        """Phase 5: continuity is tracked without metadata['history'] writes."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state()
            )
        
        metadata = result["facts"]["metadata"]
        assert "history" not in metadata
        synthesized = metadata.get("synthesized_output", {})
        assert isinstance(synthesized, dict)
        assert synthesized.get("observation_text")
    
    @pytest.mark.asyncio
    async def test_marks_completion_in_metadata(
        self, sample_interactive_state, mock_llm_client
    ):
        """Should mark completion in metadata for downstream nodes."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state()
            )
        
        metadata = result["facts"]["metadata"]
        assert metadata.get("post_tool_reasoning_completed") is True
        assert metadata.get("last_post_tool_action") in {"call_tool", "reflect"}

    @pytest.mark.asyncio
    async def test_enqueues_ingestion_with_candidate_payload_from_decision(
        self,
        sample_interactive_state,
        monkeypatch,
    ) -> None:
        """Post-tool node should enqueue ingestion with candidate payload once decision resolves."""
        sample_interactive_state.facts.metadata["last_execution_id"] = "execution-123"
        sample_interactive_state.facts.metadata["last_tool_result_compact"] = {
            "summary": "Detected PostgreSQL 11.5",
            "artifact_refs": [{"artifact_id": "artifact-1", "label": "stdout"}],
        }
        sample_interactive_state.facts.selected_tool = "information_gathering.network_discovery.nmap"

        class _StructuredDecisionClient:
            async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any):
                if kwargs.get("structured_output") is not None:
                    return LLMResponse(
                        content="",
                        usage=UsageData(
                            prompt_tokens=12,
                            completion_tokens=6,
                            total_tokens=18,
                            model="claude-sonnet-4-6",
                            provider="anthropic",
                            api_surface="messages",
                            provider_usage_components=ProviderUsageComponents(
                                provider="anthropic",
                                api_surface="messages",
                                components={
                                    "input_tokens": 10,
                                    "cache_creation_input_tokens": 2,
                                    "cache_read_input_tokens": 0,
                                    "output_tokens": 6,
                                },
                            ),
                        ),
                        structured_output={
                            "next_action": "call_tool",
                            "action_reasoning": "Need a targeted follow-up check.",
                            "tool_intent": {
                                "description": "Run a focused version validation step",
                                "target": "10.0.0.8:5432",
                                "focus": "postgresql version verification",
                            },
                            "user_goal_achieved": False,
                            "todo_progress": [],
                            "effective_next_goal": None,
                            "failure_detected": False,
                            "failure_category": None,
                            "retry_suggested": False,
                            "candidate_observations": [
                                {
                                    "observation_type": "finding.vulnerability_detected",
                                    "subject_type": "finding.instance",
                                    "subject_key_hint": "cve-2024-0001:service.socket:10.0.0.8/tcp/5432",
                                    "assertion_level": "candidate",
                                    "confidence": 0.85,
                                    "attributes": [{"key": "version", "value": "11.5"}],
                                    "rationale": "Version appears in vulnerable range.",
                                    "evidence_refs": [
                                        {
                                            "source_artifact_id": "artifact-1",
                                            "excerpt": "PostgreSQL 11.5",
                                        }
                                    ],
                                    "vulnerability": {
                                        "id": "CVE-2024-0001",
                                        "title": "PostgreSQL version likely vulnerable",
                                        "severity": "high",
                                    },
                                    "vulnerability_confidence": 0.9,
                                }
                            ],
                        },
                    )
                return LLMResponse(
                    content=(
                        "I observed version evidence suggesting a likely vulnerable PostgreSQL build. "
                        "I will run a focused follow-up command to validate impact."
                    ),
                    usage=None,
                    structured_output=None,
                )

        captured: Dict[str, Any] = {}

        def _capture_enqueue(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(
            "agent.graph.subgraphs.tool_execution._enqueue_execution_ingestion",
            _capture_enqueue,
        )

        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=_StructuredDecisionClient(),
        ):
            await post_tool_reasoning(sample_interactive_state.as_graph_state())

        assert captured["task_id"] == sample_interactive_state.facts.task_id
        assert captured["execution_id"] == "execution-123"
        assert captured["tool_name"] == "information_gathering.network_discovery.nmap"
        assert captured["post_tool_candidate_payload"]["candidate_observations"][0]["observation_type"] == (
            "finding.vulnerability_detected"
        )
        candidate_row = captured["post_tool_candidate_payload"]["candidate_observations"][0]
        assert candidate_row["evidence_refs"] == [
            {
                "source_artifact_id": "artifact-1",
                "excerpt": "PostgreSQL 11.5",
            }
        ]
        assert "evidence_archive_id" not in candidate_row["evidence_refs"][0]
        usage_summary = captured["post_tool_candidate_usage"]
        assert usage_summary["input_tokens"] == 12
        assert usage_summary["output_tokens"] == 6
        assert usage_summary["total_tokens"] == 18
        assert usage_summary["estimated_cost_usd"] == 0.0
        assert usage_summary["pricing_status"] == "unavailable"
        assert usage_summary["provider"] == "anthropic"
        assert usage_summary["model"] == "claude-sonnet-4-6"
        assert usage_summary["api_surface"] == "messages"
        assert usage_summary["provider_usage_components"] == {
            "provider": "anthropic",
            "api_surface": "messages",
            "components": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 0,
                "output_tokens": 6,
            },
        }


# -----------------------------------------------------------------------------
# Tests: No Fallback Behavior
# -----------------------------------------------------------------------------


class TestNoFallbackBehavior:
    """Tests to verify no silent fallback occurs on errors."""
    
    @pytest.mark.asyncio
    async def test_no_fallback_on_parse_error(
        self, sample_interactive_state, mock_llm_client
    ):
        """Parse errors should propagate, not fall back to defaults."""
        mock_llm_client.chat.return_value = '{"invalid": "structure"}'
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            with pytest.raises(PostToolReasoningError):
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )
    
    @pytest.mark.asyncio
    async def test_no_fallback_on_validation_error(
        self, sample_interactive_state, mock_llm_client
    ):
        """Validation errors should propagate, not fall back to defaults."""
        # Missing required fields
        mock_llm_client.chat.return_value = json.dumps({
            "observation": "Short",  # Too short
            "next_action": "call_tool",
            "action_reasoning": "Hi",  # Too short
        })
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            with pytest.raises(PostToolReasoningError):
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )
    
    @pytest.mark.asyncio
    async def test_no_fallback_on_network_error(
        self, sample_interactive_state, mock_llm_client
    ):
        """Network errors should propagate, not fall back to defaults."""
        mock_llm_client.chat.side_effect = ConnectionError("Network unreachable")
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client,
        ):
            with pytest.raises(PostToolReasoningError) as exc_info:
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state()
                )
        
        assert "LLM call failed" in str(exc_info.value)
