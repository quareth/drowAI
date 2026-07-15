"""Tests for: Progress Application Logic.

Tests the logic that applies LLM progress updates to state:
- _apply_progress_updates
- _build_progress_summary
- Integration with main post_tool_reasoning node"""

import pytest
from datetime import datetime, timezone

from agent.graph.nodes.post_tool_reasoning import (
    PostToolReasoningOutput,
    TodoProgress,
    _apply_progress_updates,
    _build_progress_summary,
)
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
def state_with_string_todos() -> InteractiveState:
    """Create state with legacy string todos."""
    facts = FactsState(
        task_id=123,
        message="Test message",
        conversation_id="conv-123",
        todo_list=[
            "Discover live hosts",
            "Port scan hosts",
            "Enumerate services",
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def state_with_todo_items() -> InteractiveState:
    """Create state with structured TodoItem objects."""
    facts = FactsState(
        task_id=123,
        message="Test message",
        conversation_id="conv-123",
        todo_list=[
            TodoItem(description="Discover live hosts", status=TodoStatus.PENDING),
            TodoItem(description="Port scan hosts", status=TodoStatus.PENDING),
            TodoItem(description="Enumerate services", status=TodoStatus.PENDING),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def output_with_completed_todo() -> PostToolReasoningOutput:
    """Create output marking a todo as completed."""
    return PostToolReasoningOutput(
        observation="Found live hosts on the network.",
        next_action="call_tool",
        action_reasoning="Need to scan ports next",
        user_goal_achieved=False,
        todo_progress=[
            TodoProgress(
                index=0,
                status="completed",
                completion_type="positive",
                completion_reason="Found 2 live hosts",
            ),
        ],
    )


@pytest.fixture
def output_with_skipped_todo() -> PostToolReasoningOutput:
    """Create output marking a todo as skipped."""
    return PostToolReasoningOutput(
        observation="Using fallback host instead.",
        next_action="call_tool",
        action_reasoning="Fallback triggered",
        user_goal_achieved=False,
        todo_progress=[
            TodoProgress(index=0, status="skipped", completion_reason="Used fallback host"),
        ],
    )


@pytest.fixture
def output_with_goal_achieved() -> PostToolReasoningOutput:
    """Create output marking goal as achieved."""
    return PostToolReasoningOutput(
        observation="All tasks complete.",
        next_action="finalize",
        action_reasoning="User goal satisfied",
        user_goal_achieved=True,
        todo_progress=[
            TodoProgress(
                index=0,
                status="completed",
                completion_type="positive",
                completion_reason="Hosts found",
            ),
            TodoProgress(
                index=1,
                status="completed",
                completion_type="positive",
                completion_reason="Ports scanned",
            ),
        ],
    )


# -----------------------------------------------------------------------------
# Tests: _apply_progress_updates
# -----------------------------------------------------------------------------


class TestApplyProgressUpdates:
    """Tests for _apply_progress_updates function."""
    
    def test_marks_todo_completed(
        self,
        state_with_todo_items: InteractiveState,
        output_with_completed_todo: PostToolReasoningOutput,
    ):
        """Should mark todo as complete with correct status and reason."""
        _apply_progress_updates(state_with_todo_items, output_with_completed_todo)
        
        todo = state_with_todo_items.facts.todo_list[0]
        assert todo.status == TodoStatus.COMPLETE_POSITIVE
        assert todo.completion_type == CompletionType.POSITIVE
        assert todo.completion_reasoning == "Found 2 live hosts"
        assert todo.completed_at is not None
    
    def test_marks_todo_skipped(
        self,
        state_with_todo_items: InteractiveState,
        output_with_skipped_todo: PostToolReasoningOutput,
    ):
        """Should mark todo as skipped (negative completion)."""
        _apply_progress_updates(state_with_todo_items, output_with_skipped_todo)
        
        todo = state_with_todo_items.facts.todo_list[0]
        assert todo.status == TodoStatus.COMPLETE_NEGATIVE
        assert todo.completion_type == CompletionType.NEGATIVE
        assert todo.completion_reasoning == "Skipped: Used fallback host"
    
    def test_marks_todo_in_progress(self, state_with_todo_items: InteractiveState):
        """Should mark todo as in progress with timestamp."""
        output = PostToolReasoningOutput(
            observation="Starting host scan.",
            next_action="call_tool",
            action_reasoning="Begin scanning",
            todo_progress=[
                TodoProgress(index=0, status="in_progress"),
            ],
        )
        
        _apply_progress_updates(state_with_todo_items, output)
        
        todo = state_with_todo_items.facts.todo_list[0]
        assert todo.status == TodoStatus.IN_PROGRESS
        assert todo.started_at is not None
    
    def test_invalid_index_warns_and_continues(
        self,
        state_with_todo_items: InteractiveState,
        caplog,
    ):
        """Invalid todo index should log warning but not crash."""
        import logging
        caplog.set_level(logging.WARNING)
        
        output = PostToolReasoningOutput(
            observation="Testing invalid index handling in progress updates.",
            next_action="call_tool",
            action_reasoning="Testing edge case",
            todo_progress=[
                TodoProgress(
                    index=99,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Invalid",
                ),
            ],
        )
        
        # Should not raise
        _apply_progress_updates(state_with_todo_items, output)
        
        # Should log warning
        assert any("invalid todo index 99" in record.message.lower() for record in caplog.records)
    
    def test_converts_string_todos_to_items(
        self,
        state_with_string_todos: InteractiveState,
        output_with_completed_todo: PostToolReasoningOutput,
    ):
        """Should convert string todos to TodoItem objects."""
        # Verify initial state is strings
        assert isinstance(state_with_string_todos.facts.todo_list[0], str)
        
        _apply_progress_updates(state_with_string_todos, output_with_completed_todo)
        
        # Should now be TodoItems
        assert isinstance(state_with_string_todos.facts.todo_list[0], TodoItem)
        assert state_with_string_todos.facts.todo_list[0].status == TodoStatus.COMPLETE_POSITIVE
    
    def test_updates_achieved_goals(
        self,
        state_with_todo_items: InteractiveState,
        output_with_completed_todo: PostToolReasoningOutput,
    ):
        """Should add completed todos to achieved_goals set."""
        _apply_progress_updates(state_with_todo_items, output_with_completed_todo)
        
        assert "Discover live hosts" in state_with_todo_items.facts.achieved_goals
    
    def test_skips_already_complete_todos(self, state_with_todo_items: InteractiveState):
        """Should not re-complete already completed todos."""
        # Pre-complete the first todo
        todo = state_with_todo_items.facts.todo_list[0]
        todo.mark_complete(CompletionType.POSITIVE, "Already done")
        original_completed_at = todo.completed_at
        
        output = PostToolReasoningOutput(
            observation="Testing that already complete todos are not re-completed.",
            next_action="call_tool",
            action_reasoning="Testing edge case",
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="New reason",
                ),
            ],
        )
        
        _apply_progress_updates(state_with_todo_items, output)
        
        # Reasoning should remain original
        assert todo.completion_reasoning == "Already done"
        assert todo.completed_at == original_completed_at
    
    def test_multiple_progress_updates(self, state_with_todo_items: InteractiveState):
        """Should handle multiple progress updates in one call."""
        output = PostToolReasoningOutput(
            observation="Testing multiple progress updates in a single iteration.",
            next_action="call_tool",
            action_reasoning="Testing multiple updates",
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Done",
                ),
                TodoProgress(index=1, status="in_progress"),
                TodoProgress(index=2, status="pending"),
            ],
        )
        
        _apply_progress_updates(state_with_todo_items, output)
        
        todos = state_with_todo_items.facts.todo_list
        assert todos[0].status == TodoStatus.COMPLETE_POSITIVE
        assert todos[1].status == TodoStatus.IN_PROGRESS
        assert todos[2].status == TodoStatus.PENDING  # Unchanged
    
    def test_empty_todo_list_is_noop(self):
        """Should handle empty todo list gracefully."""
        facts = FactsState(
            task_id=123,
            message="Test message",
            conversation_id="conv-123",
            todo_list=[],
            metadata={},
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        output = PostToolReasoningOutput(
            observation="Testing empty todo list handling in progress updates.",
            next_action="call_tool",
            action_reasoning="Testing edge case",
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Resolved in empty-todo guard test",
                ),
            ],
        )
        
        # Should not raise
        _apply_progress_updates(state, output)
    
    def test_empty_progress_list_is_noop(
        self,
        state_with_todo_items: InteractiveState,
    ):
        """Should handle empty progress list gracefully."""
        output = PostToolReasoningOutput(
            observation="Testing empty progress list handling in updates.",
            next_action="call_tool",
            action_reasoning="Testing edge case",
            todo_progress=[],
        )
        
        _apply_progress_updates(state_with_todo_items, output)
        
        # All should remain pending
        for todo in state_with_todo_items.facts.todo_list:
            assert todo.status == TodoStatus.PENDING


