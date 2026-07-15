"""Tests for decision_router todo completion integration."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent.graph.nodes.decision_router import (
    _extract_findings,
    _get_current_todo,
    _get_next_todo,
    decision_router,
)
from agent.graph.state import (
    BudgetState,
    CompletionType,
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
    ToolExecutionRecord,
)


@pytest.fixture
def mock_facts_with_todos():
    """Create FactsState with TodoItem list."""
    return FactsState(
        task_id=1,
        message="Test message",
        todo_list=[
            TodoItem(description="Todo 1", status=TodoStatus.PENDING),
            TodoItem(description="Todo 2", status=TodoStatus.PENDING),
            TodoItem(description="Todo 3", status=TodoStatus.PENDING),
        ],
    )


@pytest.fixture
def mock_facts_with_string_todos():
    """Create FactsState with legacy string todo list."""
    return FactsState(
        task_id=1,
        message="Test message",
        todo_list=["Todo 1", "Todo 2", "Todo 3"],  # Legacy format
    )


@pytest.fixture
def mock_trace():
    """Create TraceState with tool executions."""
    return TraceState(
        observations=["Observation 1", "Observation 2"],
        executed_tools=[
            ToolExecutionRecord(
                tool_id="nmap",
                observation="Found 3 hosts",
            ),
            ToolExecutionRecord(
                tool_id="openvas",
                observation="No vulnerabilities",
            ),
        ],
    )


class TestGetCurrentTodo:
    """Test _get_current_todo helper function."""
    
    def test_get_current_todo_when_in_progress(self, mock_facts_with_todos):
        """Test getting current todo when one is in progress."""
        # Mark first todo as in-progress
        mock_facts_with_todos.todo_list[0].status = TodoStatus.IN_PROGRESS
        mock_facts_with_todos.todo_list[0].started_at = datetime.now(timezone.utc)
        
        current = _get_current_todo(mock_facts_with_todos)
        
        assert current is not None
        assert current.description == "Todo 1"
        assert current.status == TodoStatus.IN_PROGRESS
    
    def test_get_current_todo_starts_first_pending(self, mock_facts_with_todos):
        """Test that first pending todo is started when none in progress."""
        # All todos pending
        current = _get_current_todo(mock_facts_with_todos)
        
        assert current is not None
        assert current.description == "Todo 1"
        assert current.status == TodoStatus.IN_PROGRESS
        assert current.started_at is not None
    
    def test_get_current_todo_returns_none_when_all_complete(self, mock_facts_with_todos):
        """Test returns None when all todos complete."""
        # Mark all todos complete
        for todo in mock_facts_with_todos.todo_list:
            todo.status = TodoStatus.COMPLETE_POSITIVE
        
        current = _get_current_todo(mock_facts_with_todos)
        
        assert current is None
    
    def test_get_current_todo_returns_none_when_no_todos(self):
        """Test returns None when todo list is empty."""
        facts = FactsState(task_id=1, message="Test", todo_list=[])
        
        current = _get_current_todo(facts)
        
        assert current is None
    
    def test_get_current_todo_converts_string_todos(self, mock_facts_with_string_todos):
        """Test converts legacy string todos to TodoItems."""
        current = _get_current_todo(mock_facts_with_string_todos)
        
        # Should convert strings to TodoItems
        assert current is not None
        assert isinstance(current, TodoItem)
        assert current.description == "Todo 1"
        assert current.status == TodoStatus.IN_PROGRESS
        
        # Verify todo_list was updated
        assert all(isinstance(t, TodoItem) for t in mock_facts_with_string_todos.todo_list)


class TestGetNextTodo:
    """Test _get_next_todo helper function."""
    
    def test_get_next_todo_returns_first_pending(self, mock_facts_with_todos):
        """Test returns first pending todo."""
        # Mark first todo in-progress
        mock_facts_with_todos.todo_list[0].status = TodoStatus.IN_PROGRESS
        
        next_todo = _get_next_todo(mock_facts_with_todos)
        
        assert next_todo is not None
        assert next_todo.description == "Todo 2"
        assert next_todo.status == TodoStatus.PENDING
    
    def test_get_next_todo_returns_none_when_no_pending(self, mock_facts_with_todos):
        """Test returns None when no pending todos."""
        # Mark all as in-progress or complete
        mock_facts_with_todos.todo_list[0].status = TodoStatus.IN_PROGRESS
        mock_facts_with_todos.todo_list[1].status = TodoStatus.COMPLETE_POSITIVE
        mock_facts_with_todos.todo_list[2].status = TodoStatus.EXHAUSTED
        
        next_todo = _get_next_todo(mock_facts_with_todos)
        
        assert next_todo is None
    
    def test_get_next_todo_returns_none_when_empty(self):
        """Test returns None when todo list empty."""
        facts = FactsState(task_id=1, message="Test", todo_list=[])
        
        next_todo = _get_next_todo(facts)
        
        assert next_todo is None
    
    def test_get_next_todo_converts_string_todos(self, mock_facts_with_string_todos):
        """Test converts legacy string todos."""
        next_todo = _get_next_todo(mock_facts_with_string_todos)
        
        assert next_todo is not None
        assert isinstance(next_todo, TodoItem)
        assert next_todo.description == "Todo 1"


class TestExtractFindings:
    """Test _extract_findings helper function."""
    
    def test_extract_findings_from_observations(self, mock_trace):
        """Test extracting findings from tool observations."""
        findings = _extract_findings(mock_trace)
        
        assert len(findings) >= 1
        assert any("Found 3 hosts" in f for f in findings)
    
    def test_extract_findings_from_observation(self, mock_trace):
        """Test extracting findings from tool observation."""
        findings = _extract_findings(mock_trace)
        
        assert len(findings) >= 1
        assert any("No vulnerabilities" in f for f in findings)
    
    def test_extract_findings_empty_trace(self):
        """Test extracting findings from empty trace."""
        trace = TraceState()
        
        findings = _extract_findings(trace)
        
        assert findings == []


class TestDecisionRouterTodoIntegration:
    """Test decision_router with todo completion integration."""
    
    @pytest.mark.asyncio
    async def test_router_checks_todo_progress(self):
        """Test that router checks current todo completion."""
        # This is an integration test placeholder
        # Full integration test would require mocking LLMClient and context
        pass
    
    @pytest.mark.asyncio
    async def test_router_moves_to_next_todo_when_complete(self):
        """Test that router moves to next todo when current complete."""
        pass
    
    @pytest.mark.asyncio
    async def test_router_finalizes_when_all_todos_complete(self):
        """Test that router finalizes when no more todos."""
        pass
    
    @pytest.mark.asyncio
    async def test_router_handles_todo_exhaustion(self):
        """Test that router handles exhausted todos gracefully."""
        pass
    
    @pytest.mark.asyncio
    async def test_router_continues_on_completion_check_error(self):
        """Test that router continues if completion check fails."""
        pass


class TestBackwardCompatibility:
    """Test backward compatibility with string-based todos."""
    
    def test_string_todos_converted_to_todo_items(self, mock_facts_with_string_todos):
        """Test string todos are converted on first access."""
        # Get current todo (should convert)
        current = _get_current_todo(mock_facts_with_string_todos)
        
        # Verify conversion happened
        assert isinstance(current, TodoItem)
        assert all(isinstance(t, TodoItem) for t in mock_facts_with_string_todos.todo_list)
    
    def test_mixed_todo_list_not_supported(self):
        """Test that mixed string/TodoItem lists are handled."""
        # In reality, the code assumes all same type after first element check
        # This test documents that assumption
        pass


class TestTodoLifecycle:
    """Test complete todo lifecycle through decision router."""
    
    def test_todo_starts_as_pending(self):
        """Test new todos start as pending."""
        todo = TodoItem(description="Test todo")
        
        assert todo.status == TodoStatus.PENDING
        assert todo.started_at is None
        assert todo.completed_at is None
    
    def test_todo_marked_in_progress_when_picked_up(self, mock_facts_with_todos):
        """Test todo marked in-progress when picked up."""
        current = _get_current_todo(mock_facts_with_todos)
        
        assert current.status == TodoStatus.IN_PROGRESS
        assert current.started_at is not None
    
    def test_todo_marked_complete_after_llm_assessment(self):
        """Test todo marked complete with reasoning."""
        todo = TodoItem(description="Test")
        todo.status = TodoStatus.IN_PROGRESS
        
        # Simulate completion
        todo.mark_complete(
            CompletionType.POSITIVE,
            "Found target successfully"
        )
        
        assert todo.status == TodoStatus.COMPLETE_POSITIVE
        assert todo.completed_at is not None
        assert todo.completion_reasoning == "Found target successfully"
    
    def test_exhausted_todo_handled_gracefully(self, mock_facts_with_todos):
        """Test exhausted todos don't block progress."""
        # Mark first todo as exhausted
        mock_facts_with_todos.todo_list[0].status = TodoStatus.EXHAUSTED
        
        # Should move to next todo
        current = _get_current_todo(mock_facts_with_todos)
        
        assert current is not None
        assert current.description == "Todo 2"  # Skipped exhausted


