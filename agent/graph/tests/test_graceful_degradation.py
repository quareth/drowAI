"""Comprehensive tests for graceful degradation and tool availability.

Tests cover:
- Tool availability checking
- Fallback capability resolution
- Graceful degradation node decisions
- Tool gap reporting
- Planner tool availability checks
- Finalizer tool gap reporting
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from agent.graph.infrastructure.state_models import CapabilityType
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.tool_availability import (
    are_tools_available,
    get_fallback_capability,
    get_available_tools_for_capability,
    clear_availability_cache,
)


class TestToolAvailabilityChecker:
    """Test tool availability checking functionality."""

    def test_are_tools_available_with_tools(self):
        """Test availability check when tools exist."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = ["tool1", "tool2"]
            clear_availability_cache()
            
            result = are_tools_available(CapabilityType.PORT_SCAN)
            assert result is True
            mock_resolve.assert_called_once()

    def test_are_tools_available_no_tools(self):
        """Test availability check when no tools exist."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = []
            clear_availability_cache()
            
            result = are_tools_available(CapabilityType.VULN_SCAN)
            assert result is False

    def test_are_tools_available_caching(self):
        """Test that availability checks are cached."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = ["tool1"]
            clear_availability_cache()
            
            # First call
            result1 = are_tools_available(CapabilityType.PORT_SCAN)
            assert result1 is True
            assert mock_resolve.call_count == 1
            
            # Second call should use cache
            result2 = are_tools_available(CapabilityType.PORT_SCAN)
            assert result2 is True
            assert mock_resolve.call_count == 1  # No additional call

    def test_are_tools_available_string_capability(self):
        """Test availability check with string capability."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = ["tool1"]
            clear_availability_cache()
            
            result = are_tools_available("port_scan")
            assert result is True

    def test_are_tools_available_respond_capability(self):
        """Test that RESPOND capability returns False (no tools needed)."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = []  # RESPOND has no tools
            clear_availability_cache()
            
            result = are_tools_available(CapabilityType.RESPOND)
            assert result is False


class TestFallbackCapability:
    """Test fallback capability resolution."""

    def test_get_fallback_vuln_scan(self):
        """Test fallback for VULN_SCAN."""
        fallback = get_fallback_capability(CapabilityType.VULN_SCAN)
        assert fallback == CapabilityType.SERVICE_ENUM

    def test_get_fallback_vuln_exploit(self):
        """Test fallback for VULN_EXPLOIT."""
        fallback = get_fallback_capability(CapabilityType.VULN_EXPLOIT)
        assert fallback == CapabilityType.VULN_SCAN

    def test_get_fallback_service_enum(self):
        """Test fallback for SERVICE_ENUM."""
        fallback = get_fallback_capability(CapabilityType.SERVICE_ENUM)
        assert fallback == CapabilityType.PORT_SCAN

    def test_get_fallback_port_scan(self):
        """Test fallback for PORT_SCAN."""
        fallback = get_fallback_capability(CapabilityType.PORT_SCAN)
        assert fallback == CapabilityType.HOST_DISCOVERY

    def test_get_fallback_no_fallback(self):
        """Test capabilities with no fallback."""
        assert get_fallback_capability(CapabilityType.HOST_DISCOVERY) is None
        assert get_fallback_capability(CapabilityType.REPORT) is None
        assert get_fallback_capability(CapabilityType.RESPOND) is None

    def test_get_fallback_string_capability(self):
        """Test fallback with string capability."""
        fallback = get_fallback_capability("vuln_scan")
        assert fallback == CapabilityType.SERVICE_ENUM


