"""End-to-End Tests for LLM-Driven Progress Tracking (Phases 5 & 6).

Tests the complete flow of progress tracking from post_tool_reasoning
through routing and finalization.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json

from agent.graph.builders.deep_reasoning_builder import _route_decision
from agent.graph.nodes.post_tool_reasoning import (
    PostToolReasoningOutput,
    TodoProgress,
    _apply_progress_updates,
    _parse_reasoning_response,
)


def _route_from_post_tool_decision(state: "InteractiveState") -> str:
    """Compatibility helper that routes via decision_router + DR dispatch."""
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
from agent.graph.state import (
    FactsState,
    InteractiveState,
    TraceState,
    TodoItem,
    TodoStatus,
    CompletionType,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def simple_scan_state() -> InteractiveState:
    """Create state for simple port scan scenario."""
    facts = FactsState(
        task_id=123,
        message="Scan 127.0.0.1 for open ports",
        conversation_id="conv-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        tool_parameters={"target": "127.0.0.1"},
        current_goal="Find open ports on target",
        plan=["Run port scan on 127.0.0.1"],
        todo_list=[
            TodoItem(description="Scan ports on 127.0.0.1", status=TodoStatus.IN_PROGRESS),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def fallback_scan_state() -> InteractiveState:
    """Create state for fallback path scenario."""
    facts = FactsState(
        task_id=456,
        message="Scan network for hosts, then scan ports. If no hosts found, use 127.0.0.1",
        conversation_id="conv-456",
        capability="deep_reasoning",
        selected_tool="nmap",
        current_goal="Discover hosts on network",
        plan=["Host discovery", "Port scan on hosts or fallback"],
        todo_list=[
            TodoItem(description="Discover network hosts", status=TodoStatus.IN_PROGRESS),
            TodoItem(description="Port scan discovered hosts", status=TodoStatus.PENDING),
        ],
        metadata={
            "user_scope": {
                "conditional_targets": {"fallback_host": "127.0.0.1"},
            }
        },
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def multi_step_state() -> InteractiveState:
    """Create state for multi-step scenario."""
    facts = FactsState(
        task_id=789,
        message="Find hosts, identify services, check for vulnerabilities",
        conversation_id="conv-789",
        capability="deep_reasoning",
        todo_list=[
            TodoItem(description="Host discovery", status=TodoStatus.PENDING),
            TodoItem(description="Service identification", status=TodoStatus.PENDING),
            TodoItem(description="Vulnerability assessment", status=TodoStatus.PENDING),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


# -----------------------------------------------------------------------------
# Tests: Graph Routing with Progress
# -----------------------------------------------------------------------------


class TestProgressRouting:
    """Tests for progress-based routing decisions."""
    
    def test_goal_achieved_routes_to_finalize(self):
        """user_goal_achieved should route to finalize."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={"user_goal_achieved": True},
                decision_history=["call_tool: Continue scanning"],  # Would route to call_tool
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        
        # Should finalize even though decision says call_tool
        assert result == "finalize"
    
    def test_goal_achieved_overrides_call_tool(self):
        """Goal achieved should override call_tool action."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={"user_goal_achieved": True},
                decision_history=["call_tool: Should continue but goal is done"],
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_no_goal_achieved_follows_decision(self):
        """Without goal achieved, should follow decision history."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={},
                decision_history=["call_tool: Continue scanning"],
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        assert result == "select_categories"  # call_tool maps to select_categories
    
    def test_finalize_action_routes_correctly(self):
        """finalize action should route to finalize."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={},
                decision_history=["finalize: Task complete"],
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"
    
    def test_think_more_routes_correctly(self):
        """think_more action should route to think_more."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={},
                decision_history=["think_more: Need to analyze results"],
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        assert result == "think_more"
    
    def test_empty_history_defaults_to_finalize(self):
        """Empty decision history should default to finalize."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                metadata={},
                decision_history=[],
            ),
            trace=TraceState(),
        )
        
        result = _route_from_post_tool_decision(state)
        assert result == "finalize"


# -----------------------------------------------------------------------------
# Tests: Scenario 1 - Normal Completion
# -----------------------------------------------------------------------------


class TestNormalCompletionScenario:
    """Tests for normal task completion flow."""
    
    def test_port_scan_complete_triggers_goal_achieved(self, simple_scan_state: InteractiveState):
        """Port scan completion should set user_goal_achieved."""
        # Simulate LLM response after successful port scan
        llm_response = """
