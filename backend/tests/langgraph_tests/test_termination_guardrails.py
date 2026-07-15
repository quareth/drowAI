"""Comprehensive tests for termination guardrails (DR.2)."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from agent.graph.state import FactsState, InteractiveState, TraceState, ToolExecutionRecord
from agent.graph.utils.termination_guardrails import (
    are_scope_goals_achieved,
    calculate_termination_bias,
    check_goal_completion,
    check_iteration_budget_warnings,
    has_sufficient_findings,
    is_action_loop_detected,
    is_stuck_without_progress,
)


@pytest.fixture
def mock_state() -> InteractiveState:
    """Create a mock InteractiveState for testing."""
    facts = FactsState(
        task_id=1,
        message="Test message",
        capability="gather_info",
        current_goal="Test goal",
        iterations=0,
        metadata={},
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace)


class TestAreScopeGoalsAchieved:
    """Tests for are_scope_goals_achieved function."""

    def test_no_goals_returns_false(self, mock_state: InteractiveState):
        """Test that state without scope goals returns False."""
        assert are_scope_goals_achieved(mock_state) is False

    def test_all_goals_achieved_returns_true(self, mock_state: InteractiveState):
        """Test that all achieved goals returns True."""
        mock_state.facts.metadata["scope_goals"] = ["goal1", "goal2"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1", "goal2"}
        
        assert are_scope_goals_achieved(mock_state) is True

    def test_partial_goals_returns_false(self, mock_state: InteractiveState):
        """Test that partial goal achievement returns False."""
        mock_state.facts.metadata["scope_goals"] = ["goal1", "goal2", "goal3"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1", "goal2"}
        
        assert are_scope_goals_achieved(mock_state) is False

    def test_handles_list_achieved_goals(self, mock_state: InteractiveState):
        """Test that list format for achieved_goals is handled."""
        mock_state.facts.metadata["scope_goals"] = ["goal1"]
        mock_state.facts.metadata["achieved_goals"] = ["goal1"]  # List format
        
        assert are_scope_goals_achieved(mock_state) is True


class TestCheckGoalCompletion:
    """Tests for check_goal_completion function."""

    def test_find_vulnerable_services_detected(self):
        """Test that vulnerable services are detected."""
        findings = [{"type": "vulnerability", "description": "SQL injection found"}]
        observations = ["Found PostgreSQL 9.6.0 with known vulnerabilities"]
        
        assert check_goal_completion("find_vulnerable_services", findings, observations) is True

    def test_identify_hosts_detected(self):
        """Test that host discovery is detected."""
        findings = [{"type": "host_discovered", "ip": "192.168.1.1"}]
        observations = ["Discovered host 192.168.1.1"]
        
        assert check_goal_completion("identify_hosts", findings, observations) is True

    def test_identify_open_ports_detected(self):
        """Test that open ports are detected."""
        findings = [{"type": "open_port", "port": 22}]
        observations = ["Found open port 22 (SSH)"]
        
        assert check_goal_completion("identify_open_ports", findings, observations) is True

    def test_unknown_goal_returns_false(self):
        """Test that unknown goal returns False."""
        assert check_goal_completion("unknown_goal", [], []) is False


class TestIsActionLoopDetected:
    """Tests for is_action_loop_detected function."""

    def test_no_history_returns_false(self, mock_state: InteractiveState):
        """Test that state without action history returns False."""
        assert is_action_loop_detected(mock_state) is False

    def test_insufficient_history_returns_false(self, mock_state: InteractiveState):
        """Test that <3 actions returns False."""
        mock_state.facts.metadata["action_history"] = [
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
        ]
        
        assert is_action_loop_detected(mock_state) is False

    def test_three_identical_actions_detected(self, mock_state: InteractiveState):
        """Test that 3 identical actions are detected as loop."""
        mock_state.facts.metadata["action_history"] = [
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "ports": "22,80"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "ports": "22,80"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "ports": "22,80"}},
        ]
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            result = is_action_loop_detected(mock_state)
            
            assert result is True
            mock_inc.assert_called_with("router_loop_detected")

    def test_different_actions_not_detected(self, mock_state: InteractiveState):
        """Test that different actions are not detected as loop."""
        mock_state.facts.metadata["action_history"] = [
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
            {"tool_id": "masscan", "params": {"target": "127.0.0.1"}},
            {"tool_id": "nmap", "params": {"target": "192.168.1.1"}},
        ]
        
        assert is_action_loop_detected(mock_state) is False

    def test_ignores_timestamp_in_comparison(self, mock_state: InteractiveState):
        """Test that timestamps are ignored in action comparison."""
        mock_state.facts.metadata["action_history"] = [
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "timestamp": "1000"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "timestamp": "2000"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1", "timestamp": "3000"}},
        ]
        
        assert is_action_loop_detected(mock_state) is True

    def test_ignores_turn_sequence_in_action_history_comparison(self, mock_state: InteractiveState):
        """Extra action-history metadata must not break existing loop detection."""
        mock_state.facts.metadata["planner_plan"] = {"selected_tools": ["nmap"]}
        mock_state.facts.metadata["action_history"] = [
            {
                "tool_id": "nmap",
                "params": {"target": "127.0.0.1", "ports": "22"},
                "turn_sequence": 7,
            },
            {
                "tool_id": "nmap",
                "params": {"target": "127.0.0.1", "ports": "22"},
                "turn_sequence": 7,
            },
            {
                "tool_id": "nmap",
                "params": {"target": "127.0.0.1", "ports": "22"},
                "turn_sequence": 7,
            },
        ]

        assert is_action_loop_detected(mock_state) is True

    def test_builds_from_executed_tools(self, mock_state: InteractiveState):
        """Test that action history is built from executed_tools if missing."""
        mock_state.trace.executed_tools = [
            ToolExecutionRecord(tool_id="nmap", args={"target": "127.0.0.1"}),
            ToolExecutionRecord(tool_id="nmap", args={"target": "127.0.0.1"}),
            ToolExecutionRecord(tool_id="nmap", args={"target": "127.0.0.1"}),
        ]
        
        assert is_action_loop_detected(mock_state) is True


class TestIsStuckWithoutProgress:
    """Tests for is_stuck_without_progress function."""

    def test_no_observation_hashes_returns_false(self, mock_state: InteractiveState):
        """Test that state without observation hashes returns False."""
        assert is_stuck_without_progress(mock_state) is False

    def test_insufficient_hashes_returns_false(self, mock_state: InteractiveState):
        """Test that <2 hashes returns False."""
        mock_state.facts.metadata["observation_hashes"] = ["hash1"]
        
        assert is_stuck_without_progress(mock_state) is False

    def test_identical_hashes_detected(self, mock_state: InteractiveState):
        """Test that identical last 2 hashes are detected."""
        mock_state.facts.metadata["observation_hashes"] = ["hash1", "hash2", "hash2"]
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            result = is_stuck_without_progress(mock_state)
            
            assert result is True
            mock_inc.assert_called_with("router_finalize_no_progress")

    def test_different_hashes_not_detected(self, mock_state: InteractiveState):
        """Test that different hashes are not detected as stuck."""
        mock_state.facts.metadata["observation_hashes"] = ["hash1", "hash2", "hash3"]
        
        assert is_stuck_without_progress(mock_state) is False


class TestHasSufficientFindings:
    """Tests for has_sufficient_findings function."""

    def test_no_findings_returns_false(self, mock_state: InteractiveState):
        """Test that state without findings returns False."""
        assert has_sufficient_findings(mock_state) is False

    def test_observations_with_substance_returns_true(self, mock_state: InteractiveState):
        """Test that substantial observations return True."""
        mock_state.trace.observations = [
            "Found PostgreSQL 9.6.0 with known vulnerabilities on port 5432"
        ]
        
        assert has_sufficient_findings(mock_state) is True

    def test_short_observations_returns_false(self, mock_state: InteractiveState):
        """Test that very short observations return False."""
        mock_state.trace.observations = ["ok"]
        
        assert has_sufficient_findings(mock_state) is False

    def test_executed_tools_returns_true(self, mock_state: InteractiveState):
        """Test that executed tools return True."""
        mock_state.trace.executed_tools = [
            ToolExecutionRecord(tool_id="nmap", args={})
        ]
        
        assert has_sufficient_findings(mock_state) is True


class TestCalculateTerminationBias:
    """Tests for calculate_termination_bias function."""

    def test_no_bias_when_early(self, mock_state: InteractiveState):
        """Test that early iterations have low bias."""
        mock_state.facts.iterations = 1
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 14}
        mock_state.facts.budgets.max_iterations = 15
        
        bias = calculate_termination_bias(mock_state)
        
        assert bias < 0.3

    def test_high_bias_when_goals_achieved(self, mock_state: InteractiveState):
        """Test that achieved goals increase bias."""
        mock_state.facts.metadata["scope_goals"] = ["goal1"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1"}
        
        bias = calculate_termination_bias(mock_state)
        
        assert bias >= 0.5

    def test_bias_increases_with_iteration_usage(self, mock_state: InteractiveState):
        """Test that high iteration usage increases bias."""
        mock_state.facts.iterations = 10
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 5}
        mock_state.facts.budgets.max_iterations = 15
        
        bias = calculate_termination_bias(mock_state)
        
        assert bias >= 0.3

    def test_bias_increases_with_no_progress(self, mock_state: InteractiveState):
        """Test that no progress increases bias."""
        mock_state.facts.metadata["observation_hashes"] = ["hash1", "hash1"]
        
        bias = calculate_termination_bias(mock_state)
        
        assert bias >= 0.4

    def test_bias_capped_at_one(self, mock_state: InteractiveState):
        """Test that bias is capped at 1.0."""
        # Set multiple high-bias conditions
        mock_state.facts.metadata["scope_goals"] = ["goal1"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1"}
        mock_state.facts.metadata["observation_hashes"] = ["hash1", "hash1"]
        mock_state.trace.observations = ["Substantial observation with findings"]
        
        bias = calculate_termination_bias(mock_state)
        
        assert bias <= 1.0


class TestCheckIterationBudgetWarnings:
    """Tests for check_iteration_budget_warnings function."""

    def test_no_warning_when_low_usage(self, mock_state: InteractiveState):
        """Test that low usage doesn't trigger warning."""
        mock_state.facts.iterations = 2
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 13}
        mock_state.facts.budgets.max_iterations = 15
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            check_iteration_budget_warnings(mock_state)
            
            mock_inc.assert_not_called()

    def test_warning_at_75_percent(self, mock_state: InteractiveState):
        """Test that warning is logged at 75% usage."""
        mock_state.facts.iterations = 11
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 4}
        mock_state.facts.budgets.max_iterations = 15
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            with patch("agent.graph.utils.termination_guardrails.logger") as mock_logger:
                check_iteration_budget_warnings(mock_state)
                
                mock_inc.assert_called_with("router_iteration_budget_warning")
                mock_logger.warning.assert_called()

    def test_warning_at_90_percent(self, mock_state: InteractiveState):
        """Test that warning is logged at 90% usage."""
        mock_state.facts.iterations = 13
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 2}
        mock_state.facts.budgets.max_iterations = 15
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            check_iteration_budget_warnings(mock_state)
            
            mock_inc.assert_called_with("router_iteration_budget_warning")

    def test_warning_at_100_percent(self, mock_state: InteractiveState):
        """Test that warning is logged at 100% usage."""
        mock_state.facts.iterations = 15
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 0}
        mock_state.facts.budgets.max_iterations = 15
        
        with patch("agent.graph.utils.termination_guardrails.safe_inc") as mock_inc:
            check_iteration_budget_warnings(mock_state)
            
            mock_inc.assert_called_with("router_iteration_budget_warning")


