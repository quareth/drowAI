"""Tests for agent-initiated pause logic.

Tests cover:
- Pause condition detection (_should_pause_for_confirmation)
- Pause request building (_build_pause_request)
- Response waiting logic (_emit_and_wait_for_pause_response)
- Integration with decision_router"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock

import pytest

from agent.config import AgentConfig
from agent.graph.nodes.decision_router import (
    _build_pause_request,
    _emit_and_wait_for_pause_response,
    _should_pause_for_confirmation,
    decision_router,
)
from agent.graph.state import (
    AgentPauseRequest,
    BudgetState,
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
)


class TestPauseConditionDetection:
    """Test _should_pause_for_confirmation() pause condition logic."""
    
    def test_many_todos_remaining(self):
        """Test pause triggered when many todos remaining."""
        config = AgentConfig()
        config.pause_min_remaining_todos = 5
        
        # Create state with 5 pending todos
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(5)
                ],
            ),
            trace=TraceState(),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is True
        assert "many_todos_remaining" in reason
        assert "5 pending" in reason
    
    def test_not_enough_todos_for_pause(self):
        """Test no pause when todos below threshold."""
        config = AgentConfig()
        config.pause_min_remaining_todos = 5
        
        # Only 3 pending todos
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(3)
                ],
            ),
            trace=TraceState(),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is False
        assert reason is None
    
    def test_context_length_threshold(self):
        """Test pause triggered when context too long."""
        config = AgentConfig()
        config.pause_context_length_threshold = 10
        
        # Create state with 10 observations
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test", todo_list=[]),
            trace=TraceState(
                observations=[f"Observation {i}" for i in range(10)]
            ),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is True
        assert reason == "context_length"
    
    def test_risky_action_detection(self):
        """Test pause triggered for risky actions."""
        config = AgentConfig()
        config.pause_min_remaining_todos = 99  # High threshold to avoid triggering
        
        # Create state with risky todo
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                todo_list=[
                    TodoItem(
                        description="Exploit vulnerability in service",
                        status=TodoStatus.IN_PROGRESS,
                    )
                ],
            ),
            trace=TraceState(),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is True
        assert reason == "risky_action"
    
    def test_budget_concerns(self):
        """Test pause triggered for budget concerns."""
        config = AgentConfig()
        config.pause_budget_concern_tools = 10
        config.pause_min_remaining_todos = 99  # High threshold
        
        # Create state with many tools used and 3+ remaining todos
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                tool_calls_used=10,
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(3)
                ],
            ),
            trace=TraceState(),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is True
        assert reason == "budget_concerns"
    
    def test_no_pause_conditions_met(self):
        """Test no pause when all conditions below thresholds."""
        config = AgentConfig()
        config.pause_min_remaining_todos = 5
        config.pause_context_length_threshold = 10
        config.pause_budget_concern_tools = 10
        
        # State with nothing triggering pause
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                tool_calls_used=5,
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(2)
                ],
            ),
            trace=TraceState(observations=["Obs1", "Obs2"]),
        )
        
        should_pause, reason = _should_pause_for_confirmation(interactive, config)
        
        assert should_pause is False
        assert reason is None


class TestPauseRequestBuilding:
    """Test _build_pause_request() pause request construction."""
    
    def test_build_pause_request_many_todos(self):
        """Test pause request for many_todos_remaining reason."""
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                tool_calls_used=5,
                iterations=3,
                todo_list=[
                    TodoItem(
                        description="Completed task",
                        status=TodoStatus.COMPLETE_POSITIVE,
                    ),
                    TodoItem(description="Pending task 1", status=TodoStatus.PENDING),
                    TodoItem(description="Pending task 2", status=TodoStatus.PENDING),
                ],
            ),
            trace=TraceState(observations=["Obs1", "Obs2"]),
        )
        
        pause_request = _build_pause_request(
            interactive, "many_todos_remaining (2 pending)"
        )
        
        assert pause_request.reason == "many_todos_remaining (2 pending)"
        assert "2 tasks still pending" in pause_request.question
        assert pause_request.current_progress["completed_todos"] == 1
        assert pause_request.current_progress["remaining_todos"] == 2
        assert pause_request.current_progress["tools_executed"] == 5
        assert len(pause_request.remaining_todos) == 2
        assert pause_request.estimated_time == 2 * 60  # 2 todos * 60 sec
        assert pause_request.estimated_tool_calls == 2 * 2  # 2 todos * 2 tools
    
    def test_build_pause_request_context_length(self):
        """Test pause request for context_length reason."""
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test", iterations=10),
            trace=TraceState(observations=[f"Obs {i}" for i in range(15)]),
        )
        
        pause_request = _build_pause_request(interactive, "context_length")
        
        assert pause_request.reason == "context_length"
        assert "15 observations" in pause_request.question
        assert pause_request.estimated_time is None
        assert pause_request.estimated_tool_calls is None
    
    def test_build_pause_request_risky_action(self):
        """Test pause request for risky_action reason."""
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                todo_list=[
                    TodoItem(
                        description="Exploit SQL injection",
                        status=TodoStatus.IN_PROGRESS,
                    )
                ],
            ),
            trace=TraceState(),
        )
        
        pause_request = _build_pause_request(interactive, "risky_action")
        
        assert pause_request.reason == "risky_action"
        assert "risky actions" in pause_request.question
        assert "Exploit SQL injection" in pause_request.question
        assert pause_request.estimated_time == 120
        assert pause_request.estimated_tool_calls == 3
    
    def test_build_pause_request_budget_concerns(self):
        """Test pause request for budget_concerns reason."""
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                tool_calls_used=12,
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(4)
                ],
            ),
            trace=TraceState(),
        )
        
        pause_request = _build_pause_request(interactive, "budget_concerns")
        
        assert pause_request.reason == "budget_concerns"
        assert "12 tools" in pause_request.question
        assert "4 tasks remaining" in pause_request.question
        assert pause_request.current_progress["tools_executed"] == 12
        assert pause_request.current_progress["remaining_todos"] == 4


class TestResponseWaiting:
    """Test _emit_and_wait_for_pause_response() response waiting logic."""
    
    @pytest.mark.asyncio
    async def test_user_approves_continuation(self, tmp_path):
        """Test user approves continuation."""
        # Setup
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test"),
            trace=TraceState(),
        )
        
        pause_request = AgentPauseRequest(
            reason="test",
            question="Continue?",
            current_progress={},
            remaining_todos=[],
        )
        
        config = AgentConfig()
        config.pause_response_timeout = 5
        
        response_file = tmp_path / "agent_pause_response.json"
        
        # Mock context with workspace path
        context = Mock()
        context.workspace_path = str(tmp_path)
        
        async def create_approval_response():
            await asyncio.sleep(0.2)
            response_file.write_text(
                json.dumps({"approved": True, "message": "Go ahead"})
            )

        response_task = asyncio.create_task(create_approval_response())
        approved = await _emit_and_wait_for_pause_response(
            pause_request, context, interactive, config
        )
        await response_task
        
        assert approved is True
        assert "[PAUSE] User response: Continue" in "\n".join(
            interactive.trace.reasoning
        )
    
    @pytest.mark.asyncio
    async def test_user_declines_continuation(self, tmp_path):
        """Test user declines continuation."""
        # Setup
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test"),
            trace=TraceState(),
        )
        
        pause_request = AgentPauseRequest(
            reason="test",
            question="Continue?",
            current_progress={},
            remaining_todos=[],
        )
        
        config = AgentConfig()
        config.pause_response_timeout = 5
        
        response_file = tmp_path / "agent_pause_response.json"
        
        # Mock context
        context = Mock()
        context.workspace_path = str(tmp_path)
        
        async def create_decline_response():
            await asyncio.sleep(0.2)
            response_file.write_text(
                json.dumps({"approved": False, "message": "Stop now"})
            )

        response_task = asyncio.create_task(create_decline_response())
        approved = await _emit_and_wait_for_pause_response(
            pause_request, context, interactive, config
        )
        await response_task
        
        assert approved is False
        assert "[PAUSE] User response: Stop" in "\n".join(
            interactive.trace.reasoning
        )
    
    @pytest.mark.asyncio
    async def test_timeout_defaults_to_continue(self, tmp_path):
        """Test timeout defaults to continuing."""
        # Setup
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test"),
            trace=TraceState(),
        )
        
        pause_request = AgentPauseRequest(
            reason="test",
            question="Continue?",
            current_progress={},
            remaining_todos=[],
        )
        
        config = AgentConfig()
        config.pause_response_timeout = 2  # Short timeout
        
        # No response file created (will timeout)
        context = Mock()
        context.workspace_path = str(tmp_path)
        
        # Test
        approved = await _emit_and_wait_for_pause_response(
            pause_request, context, interactive, config
        )
        
        assert approved is True  # Default to continue on timeout
        assert "[PAUSE] Timeout" in "\n".join(interactive.trace.reasoning)
    
    @pytest.mark.asyncio
    async def test_response_file_created_async(self, tmp_path):
        """Test response file created asynchronously during wait."""
        # Setup
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test"),
            trace=TraceState(),
        )
        
        pause_request = AgentPauseRequest(
            reason="test",
            question="Continue?",
            current_progress={},
            remaining_todos=[],
        )
        
        config = AgentConfig()
        config.pause_response_timeout = 10
        
        # Mock context
        context = Mock()
        context.workspace_path = str(tmp_path)
        
        # Create response file after 1 second (simulating user response)
        async def create_response_later():
            await asyncio.sleep(1)
            response_file = tmp_path / "agent_pause_response.json"
            response_file.write_text(
                json.dumps({"approved": True, "message": "Continue"})
            )
        
        # Start both tasks
        create_task = asyncio.create_task(create_response_later())
        wait_task = asyncio.create_task(
            _emit_and_wait_for_pause_response(
                pause_request, context, interactive, config
            )
        )
        
        # Wait for both
        await create_task
        approved = await wait_task
        
        assert approved is True
    
    @pytest.mark.asyncio
    async def test_malformed_response_file(self, tmp_path):
        """Test malformed response file defaults to continue."""
        # Setup
        interactive = InteractiveState(
            facts=FactsState(task_id=1, message="Test"),
            trace=TraceState(),
        )
        
        pause_request = AgentPauseRequest(
            reason="test",
            question="Continue?",
            current_progress={},
            remaining_todos=[],
        )
        
        config = AgentConfig()
        config.pause_response_timeout = 5
        
        # Create malformed response file
        response_file = tmp_path / "agent_pause_response.json"
        response_file.write_text("not valid json {")
        
        # Mock context
        context = Mock()
        context.workspace_path = str(tmp_path)
        
        # Test
        approved = await _emit_and_wait_for_pause_response(
            pause_request, context, interactive, config
        )
        
        assert approved is True  # Default to continue on error


class TestDecisionRouterIntegration:
    """Test pause logic integration with decision_router."""
    
    @pytest.mark.asyncio
    async def test_pause_disabled_by_default(self):
        """Test pause logic not triggered when disabled."""
        # Setup state that would trigger pause
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                budgets=BudgetState(max_iterations=15),
                metadata={"enable_agent_pause": False},  # Explicitly disabled
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(10)  # Many todos
                ],
            ),
            trace=TraceState(),
        )
        
        # Call decision_router
        result = await decision_router(interactive.as_graph_state())
        
        # Pause should not have been triggered
        assert "agent_pause_request" not in result.get("facts", {}).get("metadata", {})
    
    @pytest.mark.asyncio
    async def test_pause_triggered_and_approved(self, tmp_path):
        """Test pause triggered and user approves."""
        # Setup state that triggers pause
        interactive = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                budgets=BudgetState(max_iterations=15),
                metadata={"enable_agent_pause": True},  # Enabled
                todo_list=[
                    TodoItem(description=f"Todo {i}", status=TodoStatus.PENDING)
                    for i in range(10)  # Many todos (will trigger pause)
                ],
            ),
            trace=TraceState(),
        )
        
        # Create approval response file
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        response_file = workspace / "agent_pause_response.json"
        
        # Mock context
        context = Mock()
        context.workspace_path = str(workspace)
        
        # Create response after short delay
        async def create_approval():
            await asyncio.sleep(0.5)
            response_file.write_text(
                json.dumps({"approved": True, "message": "Continue"})
            )
        
        # Start approval task
        approval_task = asyncio.create_task(create_approval())

        # Call decision_router
        result = await decision_router(interactive.as_graph_state(), context)

        await approval_task
        
        # Pause should have been triggered and approved
        assert "agent_pause_request" in result.get("facts", {}).get("metadata", {})
        assert any(
            "[PAUSE] User approved continuation" in item
            for item in result.get("trace", {}).get("reasoning", [])
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

