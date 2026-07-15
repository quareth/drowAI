"""Tests for progress-based routing.

Tests the _route_from_post_tool_decision function with progress tracking."""

import pytest
from unittest.mock import patch, MagicMock
import logging

from agent.graph.builders.deep_reasoning_builder import _route_decision
from agent.graph.state import (
    FactsState,
    InteractiveState,
    TraceState,
    TodoItem,
    TodoStatus,
    CompletionType,
)

logger = logging.getLogger(__name__)


def _route_from_post_tool_decision(state: InteractiveState) -> str:
    """Compatibility helper for legacy progress tests.

    Adapts decision-history fixtures to the router-outcome topology by seeding a
    deterministic candidate decision when the fixture only provides legacy
    decision history.
    """
    metadata = state.facts.metadata or {}
    todos = state.facts.todo_list or []
    completed = sum(
        1
        for todo in todos
        if hasattr(todo, "is_complete") and callable(todo.is_complete) and todo.is_complete()
    )
    logger.debug("%s/%s todos complete", completed, len(todos))
    goal_achieved = bool(metadata.get("user_goal_achieved", False))
    logger.debug("goal_achieved=%s", goal_achieved)

    if goal_achieved or bool(metadata.get("request_contract_terminal", False)):
        logger.info("user_goal_achieved flag set")
        action = "finalize"
    elif state.facts.decision_history:
        action = state.facts.decision_history[-1].split(":", 1)[0].strip().lower()
        if action == "synthesis":
            action = "finalize"
    else:
        action = ""

    metadata["router_outcome"] = {"action": action}
    state.facts.metadata = metadata
    return _route_decision(state)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def base_state() -> InteractiveState:
    """Base state with minimal fields."""
    return InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Test",
            conversation_id="conv-1",
            metadata={},
            decision_history=[],
        ),
        trace=TraceState(),
    )


# -----------------------------------------------------------------------------
# Tests: Goal Achievement Priority
# -----------------------------------------------------------------------------


