"""Comprehensive tests for cache invalidation and plan management (DR.1)."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.cache_invalidation import (
    create_plan_context,
    invalidate_plan,
    is_plan_degraded,
    should_invalidate_plan,
)
from agent.graph.utils.plan_validation import (
    merge_plans,
    should_reject_plan_update,
    validate_plan_quality,
)


@pytest.fixture
def mock_state() -> InteractiveState:
    """Create a mock InteractiveState for testing."""
    facts = FactsState(
        task_id=1,
        message="Test message",
        capability="host_discovery",
        current_goal="Test goal",
        iterations=0,
        metadata={},
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def state_with_plan() -> InteractiveState:
    """Create state with a cached plan and context."""
    facts = FactsState(
        task_id=1,
        message="Test message",
        capability="host_discovery",
        current_goal="Test goal",
        iterations=0,
        metadata={
            "planner_plan": {
                "selected_tools": ["nmap"],
                "tool_parameters": {},
                "execution_strategy": "sequential",
            },
            "plan_context": {
                "capability": "host_discovery",
                "goal": "Test goal",
                "findings_count": 0,
                "iteration": 0,
                "created_at": 1000.0,
            },
        },
    )
    trace = TraceState()
    return InteractiveState(facts=facts, trace=trace)


class TestCreatePlanContext:
    """Tests for create_plan_context function."""

    def test_creates_context_with_current_state(self, mock_state: InteractiveState):
        """Test that context captures current state snapshot."""
        context = create_plan_context(mock_state)
        
        assert context["capability"] == "host_discovery"
        assert context["goal"] == "Test goal"
        assert context["findings_count"] == 0
        assert context["iteration"] == 0
        assert "created_at" in context

    def test_counts_findings_from_observations(self, mock_state: InteractiveState):
        """Test that findings are counted from trace observations."""
        mock_state.trace.observations = ["obs1", "obs2", "obs3"]
        
        context = create_plan_context(mock_state)
        
        assert context["findings_count"] == 3

    def test_counts_findings_from_executed_tools(self, mock_state: InteractiveState):
        """Test that findings are counted from executed tools."""
        from agent.graph.state import ToolExecutionRecord
        
        mock_state.trace.executed_tools = [
            ToolExecutionRecord(tool_id="nmap", args={}),
            ToolExecutionRecord(tool_id="masscan", args={}),
        ]
        
        context = create_plan_context(mock_state)
        
        assert context["findings_count"] == 2

    def test_counts_findings_from_tool_history(self, mock_state: InteractiveState):
        """Test that findings are counted from tool_history metadata."""
        mock_state.facts.metadata["tool_history"] = [
            {"tool": "nmap", "result": {}},
            {"tool": "masscan", "result": {}},
            {"tool": "nikto", "result": {}},
        ]
        
        context = create_plan_context(mock_state)
        
        assert context["findings_count"] == 3


class TestIsPlanDegraded:
    """Tests for is_plan_degraded function."""

    def test_detects_generic_steps_in_list(self):
        """Test that generic 'step 1', 'step 2' patterns are detected."""
        plan = ["step 1", "step 2", "step 3"]
        
        assert is_plan_degraded(plan) is True

    def test_detects_generic_steps_in_dict(self):
        """Test that generic steps in dict format are detected."""
        plan = {"plan": ["step 1", "step 2", "step 3"]}
        
        assert is_plan_degraded(plan) is True

    def test_accepts_specific_plans(self):
        """Test that specific plans are not marked as degraded."""
        plan = [
            "Scan target network for open ports",
            "Identify services running on discovered ports",
            "Analyze vulnerabilities in discovered services",
        ]
        
        assert is_plan_degraded(plan) is False

    def test_empty_plan_is_degraded(self):
        """Test that empty plan is considered degraded."""
        assert is_plan_degraded([]) is True
        assert is_plan_degraded({}) is True

    def test_mixed_plan_detection(self):
        """Test that >50% generic steps triggers degradation."""
        plan = ["step 1", "step 2", "Scan target for vulnerabilities"]
        
        assert is_plan_degraded(plan) is True


class TestShouldInvalidatePlan:
    """Tests for should_invalidate_plan function."""

    def test_no_plan_returns_false(self, mock_state: InteractiveState):
        """Test that state without plan returns False."""
        assert should_invalidate_plan(mock_state) is False

    def test_no_context_invalidates(self, state_with_plan: InteractiveState):
        """Test that plan without context is invalidated."""
        state_with_plan.facts.metadata.pop("plan_context")
        
        assert should_invalidate_plan(state_with_plan) is True

    def test_capability_change_invalidates(self, state_with_plan: InteractiveState):
        """Test that capability change triggers invalidation."""
        state_with_plan.facts.capability = "vuln_scan"
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_capability_change")

    def test_goal_change_invalidates(self, state_with_plan: InteractiveState):
        """Test that goal change triggers invalidation."""
        state_with_plan.facts.current_goal = "New goal"
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_goal_change")

    def test_findings_growth_invalidates(self, state_with_plan: InteractiveState):
        """Test that >50% findings growth triggers invalidation."""
        # Set initial findings count
        state_with_plan.facts.metadata["plan_context"]["findings_count"] = 2
        
        # Add more findings (>50% increase = >3 findings)
        state_with_plan.trace.observations = ["obs1", "obs2", "obs3", "obs4"]
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_findings_growth")

    def test_plan_expiration_invalidates(self, state_with_plan: InteractiveState):
        """Test that plan >3 iterations old is invalidated."""
        state_with_plan.facts.metadata["plan_context"]["iteration"] = 0
        state_with_plan.facts.iterations = 4  # 4 iterations later
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_age")

    def test_plan_degradation_invalidates(self, state_with_plan: InteractiveState):
        """Test that degraded plan triggers invalidation."""
        state_with_plan.facts.plan = ["step 1", "step 2", "step 3"]
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_degradation")

    def test_successful_tool_execution_invalidates(self, state_with_plan: InteractiveState):
        """Test that successful tool execution invalidates plan for deep reasoning progression."""
        # Simulate successful tool execution
        state_with_plan.facts.metadata["last_tool_result"] = {
            "success": True,
            "exit_code": 0,
            "stdout": "Host is up",
            "stderr": "",
        }
        
        # Plan was created at iteration 0, now at iteration 1 (after tool execution)
        state_with_plan.facts.metadata["plan_context"]["iteration"] = 0
        state_with_plan.facts.iterations = 1
        
        with patch("agent.graph.utils.cache_invalidation.safe_inc") as mock_inc:
            result = should_invalidate_plan(state_with_plan)
            
            assert result is True
            mock_inc.assert_called_with("cache_invalidation_new_observations")

    def test_plan_preserved_when_no_change(self, state_with_plan: InteractiveState):
        """Test that plan is preserved when no triggers fire."""
        # Keep all context values the same
        assert should_invalidate_plan(state_with_plan) is False


class TestInvalidatePlan:
    """Tests for invalidate_plan function."""

    def test_clears_plan_and_context(self, state_with_plan: InteractiveState):
        """Test that invalidate_plan clears both plan and context."""
        invalidate_plan(state_with_plan, reason="test")
        
        assert "planner_plan" not in state_with_plan.facts.metadata
        assert "plan_context" not in state_with_plan.facts.metadata

    def test_handles_missing_plan_gracefully(self, mock_state: InteractiveState):
        """Test that invalidate_plan handles missing plan gracefully."""
        # Should not raise exception
        invalidate_plan(mock_state, reason="test")


class TestValidatePlanQuality:
    """Tests for validate_plan_quality function."""

    def test_validates_specific_plan(self):
        """Test that specific plan is validated as high quality."""
        plan = [
            "Scan target network for open ports using Nmap",
            "Identify services and versions on discovered ports",
        ]
        
        result = validate_plan_quality(plan)
        
        assert result["valid"] is True
        assert result["generic_count"] == 0
        assert result["specificity_score"] > 0.5

    def test_rejects_generic_plan(self):
        """Test that generic plan is rejected."""
        plan = ["step 1", "step 2"]
        
        result = validate_plan_quality(plan)
        
        assert result["valid"] is False
        assert result["generic_count"] == 2
        assert result["specificity_score"] < 0.5

    def test_rejects_short_plan(self):
        """Test that plan with <2 steps is rejected."""
        plan = ["Single step"]
        
        result = validate_plan_quality(plan)
        
        assert result["valid"] is False

    def test_rejects_empty_plan(self):
        """Test that empty plan is rejected."""
        result = validate_plan_quality([])
        
        assert result["valid"] is False


class TestMergePlans:
    """Tests for merge_plans function."""

    def test_merges_high_quality_new_plan(self):
        """Test that high-quality new plan is used."""
        old_plan = ["Old step 1", "Old step 2"]
        new_plan = [
            "Scan target network for open ports",
            "Identify services and versions",
        ]
        
        merged = merge_plans(old_plan, new_plan)
        
        assert merged == new_plan

    def test_preserves_high_quality_old_plan(self):
        """Test that high-quality old plan is preserved when new is low quality."""
        old_plan = [
            "Scan target network for open ports using Nmap",
            "Identify services and versions on discovered ports",
        ]
        new_plan = ["step 1", "step 2"]
        
        merged = merge_plans(old_plan, new_plan)
        
        assert merged == old_plan

    def test_merges_specific_steps(self):
        """Test that specific steps from both plans are merged."""
        old_plan = ["Old specific step 1", "Old specific step 2"]
        new_plan = ["New specific step 1", "step 2"]
        
        merged = merge_plans(old_plan, new_plan)
        
        # Should contain specific steps from both
        assert len(merged) >= 2
        assert any("Old specific" in step for step in merged)
        assert any("New specific" in step for step in merged)

    def test_handles_empty_old_plan(self):
        """Test that empty old plan returns new plan."""
        new_plan = ["Step 1", "Step 2"]
        
        merged = merge_plans([], new_plan)
        
        assert merged == new_plan


class TestShouldRejectPlanUpdate:
    """Tests for should_reject_plan_update function."""

    def test_rejects_degraded_new_plan(self):
        """Test that degraded new plan is rejected when old is good."""
        old_plan = [
            "Scan target network for open ports",
            "Identify services and versions",
        ]
        new_plan = ["step 1", "step 2"]
        
        assert should_reject_plan_update(old_plan, new_plan) is True

    def test_accepts_improved_plan(self):
        """Test that improved plan is accepted."""
        old_plan = ["step 1", "step 2"]
        new_plan = [
            "Scan target network for open ports",
            "Identify services and versions",
        ]
        
        assert should_reject_plan_update(old_plan, new_plan) is False

    def test_accepts_when_no_old_plan(self):
        """Test that update is accepted when no old plan exists."""
        new_plan = ["Step 1", "Step 2"]
        
        assert should_reject_plan_update([], new_plan) is False

    def test_rejects_empty_new_plan(self):
        """Test that empty new plan is rejected."""
        old_plan = ["Step 1", "Step 2"]
        
        assert should_reject_plan_update(old_plan, []) is True


class TestIntegration:
    """Integration tests for cache invalidation flow."""

    def test_full_invalidation_flow(self, state_with_plan: InteractiveState):
        """Test complete invalidation flow from detection to clearing."""
        # Change capability to trigger invalidation
        state_with_plan.facts.capability = "vuln_scan"
        
        # Should detect invalidation
        assert should_invalidate_plan(state_with_plan) is True
        
        # Invalidate plan
        invalidate_plan(state_with_plan)
        
        # Plan should be cleared
        assert "planner_plan" not in state_with_plan.facts.metadata
        assert "plan_context" not in state_with_plan.facts.metadata

    def test_plan_context_creation_and_usage(self, mock_state: InteractiveState):
        """Test that plan context is created and used correctly."""
        # Create context
        context = create_plan_context(mock_state)
        
        # Store in metadata
        mock_state.facts.metadata["plan_context"] = context
        mock_state.facts.metadata["planner_plan"] = {"selected_tools": ["nmap"]}
        
        # Change capability
        mock_state.facts.capability = "vuln_scan"
        
        # Should detect invalidation
        assert should_invalidate_plan(mock_state) is True