class TestHandleUnavailableToolsNode:
    """Test handle_unavailable_tools node decisions."""

    @pytest.mark.asyncio
    async def test_finalize_when_scope_satisfied(self):
        """Test finalize when scope goals achieved despite missing tools."""
        from agent.graph.nodes.handle_unavailable_tools import handle_unavailable_tools_node
        
        # Create state with scope goals achieved
        facts = FactsState(
            task_id=1,
            message="test",
            capability="vuln_scan",
        )
        facts.metadata = {
            "scope_goals": ["find_vulnerable_services"],
            "achieved_goals": {"find_vulnerable_services"},
        }
        state = InteractiveState(facts=facts, trace=TraceState())
        
        with patch("agent.graph.nodes.handle_unavailable_tools.are_scope_goals_achieved") as mock_scope:
            mock_scope.return_value = True
            
            result = await handle_unavailable_tools_node(state)
            
            # Should finalize
            assert "finalize" in result["facts"]["decision_history"][-1].lower()
            assert "tool_gaps" in result["facts"]["metadata"]

    @pytest.mark.asyncio
    async def test_fallback_to_alternative_capability(self):
        """Test fallback to alternative capability when available."""
        from agent.graph.nodes.handle_unavailable_tools import handle_unavailable_tools_node
        
        facts = FactsState(
            task_id=1,
            message="test",
            capability="vuln_scan",
        )
        facts.metadata = {}
        state = InteractiveState(facts=facts, trace=TraceState())
        
        with patch("agent.graph.nodes.handle_unavailable_tools.are_scope_goals_achieved") as mock_scope:
            with patch("agent.graph.nodes.handle_unavailable_tools.get_fallback_capability") as mock_fallback:
                with patch("agent.graph.nodes.handle_unavailable_tools.are_tools_available") as mock_available:
                    mock_scope.return_value = False
                    mock_fallback.return_value = CapabilityType.SERVICE_ENUM
                    mock_available.return_value = True  # Fallback has tools
                    
                    result = await handle_unavailable_tools_node(state)
                    
                    # Should update capability and replan
                    assert result["facts"]["capability"] == "service_enum"
                    assert "planner" in result["facts"]["decision_history"][-1].lower()
                    assert "capability_fallbacks" in result["facts"]["metadata"]

    @pytest.mark.asyncio
    async def test_finalize_with_limitations_when_no_fallback(self):
        """Test finalize with limitations when no fallback available."""
        from agent.graph.nodes.handle_unavailable_tools import handle_unavailable_tools_node
        
        facts = FactsState(
            task_id=1,
            message="test",
            capability="vuln_scan",
        )
        facts.metadata = {}
        state = InteractiveState(facts=facts, trace=TraceState())
        
        with patch("agent.graph.nodes.handle_unavailable_tools.are_scope_goals_achieved") as mock_scope:
            with patch("agent.graph.nodes.handle_unavailable_tools.get_fallback_capability") as mock_fallback:
                with patch("agent.graph.nodes.handle_unavailable_tools.are_tools_available") as mock_available:
                    mock_scope.return_value = False
                    mock_fallback.return_value = None  # No fallback
                    mock_available.return_value = False
                    
                    result = await handle_unavailable_tools_node(state)
                    
                    # Should finalize with limitations
                    assert "finalize" in result["facts"]["decision_history"][-1].lower()
                    assert "tool_gaps" in result["facts"]["metadata"]
                    assert "limitations" in result["facts"]["metadata"]


class TestPlannerToolAvailability:
    """Test planner tool availability checks."""

    @pytest.mark.asyncio
    async def test_planner_skips_when_no_tools(self):
        """Test planner skips planning when no tools available."""
        from agent.graph.nodes.planner import planner_node
        
        facts = FactsState(
            task_id=1,
            message="scan for vulnerabilities",
            capability="vuln_scan",
        )
        facts.metadata = {}
        state = InteractiveState(facts=facts, trace=TraceState())
        
        with patch("agent.graph.nodes.planner_setup.are_tools_available") as mock_available:
            mock_available.return_value = False
            
            result = await planner_node(state)
            
            # Should route to handle_unavailable_tools
            assert "handle_unavailable_tools" in result["facts"]["decision_history"][-1]
            assert "tool_gaps" in result["facts"]["metadata"]

    def test_planner_includes_available_tools_in_prompt(self):
        """Test planner includes available tools in planning prompt."""
        from agent.graph.nodes.planner_prompting import build_planning_prompt
        
        available_tools = ["tool1", "tool2", "tool3"]
        prompt = build_planning_prompt(
            ["127.0.0.1"],
            {},
            available_tools=available_tools,
        )
        
        assert "Available Tools" in prompt
        assert "tool1" in prompt
        assert "Only plan steps that can be executed with the available tools" in prompt