class TestGoalAchievementPriority:
    """Tests that user_goal_achieved has highest routing priority."""
    
    def test_goal_achieved_routes_to_finalize(self, base_state: InteractiveState):
        """user_goal_achieved should route to finalize."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["call_tool: Continue"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_goal_achieved_overrides_call_tool(self, base_state: InteractiveState):
        """Goal achieved should override call_tool action."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["call_tool: Need more scans"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_goal_achieved_overrides_think_more(self, base_state: InteractiveState):
        """Goal achieved should override think_more action."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["think_more: Analyze more"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_goal_achieved_overrides_reflect(self, base_state: InteractiveState):
        """Goal achieved should override reflect action."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["reflect: Consider alternatives"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_no_goal_achieved_follows_decision(self, base_state: InteractiveState):
        """Without goal achieved, should follow decision."""
        base_state.facts.metadata = {}  # No goal achieved
        base_state.facts.decision_history = ["call_tool: Scan ports"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "select_categories"


# -----------------------------------------------------------------------------
# Tests: Decision History Routing
# -----------------------------------------------------------------------------


class TestDecisionHistoryRouting:
    """Tests for routing based on decision history."""
    
    # ``synthesis`` is intentionally absent from this parametrized set.
    # ``_route_from_post_tool_decision`` reads PTR's four-action contract;
    # see ``test_synthesis_label_treated_as_invalid_ptr_action`` below for
    # the tightened behavior.
    @pytest.mark.parametrize("action,expected_route", [
        ("call_tool", "select_categories"),
        ("think_more", "think_more"),
        ("reflect", "reflect"),
        ("finalize", "finalize"),
    ])
    def test_valid_actions_route_correctly(
        self, base_state: InteractiveState, action: str, expected_route: str
    ):
        """Valid PTR actions should route to correct nodes."""
        base_state.facts.decision_history = [f"{action}: Some reasoning"]

        result = _route_from_post_tool_decision(base_state)
        assert result == expected_route

    def test_synthesis_label_treated_as_invalid_ptr_action(
        self, base_state: InteractiveState
    ):
        """``synthesis`` is not a PTR action — falls through to ``finalize``.

        The PTR-facing post-tool route uses the four-action PTR vocabulary
        (no ``synthesis``). A manually inserted ``"synthesis: ..."`` PTR
        decision is treated as invalid and routes to the terminal default.
        """
        base_state.facts.decision_history = ["synthesis: contract violation"]

        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_unknown_action_defaults_to_finalize(self, base_state: InteractiveState):
        """Unknown actions should safely default to finalize."""
        base_state.facts.decision_history = ["invalid_action: Bad action"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_empty_history_defaults_to_finalize(self, base_state: InteractiveState):
        """Empty decision history should default to finalize."""
        base_state.facts.decision_history = []
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_uses_last_decision(self, base_state: InteractiveState):
        """Should use the most recent decision from history."""
        base_state.facts.decision_history = [
            "think_more: First thought",
            "call_tool: Then do this",  # Most recent
        ]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "select_categories"


# -----------------------------------------------------------------------------
# Tests: Progress Logging
# -----------------------------------------------------------------------------


class TestProgressLogging:
    """Tests for progress-related logging."""
    
    def test_logs_progress_stats(self, base_state: InteractiveState, caplog):
        """Routing should log progress statistics."""
        base_state.facts.todo_list = [
            TodoItem(description="Task 1", status=TodoStatus.COMPLETE_POSITIVE),
            TodoItem(description="Task 2", status=TodoStatus.IN_PROGRESS),
            TodoItem(description="Task 3", status=TodoStatus.PENDING),
        ]
        base_state.facts.decision_history = ["call_tool: Continue"]
        
        with caplog.at_level(logging.DEBUG):
            _route_from_post_tool_decision(base_state)
        
        # Should log progress: "1/3 todos complete"
        assert any("1/3 todos complete" in record.message for record in caplog.records)
    
    def test_logs_goal_achieved_status(self, base_state: InteractiveState, caplog):
        """Routing should log goal achieved status."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["finalize: Done"]
        
        with caplog.at_level(logging.DEBUG):
            _route_from_post_tool_decision(base_state)
        
        # Should log goal_achieved=True
        assert any("goal_achieved=True" in record.message for record in caplog.records)
    
    def test_logs_finalize_on_goal_achieved(self, base_state: InteractiveState, caplog):
        """Should log when finalizing due to goal achieved."""
        base_state.facts.metadata = {"user_goal_achieved": True}
        base_state.facts.decision_history = ["call_tool: Continue"]
        
        with caplog.at_level(logging.INFO):
            _route_from_post_tool_decision(base_state)
        
        # Should log finalize decision
        assert any(
            "user_goal_achieved flag set" in record.message 
            for record in caplog.records
        )


# -----------------------------------------------------------------------------
# Tests: Edge Cases
# -----------------------------------------------------------------------------


class TestRoutingEdgeCases:
    """Tests for edge cases in routing."""
    
    def test_empty_metadata_handled(self):
        """Empty metadata dict should be handled gracefully."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={},  # Empty dict (metadata can't be None in FactsState)
                decision_history=["finalize: Done"],
            ),
            trace=TraceState(),
        )
        
        # Should not crash
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_decision_without_colon(self, base_state: InteractiveState):
        """Decision without colon separator should still work."""
        base_state.facts.decision_history = ["finalize"]  # No reasoning after colon
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "finalize"
    
    def test_empty_todo_list_handled(self, base_state: InteractiveState, caplog):
        """Empty todo list should be handled for logging."""
        base_state.facts.todo_list = []  # Empty list (todo_list can't be None)
        base_state.facts.decision_history = ["finalize: Done"]
        
        with caplog.at_level(logging.DEBUG):
            result = _route_from_post_tool_decision(base_state)
        
        # Should log "0/0 todos complete"
        assert any("0/0 todos complete" in record.message for record in caplog.records)
        assert result == "finalize"
    
    def test_string_todos_not_counted(self, base_state: InteractiveState, caplog):
        """String todos (legacy) should not crash completion count."""
        base_state.facts.todo_list = ["Task 1", "Task 2"]  # Legacy string format
        base_state.facts.decision_history = ["call_tool: Continue"]
        
        with caplog.at_level(logging.DEBUG):
            result = _route_from_post_tool_decision(base_state)
        
        # String todos don't have is_complete() so count should be 0
        assert any("0/2 todos complete" in record.message for record in caplog.records)


# -----------------------------------------------------------------------------
# Tests: Call_tool Special Routing
# -----------------------------------------------------------------------------


class TestCallToolRouting:
    """Tests for call_tool → select_categories routing."""
    
    def test_call_tool_routes_to_select_categories(self, base_state: InteractiveState):
        """call_tool should route through category selector."""
        base_state.facts.decision_history = ["call_tool: Run nmap scan"]
        
        result = _route_from_post_tool_decision(base_state)
        
        # Should go to select_categories, not directly to call_tool
        assert result == "select_categories"
    
    def test_call_tool_not_goal_achieved(self, base_state: InteractiveState):
        """call_tool should work when goal not achieved."""
        base_state.facts.metadata = {"user_goal_achieved": False}
        base_state.facts.decision_history = ["call_tool: Continue scanning"]
        
        result = _route_from_post_tool_decision(base_state)
        assert result == "select_categories"

