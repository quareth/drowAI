"""Tests for post_tool_reasoning: Graph Integration.

Tests cover:
- Graph builder integrates post_tool_reasoning correctly
- Routing from observation_adapter based on decision
- Old nodes/files removed
- Full flow from tool_synthesizer → post_tool_reasoning → observation_adapter → route"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.graph.builders.deep_reasoning_builder import _route_decision
from agent.graph.state import (
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
)


def _route_from_post_tool_decision(state: InteractiveState) -> str:
    """Compatibility helper that routes through decision_router topology."""
    metadata = state.facts.metadata or {}
    if metadata.get("user_goal_achieved") or metadata.get("request_contract_terminal"):
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
# Tests: Graph Structure
# -----------------------------------------------------------------------------


class TestGraphStructure:
    """Tests to verify graph builder includes post_tool_reasoning correctly."""
    
    def test_graph_imports_post_tool_reasoning(self):
        """Graph builder should import post_tool_reasoning, not observation_articulation."""
        # This test verifies the import structure is correct
        from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
        
        # Should not raise ImportError
        graph = build_deep_reasoning_graph()
        assert graph is not None
    
    def test_graph_has_post_tool_reasoning_node(self):
        """Graph should have post_tool_reasoning node."""
        from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
        
        graph = build_deep_reasoning_graph()
        # StateGraph stores nodes in _nodes attribute
        nodes = getattr(graph, "nodes", None) or getattr(graph, "_nodes", {})
        assert "post_tool_reasoning" in nodes
    
    def test_graph_does_not_have_observation_articulation_node(self):
        """Graph should NOT have observation_articulation node (removed)."""
        from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
        
        graph = build_deep_reasoning_graph()
        nodes = getattr(graph, "nodes", None) or getattr(graph, "_nodes", {})
        assert "observation_articulation" not in nodes


# -----------------------------------------------------------------------------
# Tests: Routing Function
# -----------------------------------------------------------------------------


class TestRouteFromPostToolDecision:
    """Tests for _route_from_post_tool_decision routing function."""
    
    def test_routes_call_tool_to_select_categories(self):
        """call_tool decision should route to select_categories."""
        state = _create_state_with_decision("call_tool: Need more data")
        result = _route_from_post_tool_decision(state)
        assert result == "select_categories"
    
    def test_routes_think_more(self):
        """think_more decision should route to think_more."""
        state = _create_state_with_decision("think_more: Analyzing findings")
        result = _route_from_post_tool_decision(state)
        assert result == "think_more"
    
    def test_routes_reflect(self):
        """reflect decision should route to reflect."""
        state = _create_state_with_decision("reflect: Strategy not working")
        result = _route_from_post_tool_decision(state)
        assert result == "reflect"
    
    def test_routes_finalize(self):
        """finalize decision should route to finalize."""
        state = _create_state_with_decision("finalize: Goal achieved")
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_routes_synthesis_label_to_finalize_as_invalid_ptr_action(self):
        """``synthesis`` is not a PTR action — falls through to ``finalize``.

        The PTR-facing post-tool route uses the four-action PTR contract
        (no ``synthesis``); a manually inserted ``"synthesis: ..."`` PTR
        decision routes to the terminal default.
        """
        state = _create_state_with_decision("synthesis: Stuck in loop")
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_routes_to_finalize_on_empty_history(self):
        """Empty decision history should route to finalize."""
        state = _create_state_with_decision(None)  # No decision
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_routes_to_finalize_on_unknown_action(self):
        """Unknown action should route to finalize."""
        state = _create_state_with_decision("unknown_action: Some reasoning")
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_handles_action_without_reasoning(self):
        """Action without colon separator should still route correctly."""
        # Some decision formats may not have the colon separator
        state = _create_state_with_decision("call_tool")
        result = _route_from_post_tool_decision(state)
        assert result == "select_categories"


def _create_state_with_decision(decision: str | None) -> InteractiveState:
    """Helper to create an ``InteractiveState`` with a decision in history.

    Phase 2 of the LangGraph DRY migration converted deep-reasoning route
    handlers to take a typed ``InteractiveState`` directly (the graph wires
    them through ``with_interactive_state(...)``). Tests therefore call the
    handlers with the typed object rather than a raw graph state mapping.
    """
    facts = FactsState(
        task_id=123,
        message="Test task",
        capability="deep_reasoning",
        decision_history=[decision] if decision else [],
    )
    return InteractiveState(facts=facts)


# -----------------------------------------------------------------------------
# Tests: Old Files Removed
# -----------------------------------------------------------------------------


class TestOldFilesRemoved:
    """Tests to verify old files are properly removed."""
    
    def test_observation_articulation_file_removed(self):
        """tool_observation_articulation.py should be removed."""
        import importlib.util
        
        spec = importlib.util.find_spec("agent.graph.nodes.tool_observation_articulation")
        assert spec is None, "tool_observation_articulation.py should be deleted"
    
    def test_observation_articulation_prompts_removed(self):
        """legacy prompt package should be removed."""
        import importlib.util

        package_name = ".".join(["agent", "graph", "prompts"])
        spec = importlib.util.find_spec(package_name)
        assert spec is None, "legacy prompt package should be deleted"
    
    def test_old_test_file_removed(self):
        """test_tool_observation_articulation.py should be removed."""
        import importlib.util
        
        spec = importlib.util.find_spec("agent.tests.test_tool_observation_articulation")
        assert spec is None, "test_tool_observation_articulation.py should be deleted"


# -----------------------------------------------------------------------------
# Tests: Node __init__ Exports
# -----------------------------------------------------------------------------


class TestNodeExports:
    """Tests to verify nodes/__init__.py exports are correct."""
    
    def test_post_tool_reasoning_exported(self):
        """post_tool_reasoning should be exported from nodes."""
        from agent.graph.nodes import post_tool_reasoning
        
        assert callable(post_tool_reasoning)
    
    def test_articulate_tool_observation_not_exported(self):
        """articulate_tool_observation should NOT be exported (removed)."""
        import agent.graph.nodes as nodes_module
        
        assert not hasattr(nodes_module, "articulate_tool_observation")


# -----------------------------------------------------------------------------
# Tests: Observation Adapter Compatibility
# -----------------------------------------------------------------------------


class TestObservationAdapterCompatibility:
    """Tests to verify observation_adapter works with post_tool_reasoning flags."""
    
    @pytest.mark.asyncio
    async def test_adapter_recognizes_observation_streamed_flag(self):
        """Adapter should recognize observation_streamed flag from post_tool_reasoning."""
        from agent.graph.nodes.observation_adapter import adapt_to_observations
        
        facts = FactsState(
            task_id=123,
            message="Test task",
            capability="deep_reasoning",
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "observation_text": "Test observation from post_tool_reasoning",
                },
                "observation_streamed": True,  # New flag from post_tool_reasoning
            },
        )
        state = InteractiveState(facts=facts)
        
        result = await adapt_to_observations(state.as_graph_state())
        
        # Should not re-stream since already_streamed is True
        # Just verify it doesn't crash and processes correctly
        assert "trace" in result
        assert "facts" in result
    
    @pytest.mark.asyncio
    async def test_adapter_recognizes_legacy_flag(self):
        """Adapter should still recognize legacy articulated_observation_streamed flag."""
        from agent.graph.nodes.observation_adapter import adapt_to_observations
        
        facts = FactsState(
            task_id=123,
            message="Test task",
            capability="deep_reasoning",
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "observation_text": "Test observation",
                },
                "articulated_observation_streamed": True,  # Legacy flag
            },
        )
        state = InteractiveState(facts=facts)
        
        result = await adapt_to_observations(state.as_graph_state())
        
        assert "trace" in result
        assert "facts" in result
    
    @pytest.mark.asyncio
    async def test_adapter_clears_both_streaming_flags(self):
        """Adapter should clear both streaming flags after processing."""
        from agent.graph.nodes.observation_adapter import adapt_to_observations
        
        facts = FactsState(
            task_id=123,
            message="Test task",
            capability="deep_reasoning",
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "observation_text": "Test observation",
                },
                "observation_streamed": True,
                "articulated_observation_streamed": True,
            },
        )
        state = InteractiveState(facts=facts)
        
        result = await adapt_to_observations(state.as_graph_state())
        
        metadata = result["facts"]["metadata"]
        assert "observation_streamed" not in metadata
        assert "articulated_observation_streamed" not in metadata


# -----------------------------------------------------------------------------
# Tests: Full Flow Simulation
# -----------------------------------------------------------------------------


class TestFullFlowSimulation:
    """Simulated tests for the full tool → reasoning → adapter → route flow."""
    
    @pytest.mark.asyncio
    async def test_post_tool_reasoning_decision_flows_to_routing(self):
        """Decision from post_tool_reasoning should be available for routing."""
        from agent.graph.nodes.post_tool_reasoning import (
            PostToolReasoningOutput,
            _record_decision,
            _record_observation,
        )
        # Create state
        facts = FactsState(
            task_id=123,
            message="Test task",
            capability="deep_reasoning",
            decision_history=[],
            iterations=2,
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Found open ports",
                },
            },
        )
        interactive = InteractiveState(facts=facts)
        
        # Simulate post_tool_reasoning output
        output = PostToolReasoningOutput(
            observation="I found ports 22 and 80 open. This reveals SSH and HTTP services. I should enumerate the web server next.",
            next_action="call_tool",
            action_reasoning="Web server detected, need directory enumeration.",
        )
        
        # Record decision (as post_tool_reasoning would)
        _record_decision(interactive, output)
        _record_observation(interactive, output)

        # Migrated route handlers accept ``InteractiveState`` directly; the
        # graph wires them through ``with_interactive_state(...)``.
        # Route should go to select_categories (for call_tool)
        route = _route_from_post_tool_decision(interactive)
        assert route == "select_categories"
    
    @pytest.mark.asyncio
    async def test_observation_persists_through_adapter(self):
        """Observation from post_tool_reasoning should persist after adapter."""
        from agent.graph.nodes.post_tool_reasoning import (
            PostToolReasoningOutput,
            _record_decision,
            _record_observation,
        )
        from agent.graph.nodes.observation_adapter import adapt_to_observations
        
        # Create state with post_tool_reasoning output
        facts = FactsState(
            task_id=123,
            message="Test task",
            capability="deep_reasoning",
            decision_history=[],
            iterations=2,
            metadata={
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Found open ports",
                    "key_findings": ["Port 22", "Port 80"],
                },
            },
        )
        interactive = InteractiveState(facts=facts)
        
        # Simulate post_tool_reasoning
        output = PostToolReasoningOutput(
            observation="Test observation for flow verification.",
            next_action="finalize",
            action_reasoning="Task complete.",
        )
        _record_decision(interactive, output)
        _record_observation(interactive, output)
        
        # Get state after post_tool_reasoning
        state_after_reasoning = interactive.as_graph_state()
        
        # Pass through observation_adapter
        result = await adapt_to_observations(state_after_reasoning)
        
        # Decision history should still be intact for routing
        decision_history = result["facts"]["decision_history"]
        assert len(decision_history) == 1
        assert "finalize" in decision_history[-1]
        
        # Observation should be in trace
        observations = result["trace"]["observations"]
        assert any("Test observation" in obs for obs in observations)

    @pytest.mark.asyncio
    async def test_ptr_decision_reentry_routes_to_call_tool_dispatch(self):
        """PTR decision should survive adapter and dispatch via router authority."""
        from agent.graph.nodes.decision_router.helpers import extract_action_label
        from agent.graph.nodes.decision_router.router import decision_router
        from agent.graph.nodes.observation_adapter import adapt_to_observations
        from agent.graph.nodes.post_tool_reasoning import (
            PostToolReasoningOutput,
            _record_decision,
            _record_observation,
        )

        facts = FactsState(
            task_id=321,
            message="Test task",
            capability="deep_reasoning",
            iterations=2,
            decision_history=[],
            todo_list=[TodoItem(description="Continue probing", status=TodoStatus.IN_PROGRESS)],
            metadata={
                "turn_sequence": 11,
                "phase_sequence": 4,
                "runtime_budgets": {
                    "remaining_iterations": 5,
                    "remaining_tool_calls": 3,
                },
                "synthesized_output": {
                    "tool": "nmap",
                    "summary": "Open services detected",
                },
            },
        )
        interactive = InteractiveState(facts=facts)
        ptr_output = PostToolReasoningOutput(
            observation=(
                "I found reachable services and should run one more targeted "
                "enumeration step before finalizing."
            ),
            next_action="call_tool",
            action_reasoning="Need one follow-up tool execution.",
        )

        _record_decision(interactive, ptr_output)
        assert interactive.facts.get_candidate_decision() is not None
        _record_observation(interactive, ptr_output)

        after_adapter = await adapt_to_observations(interactive.as_graph_state())
        after_router = await decision_router(after_adapter)
        metadata = after_router["facts"]["metadata"]
        outcome = metadata["router_outcome"]

        assert outcome["action"] == "call_tool"
        assert outcome["reason"] == "candidate_decision_accepted"
        assert metadata.get("candidate_decision") is None
        # Dispatch edge currently reads decision_history; keep it aligned.
        assert extract_action_label(after_router["facts"]["decision_history"][-1]) == "call_tool"


# -----------------------------------------------------------------------------
# Tests: Error Cases
# -----------------------------------------------------------------------------


class TestErrorCases:
    """Tests for error handling in integration."""
    
    def test_routing_handles_malformed_decision(self):
        """Routing should handle malformed decision gracefully."""
        # Decision with no action before colon
        state = _create_state_with_decision(": just reasoning no action")
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"  # Should default to finalize
    
    def test_routing_handles_empty_string_decision(self):
        """Routing should handle empty string decision."""
        state = _create_state_with_decision("")
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"