# -----------------------------------------------------------------------------
# Tests: _build_progress_summary
# -----------------------------------------------------------------------------


class TestBuildProgressSummary:
    """Tests for _build_progress_summary function."""
    
    def test_empty_when_no_progress(self):
        """Should return empty string when no progress updates."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary when there are no updates.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            user_goal_achieved=False,
            todo_progress=[],
        )
        
        result = _build_progress_summary(output)
        assert result == ""
    
    def test_includes_completed_todos(self):
        """Should include completed todo indices."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary includes completed todos.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Completed objective 0",
                ),
                TodoProgress(
                    index=2,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Completed objective 2",
                ),
            ],
        )
        
        result = _build_progress_summary(output)
        assert "Completed: todos [0, 2]" in result
    
    def test_includes_skipped_in_completed(self):
        """Should include skipped todos in completed count."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary includes skipped todos.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            todo_progress=[
                TodoProgress(
                    index=1,
                    status="skipped",
                    completion_reason="Skipped by alternate path",
                ),
            ],
        )
        
        result = _build_progress_summary(output)
        assert "Completed: todos [1]" in result
    
    def test_includes_in_progress_todos(self):
        """Should include in_progress todo indices."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary includes in progress todos.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            todo_progress=[
                TodoProgress(index=0, status="in_progress"),
            ],
        )
        
        result = _build_progress_summary(output)
        assert "In progress: todos [0]" in result
    
    def test_includes_goal_achieved(self):
        """Should include goal achieved indicator."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary includes goal achieved indicator.",
            next_action="finalize",
            action_reasoning="Task completed successfully",
            user_goal_achieved=True,
            todo_progress=[],
        )
        
        result = _build_progress_summary(output)
        assert "✅ User goal achieved" in result
    
    def test_includes_effective_next_goal(self):
        """Should include effective next goal."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary includes effective next goal.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            effective_next_goal="Port scanning phase",
            todo_progress=[],
        )
        
        result = _build_progress_summary(output)
        assert "Next goal: Port scanning phase" in result
    
    def test_combines_all_parts(self):
        """Should combine all parts with separator."""
        output = PostToolReasoningOutput(
            observation="Testing progress summary combines all parts together.",
            next_action="call_tool",
            action_reasoning="Testing summary",
            user_goal_achieved=False,
            todo_progress=[
                TodoProgress(
                    index=0,
                    status="completed",
                    completion_type="positive",
                    completion_reason="Completed summary objective",
                ),
                TodoProgress(index=1, status="in_progress"),
            ],
            effective_next_goal="Next phase",
        )
        
        result = _build_progress_summary(output)
        
        assert "Completed: todos [0]" in result
        assert "In progress: todos [1]" in result
        assert "Next goal: Next phase" in result
        assert " | " in result  # Separator


