"""Comprehensive tests for scope management and goal tracking (DR.5)."""

from __future__ import annotations

import pytest
from typing import Dict, List, Set

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.goal_tracker import check_goal_completion, update_achieved_goals
from agent.graph.utils.scope_parser import UserScope, parse_user_scope
from agent.graph.utils.scope_progress import (
    calculate_scope_progress,
    get_progress_milestone,
    log_progress_milestone,
)
from agent.graph.utils.scope_validator import validate_plan_against_scope


class TestScopeParser:
    """Test scope parsing functionality."""

    def test_parse_find_vulnerable_services(self):
        """Test parsing 'find vulnerable services' request."""
        request = "Find vulnerable services on 127.0.0.1"
        scope = parse_user_scope(request)

        assert "find_vulnerable_services" in scope.goals
        assert len(scope.boundaries) == 0
        assert len(scope.conditional_targets) == 0

    def test_parse_identify_hosts(self):
        """Test parsing 'identify hosts' request."""
        request = "Scan network 192.168.1.0/24 for hosts"
        scope = parse_user_scope(request)

        assert "identify_hosts" in scope.goals
        assert len(scope.boundaries) == 0

    def test_parse_identify_open_ports(self):
        """Test parsing 'identify open ports' request."""
        request = "Scan 127.0.0.1 for open ports"
        scope = parse_user_scope(request)

        assert "identify_open_ports" in scope.goals

    def test_parse_no_exploitation_boundary(self):
        """Test parsing 'do NOT exploit' boundary."""
        request = "Scan ports but do NOT exploit"
        scope = parse_user_scope(request)

        assert "identify_open_ports" in scope.goals
        assert "no_exploitation" in scope.boundaries

    def test_parse_no_brute_force_boundary(self):
        """Test parsing 'no brute force' boundary."""
        request = "Find services but avoid brute force attacks"
        scope = parse_user_scope(request)

        assert "no_brute_force" in scope.boundaries

    def test_parse_conditional_target(self):
        """Test parsing conditional target."""
        request = "Scan 192.168.1.0/24 for hosts, if none found use 127.0.0.1"
        scope = parse_user_scope(request)

        assert "identify_hosts" in scope.goals
        assert "fallback_host" in scope.conditional_targets
        assert scope.conditional_targets["fallback_host"] == "127.0.0.1"

    def test_parse_explicit_tools(self):
        """Test parsing explicitly mentioned tools."""
        request = "Use nmap to scan ports on 127.0.0.1"
        scope = parse_user_scope(request)

        assert "nmap" in scope.explicit_tools
        assert "identify_open_ports" in scope.goals

    def test_parse_multiple_goals(self):
        """Test parsing multiple goals."""
        request = "Find vulnerable services and identify open ports on 127.0.0.1"
        scope = parse_user_scope(request)

        assert "find_vulnerable_services" in scope.goals
        assert "identify_open_ports" in scope.goals
        assert len(scope.goals) >= 2

    def test_parse_complex_request(self):
        """Test parsing complex request with all features."""
        request = (
            "Scan 192.168.1.0/24 for hosts and find vulnerable services, "
            "but do NOT exploit. If no hosts found use 127.0.0.1. Use nmap."
        )
        scope = parse_user_scope(request)

        assert "identify_hosts" in scope.goals
        assert "find_vulnerable_services" in scope.goals
        assert "no_exploitation" in scope.boundaries
        assert "fallback_host" in scope.conditional_targets
        assert "nmap" in scope.explicit_tools