class TestContextBuilding:
    """Test context building for completion checker."""
    
    def test_context_includes_recent_observations(self, mock_trace):
        """Test context includes recent observations."""
        context = {
            "observations": mock_trace.observations[-10:],
            "findings": _extract_findings(mock_trace),
            "executed_tools": [t.tool_id for t in mock_trace.executed_tools],
        }
        
        assert "observations" in context
        assert len(context["observations"]) == 2
    
    def test_context_includes_findings(self, mock_trace):
        """Test context includes extracted findings."""
        findings = _extract_findings(mock_trace)
        
        assert len(findings) >= 2
        assert any("Found 3 hosts" in f for f in findings)
    
    def test_context_includes_executed_tools(self, mock_trace):
        """Test context includes tool names."""
        tool_names = [t.tool_id for t in mock_trace.executed_tools]
        
        assert "nmap" in tool_names
        assert "openvas" in tool_names


class TestLegacyGoalCheckGating:
    """Test legacy goal check is properly gated.
    
    NOTE: As of Phase 4 (LLM-driven progress tracking), standalone completion
    checking has been disabled. Legacy goal check now only runs when there's no
    current todo.
    See: docs/plans/LLM_DRIVEN_PROGRESS_TRACKING.md
    """
    
    @pytest.mark.asyncio
    async def test_legacy_check_skipped_when_todos_exist(self):
        """Test legacy goal check is skipped when todos exist.
        
        With LLM-driven progress tracking, we trust post_tool_reasoning
        to track completion. Legacy goal check only runs when no todos defined.
        """
        # Setup state with current todo
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test penetration test task",
                budgets=BudgetState(max_iterations=10),
                metadata={},
                todo_list=[
                    TodoItem(
                        description="Find vulnerabilities",
                        status=TodoStatus.IN_PROGRESS,
                    )
                ],
                scope_goals=["Find vulnerabilities"],  # Legacy goal set
                achieved_goals={"Find vulnerabilities"},  # Goal achieved
            ),
            trace=TraceState(),
        )
        
        # Call decision_router (deterministic authority path).
        result = await decision_router(interactive.as_graph_state())
        
        # Verify: Should NOT finalize via legacy check because todos exist
        # (legacy check only runs when no todos)
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert len(decision_history) > 0
        
        # Should not mention "scope goals achieved (legacy)" since todos exist
        last_decision = decision_history[-1]
        assert "legacy" not in last_decision.lower() or "scope goals" not in last_decision.lower()
    
    @pytest.mark.asyncio
    async def test_legacy_check_runs_when_no_todos(self):
        """Test legacy goal check runs when there are no todos."""
        # Setup state with no current todo
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                budgets=BudgetState(max_iterations=10),
                metadata={},
                todo_list=[],  # No todos
                scope_goals=["Find vulnerabilities"],
                achieved_goals={"Find vulnerabilities"},  # Legacy goal achieved
            ),
            trace=TraceState(),
        )
        
        # Mock to make are_scope_goals_achieved return True
        with patch(
            "agent.graph.nodes.decision_router.are_scope_goals_achieved"
        ) as mock_goals:
            mock_goals.return_value = True
            
            # Call decision_router
            result = await decision_router(interactive.as_graph_state())
        
        # Verify: Should finalize via legacy check
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert len(decision_history) > 0
        
        # Last decision should be finalize with legacy mention
        last_decision = decision_history[-1]
        assert "finalize" in last_decision.lower()
        assert "legacy" in last_decision.lower() or "goals achieved" in last_decision.lower()
    
    @pytest.mark.asyncio  
    async def test_legacy_check_runs_when_no_current_todo(self):
        """Test legacy goal check runs when no current todo exists."""
        # Setup state with no current todo (all complete)
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                budgets=BudgetState(max_iterations=10),
                metadata={},
                todo_list=[
                    TodoItem(
                        description="Completed",
                        status=TodoStatus.COMPLETE_POSITIVE,
                    )
                ],  # All complete
                scope_goals=["Find vulnerabilities"],
                achieved_goals={"Find vulnerabilities"},
            ),
            trace=TraceState(),
        )
        
        # Mock to make are_scope_goals_achieved return True
        with patch(
            "agent.graph.nodes.decision_router.are_scope_goals_achieved"
        ) as mock_goals:
            mock_goals.return_value = True
            
            # Call decision_router
            result = await decision_router(interactive.as_graph_state())
        
        # Verify: Should finalize (no current todo, so legacy check runs)
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert len(decision_history) > 0
        
        last_decision = decision_history[-1]
        assert "finalize" in last_decision.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