class TestIntegration:
    """Integration tests for termination guardrails."""

    def test_multiple_guardrails_work_together(self, mock_state: InteractiveState):
        """Test that multiple guardrails can be checked together."""
        # Set up state with multiple conditions
        mock_state.facts.metadata["scope_goals"] = ["goal1"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1"}
        mock_state.facts.metadata["action_history"] = [
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
            {"tool_id": "nmap", "params": {"target": "127.0.0.1"}},
        ]
        
        # All should be detected
        assert are_scope_goals_achieved(mock_state) is True
        assert is_action_loop_detected(mock_state) is True
        assert has_sufficient_findings(mock_state) is False  # No findings yet

    def test_termination_bias_with_all_conditions(self, mock_state: InteractiveState):
        """Test termination bias calculation with all conditions."""
        mock_state.facts.metadata["scope_goals"] = ["goal1"]
        mock_state.facts.metadata["achieved_goals"] = {"goal1"}
        mock_state.facts.metadata["observation_hashes"] = ["hash1", "hash1"]
        mock_state.trace.observations = ["Substantial finding with details"]
        mock_state.facts.iterations = 12
        mock_state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 3}
        mock_state.facts.budgets.max_iterations = 15
        
        bias = calculate_termination_bias(mock_state)
        
        # Should have high bias with all conditions
        assert bias > 0.7