# -----------------------------------------------------------------------------
# Integration Tests
# -----------------------------------------------------------------------------


class TestProgressIntegration:
    """Integration tests for progress application in full flow."""
    
    def test_progress_with_goal_achieved_and_finalize(
        self,
        state_with_todo_items: InteractiveState,
        output_with_goal_achieved: PostToolReasoningOutput,
    ):
        """When goal achieved, todos should be marked complete."""
        _apply_progress_updates(state_with_todo_items, output_with_goal_achieved)
        
        todos = state_with_todo_items.facts.todo_list
        assert todos[0].is_complete()
        assert todos[1].is_complete()
        
        # achieved_goals should be updated
        assert "Discover live hosts" in state_with_todo_items.facts.achieved_goals
        assert "Port scan hosts" in state_with_todo_items.facts.achieved_goals
    
    def test_preserves_unaffected_todos(
        self,
        state_with_todo_items: InteractiveState,
        output_with_completed_todo: PostToolReasoningOutput,
    ):
        """Progress should only affect specified todos."""
        _apply_progress_updates(state_with_todo_items, output_with_completed_todo)
        
        todos = state_with_todo_items.facts.todo_list
        
        # First is completed
        assert todos[0].status == TodoStatus.COMPLETE_POSITIVE
        
        # Others unchanged
        assert todos[1].status == TodoStatus.PENDING
        assert todos[2].status == TodoStatus.PENDING