class TestGoalTracker:
    """Test goal completion tracking."""

    def test_check_goal_completion_vulnerable_services(self):
        """Test checking completion for 'find_vulnerable_services' goal."""
        findings = [{"type": "vulnerability", "content": "PostgreSQL 9.6.0 has known vulnerabilities"}]
        observations = []

        is_complete = check_goal_completion("find_vulnerable_services", findings, observations)
        assert is_complete is True

    def test_check_goal_completion_identify_hosts(self):
        """Test checking completion for 'identify_hosts' goal."""
        findings = [{"type": "host_discovered", "content": "192.168.1.1"}]
        observations = []

        is_complete = check_goal_completion("identify_hosts", findings, observations)
        assert is_complete is True

    def test_check_goal_completion_identify_open_ports(self):
        """Test checking completion for 'identify_open_ports' goal."""
        findings = [{"type": "port_scan", "content": "Port 22/tcp is open"}]
        observations = []

        is_complete = check_goal_completion("identify_open_ports", findings, observations)
        assert is_complete is True

    def test_check_goal_completion_from_observations(self):
        """Test checking completion from observations."""
        findings = []
        observations = ["Found vulnerable service: PostgreSQL 9.6.0"]

        is_complete = check_goal_completion("find_vulnerable_services", findings, observations)
        assert is_complete is True

    def test_check_goal_completion_not_achieved(self):
        """Test checking completion when goal not achieved."""
        findings = [{"type": "port_scan", "content": "Port 22/tcp is open"}]
        observations = []

        is_complete = check_goal_completion("find_vulnerable_services", findings, observations)
        assert is_complete is False

    def test_update_achieved_goals(self):
        """Test updating achieved goals in state."""
        facts = FactsState(
            task_id=1,
            message="Find vulnerable services",
            scope_goals=["find_vulnerable_services"],
            achieved_goals=set(),
        )
        trace = TraceState(
            observations=["Found vulnerable service: PostgreSQL 9.6.0"],
            executed_tools=[],
        )
        state = InteractiveState(facts=facts, trace=trace)

        update_achieved_goals(state)

        assert "find_vulnerable_services" in state.facts.achieved_goals

    def test_update_achieved_goals_multiple(self):
        """Test updating multiple goals."""
        facts = FactsState(
            task_id=1,
            message="Find vulnerable services and identify hosts",
            scope_goals=["find_vulnerable_services", "identify_hosts"],
            achieved_goals=set(),
        )
        trace = TraceState(
            observations=[
                "Found vulnerable service: PostgreSQL 9.6.0",
                "Discovered host: 192.168.1.1",
            ],
            executed_tools=[],
        )
        state = InteractiveState(facts=facts, trace=trace)

        update_achieved_goals(state)

        assert "find_vulnerable_services" in state.facts.achieved_goals
        assert "identify_hosts" in state.facts.achieved_goals


class TestScopeValidator:
    """Test plan validation against scope."""

    def test_validate_plan_no_violations(self):
        """Test validating plan with no violations."""
        plan = [
            "Step 1: Scan ports on 127.0.0.1",
            "Step 2: Identify services",
            "Step 3: Check for vulnerabilities",
        ]
        scope = UserScope(
            goals=["identify_open_ports", "find_vulnerable_services"],
            boundaries=[],
            conditional_targets={},
            explicit_tools=[],
        )

        result = validate_plan_against_scope(plan, scope)

        assert result["valid"] is True
        assert len(result["violations"]) == 0

    def test_validate_plan_exploitation_violation(self):
        """Test validating plan with exploitation violation."""
        plan = [
            "Step 1: Scan ports",
            "Step 2: Exploit vulnerability",
            "Step 3: Report findings",
        ]
        scope = UserScope(
            goals=["identify_open_ports"],
            boundaries=["no_exploitation"],
            conditional_targets={},
            explicit_tools=[],
        )

        result = validate_plan_against_scope(plan, scope)

        assert result["valid"] is False
        assert len(result["violations"]) > 0
        assert any("exploitation" in v.lower() for v in result["violations"])

    def test_validate_plan_brute_force_violation(self):
        """Test validating plan with brute force violation."""
        plan = [
            "Step 1: Scan ports",
            "Step 2: Brute force SSH passwords",
            "Step 3: Report findings",
        ]
        scope = UserScope(
            goals=["identify_open_ports"],
            boundaries=["no_brute_force"],
            conditional_targets={},
            explicit_tools=[],
        )

        result = validate_plan_against_scope(plan, scope)

        assert result["valid"] is False
        assert any("brute" in v.lower() for v in result["violations"])

    def test_validate_plan_missing_goal(self):
        """Test validating plan that doesn't address a goal."""
        plan = [
            "Step 1: Scan ports",
            "Step 2: Report findings",
        ]
        scope = UserScope(
            goals=["identify_open_ports", "find_vulnerable_services"],
            boundaries=[],
            conditional_targets={},
            explicit_tools=[],
        )

        result = validate_plan_against_scope(plan, scope)

        assert result["valid"] is False
        assert any("find_vulnerable_services" in v for v in result["violations"])

    def test_validate_plan_empty_plan(self):
        """Test validating empty plan."""
        plan = []
        scope = UserScope(
            goals=["identify_open_ports"],
            boundaries=[],
            conditional_targets={},
            explicit_tools=[],
        )

        result = validate_plan_against_scope(plan, scope)

        assert result["valid"] is True  # Empty plan is considered valid