I successfully scanned 127.0.0.1 and found ports 22 (SSH) and 5432 (PostgreSQL) open. 
The user's request to scan for open ports is complete.
===DECISION===
{
    "next_action": "finalize",
    "action_reasoning": "Port scan complete - found open ports as requested",
    "user_goal_achieved": true,
    "todo_progress": [{"index": 0, "status": "completed", "completion_type": "positive", "completion_reason": "Found 2 open ports"}]
}
"""
        output = _parse_reasoning_response(llm_response)
        
        assert output.user_goal_achieved is True
        assert output.next_action == "finalize"
        assert len(output.todo_progress) == 1
        assert output.todo_progress[0].status == "completed"
    
    def test_simple_goal_completion_flow(self, simple_scan_state: InteractiveState):
        """Simple goal should finalize when achieved."""
        output = PostToolReasoningOutput(
            observation="Port scan found SSH on 22 and PostgreSQL on 5432.",
            next_action="finalize",
            action_reasoning="User goal satisfied",
            user_goal_achieved=True,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Ports found",
                ),
            ],
        )
        
        # Apply progress
        _apply_progress_updates(simple_scan_state, output)
        
        # Verify todo marked complete
        assert simple_scan_state.facts.todo_list[0].status == TodoStatus.COMPLETE_POSITIVE
        
        # Simulate storing goal achieved
        simple_scan_state.facts.metadata["user_goal_achieved"] = True
        
        # Route should go to finalize
        route = _route_from_post_tool_decision(simple_scan_state)
        assert route == "finalize"


# -----------------------------------------------------------------------------
# Tests: Scenario 2 - Fallback Path
# -----------------------------------------------------------------------------


class TestFallbackPathScenario:
    """Tests for fallback path completion."""
    
    def test_fallback_triggers_finalization(self, fallback_scan_state: InteractiveState):
        """Fallback completion should finalize without looping."""
        # Simulate: No hosts found, used fallback, scanned 127.0.0.1
        output = PostToolReasoningOutput(
            observation=(
                "No hosts were found on the network scan. Using the fallback host 127.0.0.1 "
                "as specified by the user. Port scan on fallback completed - found SSH and PostgreSQL."
            ),
            next_action="finalize",
            action_reasoning="Fallback path completed successfully - user goal achieved",
            user_goal_achieved=True,
            todo_progress=[
                TodoProgress(index=0, status="skipped", completion_reason="No hosts found - used fallback"),
                TodoProgress(
                    index=1,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Port scan on fallback host done",
                ),
            ],
        )
        
        # Apply progress
        _apply_progress_updates(fallback_scan_state, output)
        
        # Verify todos
        todos = fallback_scan_state.facts.todo_list
        assert todos[0].status == TodoStatus.COMPLETE_NEGATIVE  # Skipped
        assert todos[1].status == TodoStatus.COMPLETE_POSITIVE  # Completed
        
        # Store goal achieved
        fallback_scan_state.facts.metadata["user_goal_achieved"] = True
        
        # Route should finalize, NOT loop back to host discovery
        route = _route_from_post_tool_decision(fallback_scan_state)
        assert route == "finalize"
    
    def test_fallback_response_parsing(self):
        """Fallback path response should parse correctly."""
        llm_response = """
