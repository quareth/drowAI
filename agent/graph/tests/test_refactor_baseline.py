"""Baseline tests to verify refactoring doesn't change behavior.

These tests capture the current behavior of post_tool_reasoning and decision_router
to ensure the modularization refactoring doesn't introduce functional changes.

Created: December 28, 2024
Purpose: baseline validation for DR graph refactoring"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# POST TOOL REASONING BASELINE TESTS
# =============================================================================


class TestPostToolReasoningImports:
    """Verify all public exports are importable."""

    def test_main_function_importable(self):
        """post_tool_reasoning function is importable."""
        from agent.graph.nodes.post_tool_reasoning import post_tool_reasoning

        assert callable(post_tool_reasoning)

    def test_models_importable(self):
        """Pydantic models are importable."""
        from agent.graph.nodes.post_tool_reasoning import (
            PostToolReasoningError,
            PostToolReasoningOutput,
            TodoProgress,
            ToolIntent,
        )

        assert issubclass(PostToolReasoningError, Exception)
        assert hasattr(PostToolReasoningOutput, "model_validate")
        assert hasattr(TodoProgress, "model_validate")
        assert hasattr(ToolIntent, "model_validate")

    def test_constants_importable(self):
        """Constants are importable with correct types."""
        from agent.graph.nodes.post_tool_reasoning import (
            MAX_HISTORY_ENTRIES,
            MAX_OBSERVATION_TOKENS,
            MAX_TODOS_IN_PROMPT,
            STREAMING_STEP_NAME,
            VALID_POST_TOOL_ACTIONS,
            VALID_TODO_STATUSES,
        )

        assert isinstance(VALID_POST_TOOL_ACTIONS, frozenset)
        assert isinstance(VALID_TODO_STATUSES, frozenset)
        assert isinstance(MAX_HISTORY_ENTRIES, int)
        assert isinstance(MAX_OBSERVATION_TOKENS, int)
        assert isinstance(MAX_TODOS_IN_PROMPT, int)
        assert isinstance(STREAMING_STEP_NAME, str)

    def test_helper_functions_importable(self):
        """Helper functions are importable."""
        from agent.graph.nodes.post_tool_reasoning import (
            build_conversation_history_from_state,
            _apply_progress_updates,
            _build_progress_summary,
            _extract_json_from_text,
            _non_streaming_call,
            _parse_reasoning_response,
            _record_decision,
            _record_observation,
            _split_observation_and_decision,
            _stream_and_parse_response,
        )

        assert callable(build_conversation_history_from_state)
        assert callable(_parse_reasoning_response)
        assert callable(_record_decision)
        assert callable(_record_observation)
        assert callable(_split_observation_and_decision)
        assert callable(_extract_json_from_text)
        assert callable(_stream_and_parse_response)
        assert callable(_non_streaming_call)
        assert callable(_apply_progress_updates)
        assert callable(_build_progress_summary)


class TestPostToolReasoningConstants:
    """Verify constants have expected values."""

    def test_valid_actions(self):
        """VALID_POST_TOOL_ACTIONS contains exactly 4 actions."""
        from agent.graph.nodes.post_tool_reasoning import VALID_POST_TOOL_ACTIONS

        assert VALID_POST_TOOL_ACTIONS == frozenset(
            {"call_tool", "think_more", "reflect", "finalize"}
        )

    def test_valid_todo_statuses(self):
        """VALID_TODO_STATUSES contains expected statuses."""
        from agent.graph.nodes.post_tool_reasoning import VALID_TODO_STATUSES

        assert VALID_TODO_STATUSES == frozenset(
            {"pending", "in_progress", "completed", "skipped"}
        )

    def test_split_helper_available(self):
        """Split helper remains available for compatibility."""
        from agent.graph.nodes.post_tool_reasoning import split_observation_and_decision

        assert callable(split_observation_and_decision)


class TestPostToolReasoningOutputModel:
    """Verify PostToolReasoningOutput model behavior."""

    def test_minimal_valid_output(self):
        """Minimal valid output can be created."""
        from agent.graph.nodes.post_tool_reasoning import PostToolReasoningOutput

        output = PostToolReasoningOutput(
            observation="This is a test observation with enough length.",
            next_action="finalize",
            action_reasoning="Reasoning for the decision",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is False
        assert output.todo_progress == []
        assert output.tool_intent is None

    def test_full_output_with_tool_intent(self):
        """Full output with tool_intent can be created."""
        from agent.graph.nodes.post_tool_reasoning import (
            PostToolReasoningOutput,
            TodoProgress,
            ToolIntent,
        )

        output = PostToolReasoningOutput(
            observation="This is a test observation with enough length.",
            next_action="call_tool",
            action_reasoning="Need to scan more ports",
            tool_intent=ToolIntent(
                description="Run port scan",
                target="192.168.1.1",
                focus="open ports",
            ),
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(index=0, status="in_progress"),
            ],
            effective_next_goal="Complete port scan",
        )

        assert output.next_action == "call_tool"
        assert output.tool_intent is not None
        assert output.tool_intent.target == "192.168.1.1"
        assert len(output.todo_progress) == 1


class TestPostToolReasoningParsing:
    """Verify response parsing behavior."""

    def test_parse_decision_json_format(self):
        """Decision-only JSON format parses correctly."""
        from agent.graph.nodes.post_tool_reasoning import _parse_reasoning_response

        response = '{"next_action": "finalize", "action_reasoning": "Goal achieved - found services"}'

        output = _parse_reasoning_response(response)

        assert "Decision: finalize" in output.observation
        assert output.next_action == "finalize"
        assert "Goal achieved" in output.action_reasoning

    def test_parse_json_extraction(self):
        """JSON can be extracted from various formats."""
        from agent.graph.nodes.post_tool_reasoning import _extract_json_from_text

        # Pure JSON
        json_str = _extract_json_from_text('{"key": "value"}')
        assert json_str == '{"key": "value"}'

        # Markdown code block
        json_str = _extract_json_from_text('```json\n{"key": "value"}\n```')
        assert '{"key": "value"}' in json_str

        # Embedded JSON
        json_str = _extract_json_from_text('Some text {"key": "value"} more text')
        assert '{"key": "value"}' in json_str

    def test_call_tool_without_intent_converts_to_reflect(self):
        """call_tool without tool_intent is converted to reflect."""
        from agent.graph.nodes.post_tool_reasoning import _parse_reasoning_response

        response = '{"next_action": "call_tool", "action_reasoning": "Need more data"}'

        output = _parse_reasoning_response(response)

        # Should be converted to reflect due to missing tool_intent
        assert output.next_action == "reflect"
        assert "FORCED" in output.action_reasoning

    def test_goal_achieved_forces_finalize(self):
        """user_goal_achieved=True forces finalize action."""
        from agent.graph.nodes.post_tool_reasoning import _parse_reasoning_response

        response = """{"next_action": "call_tool", "action_reasoning": "Continue",
 "user_goal_achieved": true,
 "tool_intent": {"description": "More scanning", "target": "192.168.1.1", "focus": null}}"""

        output = _parse_reasoning_response(response)

        # Should be overridden to finalize
        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True


# =============================================================================
# DECISION ROUTER BASELINE TESTS
# =============================================================================


class TestDecisionRouterImports:
    """Verify all public exports are importable."""

    def test_main_function_importable(self):
        """decision_router function is importable."""
        from agent.graph.nodes.decision_router import decision_router

        assert callable(decision_router)

    def test_internal_functions_importable(self):
        """Internal helper functions are importable (for tests)."""
        from agent.graph.nodes.decision_router import (
            _build_pause_request,
            _check_all_todos_complete,
            _consume_post_reflect_hint,
            _count_consecutive_reflections,
            _emit_and_wait_for_pause_response,
            _extract_findings,
            _get_current_todo,
            _get_next_todo,
            _heuristic_decision,
            _parse_decision_response,
            _record_decision,
            _should_pause_for_confirmation,
        )

        assert callable(_check_all_todos_complete)
        assert callable(_get_current_todo)
        assert callable(_get_next_todo)
        assert callable(_extract_findings)
        assert callable(_record_decision)
        assert callable(_count_consecutive_reflections)
        assert callable(_consume_post_reflect_hint)
        assert callable(_should_pause_for_confirmation)
        assert callable(_build_pause_request)
        assert callable(_emit_and_wait_for_pause_response)
        assert callable(_heuristic_decision)
        assert callable(_parse_decision_response)


class TestDecisionRouterValidActions:
    """Verify valid actions constant."""

    def test_valid_actions(self):
        """VALID_ACTIONS contains expected actions."""
        from agent.graph.nodes.decision_router import VALID_ACTIONS

        assert VALID_ACTIONS == {"think_more", "call_tool", "reflect", "finalize", "synthesis"}


class TestDecisionRouterConsecutiveReflections:
    """Verify reflection counting behavior."""

    def test_no_reflections_returns_zero(self):
        """Empty history returns 0."""
        from agent.graph.nodes.decision_router import _count_consecutive_reflections

        assert _count_consecutive_reflections([]) == 0
        assert _count_consecutive_reflections(["call_tool: reason"]) == 0

    def test_counts_consecutive_reflections(self):
        """Correctly counts consecutive reflections at end."""
        from agent.graph.nodes.decision_router import _count_consecutive_reflections

        history = [
            "call_tool: first",
            "reflect: second",
            "reflect: third",
        ]
        assert _count_consecutive_reflections(history) == 2

    def test_breaks_on_non_reflect(self):
        """Chain broken by non-reflect action."""
        from agent.graph.nodes.decision_router import _count_consecutive_reflections

        history = [
            "reflect: first",
            "call_tool: break",
            "reflect: last",
        ]
        assert _count_consecutive_reflections(history) == 1


class TestDecisionRouterTodoHelpers:
    """Verify todo helper functions."""

    def test_check_all_todos_complete_empty(self):
        """Empty list returns False."""
        from agent.graph.nodes.decision_router import _check_all_todos_complete

        class MockFacts:
            todo_list = []
            safe_todo_list = []

        assert _check_all_todos_complete(MockFacts()) is False

    def test_check_all_todos_complete_strings(self):
        """String todos return False (not processed)."""
        from agent.graph.nodes.decision_router import _check_all_todos_complete

        class MockFacts:
            todo_list = ["Task 1", "Task 2"]
            safe_todo_list = ["Task 1", "Task 2"]

        assert _check_all_todos_complete(MockFacts()) is False


class TestDecisionRouterParseResponse:
    """Verify response parsing."""

    def test_parse_json_response(self):
        """JSON response parses correctly."""
        from agent.graph.nodes.decision_router import _parse_decision_response

        response = '{"action": "call_tool", "reasoning": "Need more data"}'
        action, reasoning = _parse_decision_response(response)

        assert action == "call_tool"
        assert "more data" in reasoning

    def test_parse_text_response(self):
        """Text with action keyword parses correctly."""
        from agent.graph.nodes.decision_router import _parse_decision_response

        response = "I should call_tool to gather more information."
        action, reasoning = _parse_decision_response(response)

        assert action == "call_tool"


# =============================================================================
# GRAPH INTEGRATION BASELINE TESTS
# =============================================================================


class TestGraphIntegration:
    """Verify graph integration works."""

    def test_graph_builds_successfully(self):
        """Deep reasoning graph builds without errors."""
        # This requires DATABASE_URL, so we patch the database module
        with patch("backend.database.engine"):
            with patch("backend.database.SessionLocal"):
                try:
                    from agent.graph.builders.deep_reasoning_builder import (
                        build_deep_reasoning_graph,
                    )

                    # If imports succeed, that's the main test
                    assert build_deep_reasoning_graph is not None
                except ImportError as e:
                    # Allow import errors related to database
                    if "DATABASE_URL" not in str(e):
                        raise

    def test_nodes_init_exports_both(self):
        """nodes/__init__.py exports both node functions."""
        from agent.graph.nodes import decision_router, post_tool_reasoning

        assert callable(decision_router)
        assert callable(post_tool_reasoning)


# =============================================================================
# STATE MODEL INTEGRATION TESTS
# =============================================================================


class TestStateModelIntegration:
    """Verify state model usage is consistent."""

    def test_interactive_state_importable(self):
        """InteractiveState can be imported from state.py."""
        from agent.graph.state import InteractiveState

        assert hasattr(InteractiveState, "from_mapping")
        assert hasattr(InteractiveState, "as_graph_update")

    def test_todo_models_importable(self):
        """Todo-related models can be imported from state.py."""
        from agent.graph.state import (
            AgentPauseRequest,
            CompletionType,
            TodoItem,
            TodoStatus,
        )

        assert TodoStatus.PENDING.value == "pending"
        assert TodoStatus.IN_PROGRESS.value == "in_progress"
        assert CompletionType.POSITIVE.value == "positive"
        assert hasattr(TodoItem, "from_string")
        assert hasattr(AgentPauseRequest, "model_validate")


# =============================================================================
# NODE UTILS INTEGRATION TESTS
# =============================================================================


class TestNodeUtilsIntegration:
    """Verify node_utils functions work."""

    def test_determine_post_reflect_action_importable(self):
        """determine_post_reflect_action can be imported."""
        from agent.graph.nodes.node_utils import determine_post_reflect_action

        assert callable(determine_post_reflect_action)

        # Test behavior
        assert determine_post_reflect_action([]) == "think_more"
        assert determine_post_reflect_action(["task1"]) == "call_tool"


# =============================================================================
# GUARDRAILS INTEGRATION TESTS
# =============================================================================


class TestGuardrailsIntegration:
    """Verify guardrail functions are importable."""

    def test_termination_guardrails_importable(self):
        """Termination guardrail functions can be imported."""
        from agent.graph.utils.termination_guardrails import (
            are_scope_goals_achieved,
            calculate_termination_bias,
            check_iteration_budget_warnings,
            has_sufficient_findings,
            is_action_loop_detected,
            is_stuck_without_progress,
        )

        assert callable(are_scope_goals_achieved)
        assert callable(is_action_loop_detected)
        assert callable(is_stuck_without_progress)
        assert callable(has_sufficient_findings)
        assert callable(calculate_termination_bias)
        assert callable(check_iteration_budget_warnings)

    def test_common_edges_importable(self):
        """Common edge functions can be imported."""
        from agent.graph.builders.common_edges import increment_stuck_counter

        assert callable(increment_stuck_counter)