class TestScopeProgress:
    """Test scope progress tracking."""

    def test_calculate_scope_progress_no_goals(self):
        """Test calculating progress with no goals."""
        facts = FactsState(task_id=1, message="Test", scope_goals=[], achieved_goals=set())
        state = InteractiveState(facts=facts)

        progress = calculate_scope_progress(state)

        assert progress == 0.0

    def test_calculate_scope_progress_zero(self):
        """Test calculating progress with no achieved goals."""
        facts = FactsState(
            task_id=1,
            message="Test",
            scope_goals=["goal1", "goal2"],
            achieved_goals=set(),
        )
        state = InteractiveState(facts=facts)

        progress = calculate_scope_progress(state)

        assert progress == 0.0

    def test_calculate_scope_progress_partial(self):
        """Test calculating partial progress."""
        facts = FactsState(
            task_id=1,
            message="Test",
            scope_goals=["goal1", "goal2", "goal3"],
            achieved_goals={"goal1"},
        )
        state = InteractiveState(facts=facts)

        progress = calculate_scope_progress(state)

        assert progress == pytest.approx(1.0 / 3.0, rel=0.01)

    def test_calculate_scope_progress_complete(self):
        """Test calculating complete progress."""
        facts = FactsState(
            task_id=1,
            message="Test",
            scope_goals=["goal1", "goal2"],
            achieved_goals={"goal1", "goal2"},
        )
        state = InteractiveState(facts=facts)

        progress = calculate_scope_progress(state)

        assert progress == 1.0

    def test_get_progress_milestone(self):
        """Test getting progress milestone."""
        assert get_progress_milestone(0.0) is None
        assert get_progress_milestone(0.25) == "25%"
        assert get_progress_milestone(0.5) == "50%"
        assert get_progress_milestone(0.75) == "75%"
        assert get_progress_milestone(1.0) == "100%"

    def test_log_progress_milestone(self):
        """Test logging progress milestone."""
        facts = FactsState(
            task_id=1,
            message="Test",
            scope_goals=["goal1", "goal2", "goal3", "goal4"],
            achieved_goals={"goal1"},  # 25% progress
            metadata={},
        )
        state = InteractiveState(facts=facts)

        log_progress_milestone(state)

        assert state.facts.metadata.get("last_progress_milestone") == "25%"

    def test_log_progress_milestone_no_duplicate(self):
        """Test that milestones are not logged twice."""
        facts = FactsState(
            task_id=1,
            message="Test",
            scope_goals=["goal1", "goal2"],
            achieved_goals={"goal1"},  # 50% progress
            metadata={"last_progress_milestone": "50%"},
        )
        state = InteractiveState(facts=facts)

        log_progress_milestone(state)

        # Should not change since already logged
        assert state.facts.metadata.get("last_progress_milestone") == "50%"


class TestIntegration:
    """Integration tests for scope management."""

    def test_full_scope_workflow(self):
        """Test full workflow: parse → track → validate → progress."""
        # Parse scope
        request = "Find vulnerable services on 127.0.0.1 but do NOT exploit"
        scope = parse_user_scope(request)

        assert "find_vulnerable_services" in scope.goals
        assert "no_exploitation" in scope.boundaries

        # Validate plan
        plan = [
            "Step 1: Scan ports",
            "Step 2: Identify services",
            "Step 3: Check for vulnerabilities",
        ]
        validation = validate_plan_against_scope(plan, scope)

        assert validation["valid"] is True

        # Track goals
        facts = FactsState(
            task_id=1,
            message=request,
            scope_goals=scope.goals,
            achieved_goals=set(),
        )
        trace = TraceState(
            observations=["Found vulnerable service: PostgreSQL 9.6.0"],
            executed_tools=[],
        )
        state = InteractiveState(facts=facts, trace=trace)

        update_achieved_goals(state)

        assert "find_vulnerable_services" in state.facts.achieved_goals

        # Calculate progress
        progress = calculate_scope_progress(state)
        assert progress == 1.0  # All goals achieved


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