The port scan on the fallback host 127.0.0.1 completed successfully since no network hosts were found.
I found SSH on port 22 and PostgreSQL on port 5432. The user's goal is satisfied via the fallback path.
===DECISION===
{
    "next_action": "finalize",
    "action_reasoning": "User goal satisfied via fallback - no need to retry host discovery",
    "user_goal_achieved": true,
    "todo_progress": [
        {"index": 0, "status": "skipped", "completion_reason": "Used fallback instead"},
        {"index": 1, "status": "completed", "completion_type": "positive", "completion_reason": "Port scan on fallback host complete"}
    ]
}
"""
        output = _parse_reasoning_response(llm_response)
        
        assert output.user_goal_achieved is True
        assert output.next_action == "finalize"
        assert len(output.todo_progress) == 2
        assert output.todo_progress[0].status == "skipped"
        assert output.todo_progress[1].status == "completed"


# -----------------------------------------------------------------------------
# Tests: Scenario 3 - Multi-Step Completion
# -----------------------------------------------------------------------------


class TestMultiStepScenario:
    """Tests for multi-step task tracking."""
    
    def test_incremental_todo_progress(self, multi_step_state: InteractiveState):
        """Todos should be marked complete incrementally."""
        # Step 1: Host discovery complete
        output1 = PostToolReasoningOutput(
            observation="Found 2 hosts on the network: 192.168.1.1 and 192.168.1.5.",
            next_action="call_tool",
            action_reasoning="Moving to service identification",
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Found 2 hosts",
                ),
                TodoProgress(index=1, status="in_progress"),
            ],
            effective_next_goal="Identify services on discovered hosts",
            tool_intent={"description": "Service scan", "target": "192.168.1.1,192.168.1.5"},
        )
        
        _apply_progress_updates(multi_step_state, output1)
        
        todos = multi_step_state.facts.todo_list
        assert todos[0].status == TodoStatus.COMPLETE_POSITIVE
        assert todos[1].status == TodoStatus.IN_PROGRESS
        assert todos[2].status == TodoStatus.PENDING
    
    def test_all_todos_complete_triggers_finalization(self, multi_step_state: InteractiveState):
        """All todos complete should trigger finalization."""
        # Mark all todos complete
        for todo in multi_step_state.facts.todo_list:
            todo.mark_complete(CompletionType.POSITIVE, "Task completed")
        
        # Create final output
        output = PostToolReasoningOutput(
            observation="All tasks complete. Found hosts, identified services, assessed vulnerabilities.",
            next_action="finalize",
            action_reasoning="All todos complete",
            user_goal_achieved=True,
            todo_progress=[
                TodoProgress(
                    index=2,
                    status="completed",
                    completion_type="negative",
                    completion_reason="No critical vulns found",
                ),
            ],
        )
        
        # Apply and check
        _apply_progress_updates(multi_step_state, output)
        multi_step_state.facts.metadata["user_goal_achieved"] = True
        
        route = _route_from_post_tool_decision(multi_step_state)
        assert route == "finalize"


# -----------------------------------------------------------------------------
# Tests: Edge Cases
# -----------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_goal_achieved_with_pending_todos_still_finalizes(self):
        """Goal achieved should finalize even if some todos pending."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                todo_list=[
                    TodoItem(description="Task 1", status=TodoStatus.COMPLETE_POSITIVE),
                    TodoItem(description="Task 2", status=TodoStatus.PENDING),  # Still pending
                ],
                metadata={"user_goal_achieved": True},
                decision_history=["finalize: Goal achieved via alternative path"],
            ),
            trace=TraceState(),
        )
        
        route = _route_from_post_tool_decision(state)
        assert route == "finalize"
    
    def test_consistency_override_works(self):
        """user_goal_achieved + non-finalize action should override to finalize."""
        # LLM says call_tool but also says goal achieved (contradiction)
        llm_response = """
Found the ports. I'll run another scan to be thorough.
===DECISION===
{
    "next_action": "call_tool",
    "action_reasoning": "Run extra scan",
    "user_goal_achieved": true,
    "tool_intent": {"description": "Extra scan"}
}
"""
        output = _parse_reasoning_response(llm_response)
        
        # Validation should override to finalize
        assert output.user_goal_achieved is True
        assert output.next_action == "finalize"  # Overridden
        assert "(Override: goal achieved)" in output.action_reasoning
    
    def test_empty_progress_doesnt_break(self):
        """Empty progress array should be handled gracefully."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-1",
                todo_list=[
                    TodoItem(description="Task", status=TodoStatus.IN_PROGRESS),
                ],
                metadata={},
            ),
            trace=TraceState(),
        )
        
        output = PostToolReasoningOutput(
            observation="Continuing analysis of the target system.",
            next_action="call_tool",
            action_reasoning="Need more data",
            user_goal_achieved=False,
            todo_progress=[],  # Empty
            tool_intent={"description": "Continue scan"},
        )
        
        # Should not crash
        _apply_progress_updates(state, output)
        
        # Todo should remain unchanged
        assert state.facts.todo_list[0].status == TodoStatus.IN_PROGRESS


# -----------------------------------------------------------------------------
# Tests: Performance - Single LLM Call
# -----------------------------------------------------------------------------


class TestSingleLLMCall:
    """Tests verifying we only use one LLM call per iteration."""
    
    def test_no_separate_completion_check(self):
        """Should NOT make separate TodoCompletionChecker calls."""
        # This is verified by the fact that we:
        # 1. Removed TodoCompletionChecker from imports
        # 2. Use progress from post_tool_reasoning directly
        
        from agent.graph.nodes import decision_router
        
        # Verify TodoCompletionChecker is not in the module
        assert not hasattr(decision_router, 'TodoCompletionChecker')
    
    def test_progress_comes_from_single_output(self):
        """All progress info should come from single PostToolReasoningOutput."""
        output = PostToolReasoningOutput(
            observation="Analysis complete with all requested data gathered.",
            next_action="finalize",
            action_reasoning="Task complete - user goal satisfied",
            user_goal_achieved=True,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Completed objective in single output",
                ),
            ],
            effective_next_goal=None,
        )
        
        # Single output contains: observation, action, progress, goal status
        assert output.observation is not None
        assert output.next_action is not None
        assert output.user_goal_achieved is True
        assert len(output.todo_progress) > 0
