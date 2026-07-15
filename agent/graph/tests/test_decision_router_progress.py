"""Tests for: Decision Router Progress Integration.

Tests the simplified decision router that trusts post_tool_reasoning:
- user_goal_achieved flag triggers finalization
- All todos complete triggers finalization
- TodoCompletionChecker is disabled"""

import pytest

from agent.graph.nodes.decision_router import (
    decision_router,
    _check_all_todos_complete,
    _record_decision,
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
def base_state() -> InteractiveState:
    """Create a base state for testing."""
    facts = FactsState(
        task_id=123,
        message="Test message for scanning",
        conversation_id="conv-123",
        capability="deep_reasoning",
        todo_list=[
            TodoItem(description="Discover hosts", status=TodoStatus.PENDING),
            TodoItem(description="Port scan", status=TodoStatus.PENDING),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def state_with_goal_achieved(base_state: InteractiveState) -> InteractiveState:
    """Create state where user_goal_achieved is set."""
    base_state.facts.metadata["user_goal_achieved"] = True
    return base_state


@pytest.fixture
def state_with_all_todos_complete() -> InteractiveState:
    """Create state where all todos are complete."""
    facts = FactsState(
        task_id=123,
        message="Test message",
        conversation_id="conv-123",
        todo_list=[
            TodoItem(
                description="Discover hosts",
                status=TodoStatus.COMPLETE_POSITIVE,
                completion_type=CompletionType.POSITIVE,
            ),
            TodoItem(
                description="Port scan",
                status=TodoStatus.COMPLETE_POSITIVE,
                completion_type=CompletionType.POSITIVE,
            ),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.fixture
def state_with_partial_todos() -> InteractiveState:
    """Create state where some todos are complete."""
    facts = FactsState(
        task_id=123,
        message="Test message",
        conversation_id="conv-123",
        todo_list=[
            TodoItem(
                description="Discover hosts",
                status=TodoStatus.COMPLETE_POSITIVE,
                completion_type=CompletionType.POSITIVE,
            ),
            TodoItem(description="Port scan", status=TodoStatus.IN_PROGRESS),
            TodoItem(description="Enumerate", status=TodoStatus.PENDING),
        ],
        metadata={},
    )
    return InteractiveState(facts=facts, trace=TraceState())


# -----------------------------------------------------------------------------
# Tests: _check_all_todos_complete
# -----------------------------------------------------------------------------


class TestCheckAllTodosComplete:
    """Tests for _check_all_todos_complete function."""
    
    def test_empty_list_returns_false(self):
        """Empty todo list should return False."""
        facts = FactsState(
            task_id=123,
            message="Test",
            conversation_id="conv-123",
            todo_list=[],
            metadata={},
        )
        
        assert _check_all_todos_complete(facts) is False
    
    def test_string_todos_return_false(self):
        """String todos (legacy) should return False."""
        facts = FactsState(
            task_id=123,
            message="Test",
            conversation_id="conv-123",
            todo_list=["Task 1", "Task 2"],
            metadata={},
        )
        
        assert _check_all_todos_complete(facts) is False
    
    def test_all_complete_returns_true(self, state_with_all_todos_complete: InteractiveState):
        """All completed todos should return True."""
        assert _check_all_todos_complete(state_with_all_todos_complete.facts) is True
    
    def test_partial_complete_returns_false(self, state_with_partial_todos: InteractiveState):
        """Partially completed todos should return False."""
        assert _check_all_todos_complete(state_with_partial_todos.facts) is False
    
    def test_pending_todos_return_false(self, base_state: InteractiveState):
        """All pending todos should return False."""
        assert _check_all_todos_complete(base_state.facts) is False
    
    def test_mixed_completion_types_returns_true(self):
        """Mixed completion types (positive, negative, exhausted) should all count as complete."""
        facts = FactsState(
            task_id=123,
            message="Test",
            conversation_id="conv-123",
            todo_list=[
                TodoItem(
                    description="Task 1",
                    status=TodoStatus.COMPLETE_POSITIVE,
                    completion_type=CompletionType.POSITIVE,
                ),
                TodoItem(
                    description="Task 2",
                    status=TodoStatus.COMPLETE_NEGATIVE,
                    completion_type=CompletionType.NEGATIVE,
                ),
                TodoItem(
                    description="Task 3",
                    status=TodoStatus.EXHAUSTED,
                    completion_type=CompletionType.EXHAUSTED,
                ),
            ],
            metadata={},
        )
        
        assert _check_all_todos_complete(facts) is True


# -----------------------------------------------------------------------------
# Tests: Goal Achievement Routing
# -----------------------------------------------------------------------------


class TestGoalAchievementRouting:
    """Tests for user_goal_achieved routing."""
    
    @pytest.mark.asyncio
    async def test_user_goal_achieved_finalizes(
        self,
        state_with_goal_achieved: InteractiveState,
    ):
        """user_goal_achieved=True should route to finalize."""
        result = await decision_router(state_with_goal_achieved.as_graph_state())
        
        # Check decision was recorded
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert len(decision_history) > 0
        assert "finalize" in decision_history[-1].lower()
        assert "user goal achieved" in decision_history[-1].lower()
    
    @pytest.mark.asyncio
    async def test_goal_flag_cleared_after_use(
        self,
        state_with_goal_achieved: InteractiveState,
    ):
        """user_goal_achieved flag should be cleared after routing."""
        result = await decision_router(state_with_goal_achieved.as_graph_state())
        
        # Flag should be cleared from metadata
        metadata = result.get("facts", {}).get("metadata", {})
        assert metadata.get("user_goal_achieved") is None
    
    @pytest.mark.asyncio
    async def test_all_todos_complete_finalizes(
        self,
        state_with_all_todos_complete: InteractiveState,
    ):
        """All todos in completed state should route to finalize."""
        result = await decision_router(state_with_all_todos_complete.as_graph_state())
        
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert len(decision_history) > 0
        assert "finalize" in decision_history[-1].lower()
        assert "todos complete" in decision_history[-1].lower()
    
    @pytest.mark.asyncio
    async def test_goal_achieved_takes_priority_over_todos_check(self):
        """user_goal_achieved should be checked before all todos complete."""
        # Create state where both conditions are true
        facts = FactsState(
            task_id=123,
            message="Test",
            conversation_id="conv-123",
            todo_list=[
                TodoItem(
                    description="Task 1",
                    status=TodoStatus.COMPLETE_POSITIVE,
                ),
            ],
            metadata={"user_goal_achieved": True},
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        result = await decision_router(state.as_graph_state())
        
        # Should mention user goal achieved, not todos complete
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert "user goal achieved" in decision_history[-1].lower()


class TestPlannerEntrypointRouting:
    """Tests for planner handoff into the shared execution loop."""

    @pytest.mark.asyncio
    async def test_plan_ready_routes_to_call_tool_before_candidate_resolution(self):
        """Planner handoff should not finalize with candidate_missing."""
        facts = FactsState(
            task_id=123,
            message="Check whether SSH is exposed",
            conversation_id="conv-123",
            capability="deep_reasoning",
            todo_list=[
                TodoItem(
                    description="Run a bounded SSH exposure check",
                    status=TodoStatus.IN_PROGRESS,
                ),
            ],
            metadata={
                "planner_mode": "plan_ready",
            },
        )
        state = InteractiveState(facts=facts, trace=TraceState())

        result = await decision_router(state.as_graph_state())

        metadata = result["facts"]["metadata"]
        assert metadata["router_outcome"]["action"] == "call_tool"
        assert metadata["router_outcome"]["reason"] == "planner_entrypoint_start_execution"
        assert metadata["router_outcome"]["candidate_source"] == "planner_entrypoint"
        assert metadata["planner_entrypoint_consumed"] is True
        assert "bootstrap_entrypoint_consumed" not in metadata

    @pytest.mark.asyncio
    async def test_consumed_planner_entrypoint_does_not_reuse_planner_source(self):
        """A consumed planner entrypoint must fall back to existing route authority."""
        facts = FactsState(
            task_id=123,
            message="Check whether SSH is exposed",
            conversation_id="conv-123",
            capability="deep_reasoning",
            todo_list=[
                TodoItem(
                    description="Run a bounded SSH exposure check",
                    status=TodoStatus.IN_PROGRESS,
                ),
            ],
            metadata={
                "planner_mode": "plan_ready",
                "planner_entrypoint_consumed": True,
            },
            decision_history=["think_more: prior PTR candidate"],
        )
        state = InteractiveState(facts=facts, trace=TraceState())

        result = await decision_router(state.as_graph_state())

        metadata = result["facts"]["metadata"]
        assert metadata["router_outcome"]["candidate_source"] == "legacy_compatibility"
        assert metadata["router_outcome"]["reason"] == "decision_history_fallback"
        assert metadata["planner_entrypoint_consumed"] is True

    @pytest.mark.asyncio
    async def test_plan_ready_without_work_finalizes(self):
        """Planner handoff without executable work fails closed."""
        facts = FactsState(
            task_id=123,
            message="Pentest it",
            conversation_id="conv-123",
            capability="deep_reasoning",
            todo_list=[],
            metadata={
                "planner_mode": "plan_ready",
            },
        )
        state = InteractiveState(facts=facts, trace=TraceState())

        result = await decision_router(state.as_graph_state())

        metadata = result["facts"]["metadata"]
        assert metadata["router_outcome"]["action"] == "finalize"
        assert metadata["router_outcome"]["reason"] == "planner_entrypoint_no_executable_work"
        assert metadata["router_outcome"]["candidate_source"] == "planner_entrypoint"
        assert metadata["planner_entrypoint_consumed"] is True
        assert "bootstrap_entrypoint_consumed" not in metadata


# -----------------------------------------------------------------------------
# Tests: TodoCompletionChecker Disabled
# -----------------------------------------------------------------------------


class TestTodoCompletionCheckerDisabled:
    """Tests verifying TodoCompletionChecker is disabled."""
    
    @pytest.mark.asyncio
    async def test_no_completion_checker_import_used(self, base_state: InteractiveState):
        """TodoCompletionChecker should not be imported or used."""
        # This test verifies the import was removed
        from agent.graph.nodes import decision_router as router_module
        
        # Check that TodoCompletionChecker is not in the module namespace
        assert not hasattr(router_module, 'TodoCompletionChecker')
    
    @pytest.mark.asyncio  
    async def test_partial_todos_dont_trigger_completion_check(
        self,
        state_with_partial_todos: InteractiveState,
    ):
        """Partial todos should NOT trigger old completion checker logic."""
        # With deterministic routing, partial todos should proceed without any
        # completion-checker or router LLM dependency.
        result = await decision_router(state_with_partial_todos.as_graph_state())

        metadata = result.get("facts", {}).get("metadata", {})
        outcome = metadata.get("router_outcome", {})
        assert outcome.get("action") in {"call_tool", "think_more", "reflect", "finalize"}
        assert outcome.get("reason")


# -----------------------------------------------------------------------------
# Tests: Integration with Post-Tool Reasoning
# -----------------------------------------------------------------------------


class TestIntegrationWithPostToolReasoning:
    """Tests for integration between decision_router and post_tool_reasoning."""
    
    @pytest.mark.asyncio
    async def test_respects_post_tool_reasoning_decision(self):
        """decision_router should respect the decision from post_tool_reasoning."""
        # Create state where post_tool_reasoning marked goal as achieved
        facts = FactsState(
            task_id=123,
            message="Scan network for hosts",
            conversation_id="conv-123",
            todo_list=[
                TodoItem(description="Find hosts", status=TodoStatus.IN_PROGRESS),
            ],
            metadata={
                "user_goal_achieved": True,
                "last_post_tool_action": "finalize",
            },
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        result = await decision_router(state.as_graph_state())
        
        # Should finalize based on post_tool_reasoning's assessment
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert "finalize" in decision_history[-1].lower()
    
    @pytest.mark.asyncio
    async def test_budget_exhaustion_still_works(self):
        """Budget exhaustion guardrails should still work."""
        facts = FactsState(
            task_id=123,
            message="Test",
            conversation_id="conv-123",
            iterations=20,  # Exceeds default max of 15
            budgets=FactsState.model_fields["budgets"].default_factory(),
            todo_list=[
                TodoItem(description="Task", status=TodoStatus.PENDING),
            ],
            metadata={},
        )
        state = InteractiveState(facts=facts, trace=TraceState())
        
        result = await decision_router(state.as_graph_state())
        
        decision_history = result.get("facts", {}).get("decision_history", [])
        assert "finalize" in decision_history[-1].lower()
        assert "budget" in decision_history[-1].lower() or "max iterations" in decision_history[-1].lower()