class TestToolGapReporting:
    """Test tool gap reporting in finalizer."""

    def test_finalizer_includes_tool_gaps(self):
        """Test finalizer includes tool gaps in final text."""
        from agent.graph.nodes.finalizer import finalize_turn
        
        facts = FactsState(
            task_id=1,
            message="test",
        )
        facts.metadata = {
            "tool_gaps": ["vuln_scan was requested but no tools available"],
            "limitations": ["Unable to perform vuln_scan due to missing tools"],
        }
        trace = TraceState()
        trace.final_text = "Initial summary"
        state = InteractiveState(facts=facts, trace=trace)
        
        result = finalize_turn(state)
        
        final_text = result["trace"]["final_text"]
        assert "Tool Availability Notes" in final_text
        assert "vuln_scan was requested" in final_text
        assert "Limitations" in final_text
        assert "Suggestions" in final_text

    def test_finalizer_includes_capability_fallbacks(self):
        """Test finalizer includes capability fallbacks in final text."""
        from agent.graph.nodes.finalizer import finalize_turn
        
        facts = FactsState(
            task_id=1,
            message="test",
        )
        facts.metadata = {
            "capability_fallbacks": ["vuln_scan → service_enum"],
        }
        trace = TraceState()
        trace.final_text = "Initial summary"
        state = InteractiveState(facts=facts, trace=trace)
        
        result = finalize_turn(state)
        
        final_text = result["trace"]["final_text"]
        assert "Capability Fallbacks" in final_text
        assert "vuln_scan → service_enum" in final_text

    def test_finalizer_no_gaps_no_notes(self):
        """Test finalizer doesn't add notes when no gaps."""
        from agent.graph.nodes.finalizer import finalize_turn
        
        facts = FactsState(
            task_id=1,
            message="test",
        )
        facts.metadata = {}
        trace = TraceState()
        trace.final_text = "Initial summary"
        state = InteractiveState(facts=facts, trace=trace)
        
        result = finalize_turn(state)
        
        final_text = result["trace"]["final_text"]
        assert "Tool Availability Notes" not in final_text
        assert final_text == "Initial summary"


class TestGetAvailableToolsForCapability:
    """Test getting available tools for a capability."""

    def test_get_available_tools_returns_list(self):
        """Test that get_available_tools_for_capability returns tool list."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = ["tool1", "tool2", "tool3"]
            
            tools = get_available_tools_for_capability(CapabilityType.PORT_SCAN)
            assert tools == ["tool1", "tool2", "tool3"]
            mock_resolve.assert_called_once()

    def test_get_available_tools_empty_when_none(self):
        """Test that empty list returned when no tools."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = []
            
            tools = get_available_tools_for_capability(CapabilityType.VULN_SCAN)
            assert tools == []

    def test_get_available_tools_passes_context(self):
        """Test that context is passed to resolve_tools."""
        with patch("agent.tools.resolve_tools.resolve_tools_for_capability") as mock_resolve:
            mock_resolve.return_value = []
            
            context = {"targets": ["127.0.0.1"]}
            get_available_tools_for_capability(
                CapabilityType.PORT_SCAN,
                context=context
            )
            
            mock_resolve.assert_called_once()
            call_args = mock_resolve.call_args
            assert call_args[1]["context"] == context


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
