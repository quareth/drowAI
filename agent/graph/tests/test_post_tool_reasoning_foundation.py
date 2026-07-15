"""Tests for post_tool_reasoning: Foundation components.

Tests cover:
- PostToolReasoningOutput Pydantic model validation
- Conversation history builder
- PostToolReasoningPromptBuilder"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from agent.graph.nodes.post_tool_reasoning import (
    MAX_HISTORY_ENTRIES,
    PostToolReasoningError,
    PostToolReasoningOutput,
    VALID_POST_TOOL_ACTIONS,
    build_conversation_history_from_state,
    _build_conversation_history,
    _truncate_content,
)
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder, SYSTEM_PROMPT
from agent.graph.state import FactsState, InteractiveState, TraceState


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------


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
        plan=["Run host discovery", "Port scan live hosts", "Service enumeration"],
        todo_list=["Discover live hosts", "Scan for open ports"],
        metadata={
            "last_tool_result": {
                "parameters": {"target": "192.168.1.0/24"},
                "stdout_excerpt": "Host: 192.168.1.1 is up\nHost: 192.168.1.5 is up",
            },
        },
    )
    trace = TraceState(
        reasoning=["Decision: call_tool - Starting host discovery"],
        observations=["Found 2 hosts responding to ping sweep"],
    )
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def sample_synthesized_output() -> dict:
    """Create a sample synthesized output for testing."""
    return {
        "tool": "nmap",
        "summary": "Ping sweep completed successfully. Found 2 live hosts.",
        "key_findings": [
            "192.168.1.1 is up (gateway)",
            "192.168.1.5 is up (potential target)",
        ],
        "vulnerabilities": [],
        "next_actions": [
            "Run detailed port scan on discovered hosts",
            "Enumerate services on open ports",
        ],
        "stdout_excerpt": "Host: 192.168.1.1 is up\nHost: 192.168.1.5 is up",
    }


# -----------------------------------------------------------------------------
# Tests: PostToolReasoningOutput Model
# -----------------------------------------------------------------------------


class TestPostToolReasoningOutput:
    """Tests for PostToolReasoningOutput Pydantic model."""
    
    def test_valid_output_parses(self):
        """Valid JSON should parse to PostToolReasoningOutput."""
        data = {
            "observation": (
                "I found two live hosts on the network: 192.168.1.1 and 192.168.1.5. "
                "The first appears to be the gateway, while the second is likely our target. "
                "This confirms the network is active and reachable. "
                "I'll run a port scan on the discovered hosts to identify open services."
            ),
            "next_action": "call_tool",
            "action_reasoning": "Need to discover open ports on the live hosts.",
        }
        
        output = PostToolReasoningOutput.model_validate(data)
        
        assert output.observation.startswith("I found two live hosts")
        assert output.next_action == "call_tool"
        assert "open ports" in output.action_reasoning
    
    def test_all_valid_actions_accepted(self):
        """All valid next_action values should be accepted."""
        for action in VALID_POST_TOOL_ACTIONS:
            data = {
                "observation": "This is a valid observation with enough content.",
                "next_action": action,
                "action_reasoning": "This is the reasoning.",
            }
            output = PostToolReasoningOutput.model_validate(data)
            assert output.next_action == action
    
    def test_invalid_action_rejected(self):
        """Invalid next_action value should raise ValidationError."""
        data = {
            "observation": "This is a valid observation with enough content.",
            "next_action": "invalid_action",
            "action_reasoning": "This is the reasoning.",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        error_str = str(exc_info.value)
        assert "next_action" in error_str
    
    def test_missing_observation_rejected(self):
        """Missing observation field should raise ValidationError."""
        data = {
            "next_action": "call_tool",
            "action_reasoning": "This is the reasoning.",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        assert "observation" in str(exc_info.value)
    
    def test_missing_next_action_rejected(self):
        """Missing next_action field should raise ValidationError."""
        data = {
            "observation": "This is a valid observation with enough content.",
            "action_reasoning": "This is the reasoning.",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        assert "next_action" in str(exc_info.value)
    
    def test_missing_action_reasoning_rejected(self):
        """Missing action_reasoning field should raise ValidationError."""
        data = {
            "observation": "This is a valid observation with enough content.",
            "next_action": "call_tool",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        assert "action_reasoning" in str(exc_info.value)
    
    def test_short_observation_rejected(self):
        """Observation shorter than min_length should be rejected."""
        data = {
            "observation": "Too short",  # Less than 10 chars
            "next_action": "call_tool",
            "action_reasoning": "This is the reasoning.",
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        assert "observation" in str(exc_info.value).lower()
    
    def test_short_action_reasoning_rejected(self):
        """action_reasoning shorter than min_length should be rejected."""
        data = {
            "observation": "This is a valid observation with enough content.",
            "next_action": "call_tool",
            "action_reasoning": "Hi",  # Less than 5 chars
        }
        
        with pytest.raises(ValidationError) as exc_info:
            PostToolReasoningOutput.model_validate(data)
        
        assert "action_reasoning" in str(exc_info.value).lower()
    
    def test_extra_fields_ignored(self):
        """Extra fields should be ignored (not cause errors)."""
        data = {
            "observation": "This is a valid observation with enough content.",
            "next_action": "call_tool",
            "action_reasoning": "This is the reasoning.",
            "extra_field": "should be ignored",
            "another_extra": 123,
        }
        
        output = PostToolReasoningOutput.model_validate(data)
        
        assert output.next_action == "call_tool"
        assert not hasattr(output, "extra_field")


class TestPostToolReasoningError:
    """Tests for PostToolReasoningError exception."""
    
    def test_exception_with_message(self):
        """Exception should preserve message."""
        error = PostToolReasoningError("LLM call failed: timeout")
        assert str(error) == "LLM call failed: timeout"
    
    def test_exception_is_exception_subclass(self):
        """Exception should be subclass of Exception."""
        assert issubclass(PostToolReasoningError, Exception)
    
    def test_exception_can_be_raised_and_caught(self):
        """Exception should be raisable and catchable."""
        with pytest.raises(PostToolReasoningError) as exc_info:
            raise PostToolReasoningError("Test error")
        
        assert "Test error" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Tests: Conversation History Builder
# -----------------------------------------------------------------------------


class TestConversationHistoryBuilder:
    """Tests for conversation history building functions."""
    
    def test_history_builder_returns_marker_without_ledger(self):
        """Without current-turn ledger records, builder returns marker."""
        result = _build_conversation_history(
            trace_observations=None,
            trace_reasoning=None,
        )
        
        assert "No prior context" in result
        assert "first reasoning iteration" in result
    
    def test_history_builder_returns_marker_with_ledger(self):
        """With ledger records present, builder still returns marker."""
        state = InteractiveState(
            facts=FactsState(
                task_id=123,
                message="Scan network",
                metadata={"working_memory": {"current_turn_phases": []}},
            ),
            trace=TraceState(),
        )
        state.facts.metadata["turn_sequence"] = 1
        state.facts.metadata["working_memory"]["current_turn_phases"] = [
            {
                "turn_sequence": 1,
                "phase_sequence": 0,
                "source": "ptr",
                "kind": "reasoning_step",
                "summary": "PTR summary",
            }
        ]
        result = _build_conversation_history(
            trace_observations=[],
            trace_reasoning=[],
            metadata=state.facts.metadata,
            turn_sequence=1,
        )
        
        assert "No prior context" in result
    
    def test_build_from_state(self, sample_interactive_state):
        """build_conversation_history_from_state should work with InteractiveState."""
        result = build_conversation_history_from_state(sample_interactive_state)
        
        assert "No prior context" in result


class TestTruncateContent:
    """Tests for _truncate_content helper."""
    
    def test_short_content_unchanged(self):
        """Content under limit should be unchanged."""
        content = "Short content"
        result = _truncate_content(content, max_chars=100)
        assert result == content
    
    def test_long_content_truncated(self):
        """Content over limit should be truncated with ellipsis."""
        content = "A" * 200
        result = _truncate_content(content, max_chars=50)
        
        assert len(result) == 50
        assert result.endswith("…")
    
    def test_strips_whitespace(self):
        """Should strip leading/trailing whitespace."""
        content = "  content with spaces  "
        result = _truncate_content(content, max_chars=100)
        assert result == "content with spaces"


# -----------------------------------------------------------------------------
# Tests: PostToolReasoningPromptBuilder
# -----------------------------------------------------------------------------


class TestPromptBuilder:
    """Tests for PostToolReasoningPromptBuilder."""
    
    def test_system_prompt_contains_required_sections(self):
        """System prompt should include role, output format, constraints."""
        builder = PostToolReasoningPromptBuilder()
        system_prompt = builder.build_system_prompt()
        
        # Should define role
        assert "pentesting agent" in system_prompt.lower()
        
        # Should define output format
        assert "JSON" in system_prompt
        assert "observation" in system_prompt
        assert "next_action" in system_prompt
        assert "action_reasoning" in system_prompt
        
        # Should define valid actions
        assert "call_tool" in system_prompt
        assert "think_more" in system_prompt
        assert "reflect" in system_prompt
        assert "finalize" in system_prompt
    
    def test_system_prompt_is_constant(self):
        """SYSTEM_PROMPT constant should match build_system_prompt()."""
        builder = PostToolReasoningPromptBuilder()
        assert builder.build_system_prompt() == SYSTEM_PROMPT
    
    def test_user_prompt_includes_verbatim_user_input(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """User prompt must surface the verbatim user message under ``## User Input``."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )

        assert "Scan the network to find online hosts" in prompt
        assert "## User Input" in prompt
        # No classifier brief is attached to the fixture, so the optional
        # ``## User Goal`` section must NOT be rendered (fixing the bug
        # where the verbatim message was dressed up as a goal).
        assert "## User Goal" not in prompt

    def test_user_prompt_renders_classifier_derived_user_goal(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """When the classifier brief is present, ``## User Goal`` mirrors it."""
        sample_interactive_state.facts.metadata["working_memory"] = {
            "intent_brief": {
                "resolved_user_intent": "Map live hosts in the lab subnet",
                "overall_goal": "Build an attack surface inventory",
            }
        }

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )

        assert "## User Input" in prompt
        assert "Scan the network to find online hosts" in prompt
        assert "## User Goal" in prompt
        assert "Map live hosts in the lab subnet" in prompt

    def test_user_prompt_prefers_original_goal_for_user_goal(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """``## User Goal`` uses the stable original goal when present."""
        sample_interactive_state.facts.metadata["working_memory"] = {
            "intent_brief": {
                "original_goal": (
                    "Map the lab subnet, then scan one live host for PostgreSQL"
                ),
                "resolved_user_intent": "Map live hosts in the lab subnet",
                "overall_goal": "Build an attack surface inventory",
            }
        }

        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )

        assert prompt.count("## User Goal") == 1
        assert "Map the lab subnet, then scan one live host for PostgreSQL" in prompt
        assert "Map live hosts in the lab subnet" not in prompt
    
    def test_user_prompt_includes_tool_output(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """User prompt should include synthesized tool output."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )
        
        # Tool name
        assert "nmap" in prompt
        
        # Summary
        assert "Ping sweep completed" in prompt or "live hosts" in prompt.lower()
        
        # Key findings
        assert "192.168.1.1" in prompt or "gateway" in prompt.lower()
    
    def test_user_prompt_rejects_conversation_history(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """PTR decision prompts expose no transcript parameter."""
        builder = PostToolReasoningPromptBuilder()
        signature = inspect.signature(builder.build_user_prompt)

        assert "conversation_history" not in signature.parameters
    
    def test_user_prompt_includes_plan(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """User prompt should include current plan."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )
        
        assert "Current Plan" in prompt
        assert "host discovery" in prompt.lower() or "Run host discovery" in prompt
    
    def test_user_prompt_includes_todos(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """User prompt should include todo list."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )
        
        assert "Todo List" in prompt
        assert "Discover live hosts" in prompt or "live hosts" in prompt.lower()
    
    def test_user_prompt_includes_task_instruction(
        self, sample_interactive_state, sample_synthesized_output
    ):
        """User prompt should include final task instruction."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            sample_synthesized_output,
        )
        
        assert "Your Task" in prompt
        assert "Prior Current-Turn Phase Memory" in prompt
    
    def test_user_prompt_handles_empty_synthesized(self, sample_interactive_state):
        """User prompt should handle empty synthesized output gracefully."""
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            sample_interactive_state,
            synthesized={},  # Empty
        )

        # Should still include the verbatim user input and task instruction.
        assert "## User Input" in prompt
        assert "Your Task" in prompt
    
    def test_user_prompt_skips_empty_sections(self, sample_interactive_state):
        """User prompt should skip sections with no content."""
        # Create state with minimal content
        facts = FactsState(
            task_id=123,
            message="Test message",
        )
        state = InteractiveState(facts=facts)
        
        builder = PostToolReasoningPromptBuilder()
        prompt = builder.build_user_prompt(
            state,
            synthesized={},
        )
        
        # These sections should not appear
        assert "Current Plan" not in prompt
        assert "Todo List" not in prompt
        assert "Scope Hints" not in prompt
    
    def test_format_parameters(self):
        """_format_parameters should format dict as key=value pairs."""
        builder = PostToolReasoningPromptBuilder()
        
        params = {"target": "192.168.1.1", "ports": "1-1000", "empty": None}
        result = builder._format_parameters(params)
        
        assert "target=192.168.1.1" in result
        assert "ports=1-1000" in result
        assert "empty" not in result  # None values should be excluded
    
    def test_format_sequence(self):
        """_format_sequence should format as bulleted list."""
        builder = PostToolReasoningPromptBuilder()
        
        items = ["First item", "Second item", "Third item"]
        result = builder._format_sequence(items)
        
        assert "• First item" in result
        assert "• Second item" in result
        assert "• Third item" in result
    
    def test_format_todos_with_strings(self):
        """_format_todos should handle string todo items."""
        builder = PostToolReasoningPromptBuilder()
        
        todos = ["First todo", "Second todo"]
        result = builder._format_todos(todos)
        
        assert "☐ First todo" in result
        assert "☐ Second todo" in result


# -----------------------------------------------------------------------------
# Tests: Constants
# -----------------------------------------------------------------------------


class TestConstants:
    """Tests for module constants."""
    
    def test_valid_actions_is_frozenset(self):
        """VALID_POST_TOOL_ACTIONS should be a frozenset."""
        assert isinstance(VALID_POST_TOOL_ACTIONS, frozenset)
    
    def test_valid_actions_contains_expected_values(self):
        """VALID_POST_TOOL_ACTIONS should contain all expected actions."""
        expected = {"call_tool", "think_more", "reflect", "finalize"}
        assert VALID_POST_TOOL_ACTIONS == expected
    
    def test_max_history_entries_is_positive(self):
        """MAX_HISTORY_ENTRIES should be a positive integer."""
        assert isinstance(MAX_HISTORY_ENTRIES, int)
        assert MAX_HISTORY_ENTRIES > 0
