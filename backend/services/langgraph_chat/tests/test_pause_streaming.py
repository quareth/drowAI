"""Tests for agent pause request streaming in LangGraph adapter.

Verifies that pause requests are properly detected and emitted as
dedicated streaming events for frontend consumption.
"""

from agent.graph.state import FactsState, InteractiveState, TodoItem, TodoStatus, TraceState
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


class TestAgentPauseRequestStreaming:
    """Test pause request event emission in streaming adapter."""
    
    def test_no_pause_request_returns_none(self):
        """Test that no event is emitted when no pause request in metadata."""
        adapter = LangGraphStreamingAdapter()
        
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                metadata={},  # No pause request
            ),
            trace=TraceState(),
        )
        
        event = adapter.build_agent_pause_request_event(state)
        
        assert event is None
    
    def test_pause_request_event_basic(self):
        """Test basic pause request event emission."""
        adapter = LangGraphStreamingAdapter()
        
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                conversation_id="conv-123",
                metadata={
                    "agent_pause_request": {
                        "reason": "many_todos_remaining (5 pending)",
                        "question": "I have 5 tasks still pending. Should I continue?",
                        "current_progress": {
                            "completed_todos": 2,
                            "remaining_todos": 5,
                            "tools_executed": 8,
                            "iterations": 6,
                        },
                        "remaining_todos": [
                            "Scan for open ports",
                            "Check for vulnerabilities",
                            "Test authentication",
                        ],
                        "estimated_time": 300,
                        "estimated_tool_calls": 10,
                        "pause_timestamp": "2025-11-08T12:34:56.789Z",
                    }
                },
            ),
            trace=TraceState(),
        )
        
        event = adapter.build_agent_pause_request_event(state)
        
        assert event is not None
        assert event["type"] == "agent_pause_request"
        assert event["metadata"]["subtype"] == "agent_pause_request"
        assert event["metadata"]["requires_user_action"] is True
        
        pause_data = event["metadata"]["pause_request"]
        assert pause_data["reason"] == "many_todos_remaining (5 pending)"
        assert pause_data["question"] == "I have 5 tasks still pending. Should I continue?"
        assert pause_data["current_progress"]["completed_todos"] == 2
        assert pause_data["current_progress"]["remaining_todos"] == 5
        assert len(pause_data["remaining_todos"]) == 3
        assert pause_data["estimated_time"] == 300
        assert pause_data["estimated_tool_calls"] == 10
    
    def test_pause_request_content_formatting(self):
        """Test that pause request content is properly formatted."""
        adapter = LangGraphStreamingAdapter()
        
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                metadata={
                    "agent_pause_request": {
                        "reason": "risky_action",
                        "question": "Next task involves risky actions. Proceed?",
                        "current_progress": {
                            "completed_todos": 1,
                            "remaining_todos": 2,
                            "tools_executed": 3,
                            "iterations": 2,
                        },
                        "remaining_todos": ["Exploit vulnerability", "Test payload"],
                        "estimated_time": 120,
                        "estimated_tool_calls": 3,
                    }
                },
            ),
            trace=TraceState(),
        )
        
        event = adapter.build_agent_pause_request_event(state)
        
        content = event["content"]
        assert "🛑 **Agent Pause Request**" in content
        assert "Next task involves risky actions. Proceed?" in content
        assert "**Current Progress:**" in content
        assert "Completed todos: 1" in content
        assert "**Remaining Tasks:**" in content
        assert "Exploit vulnerability" in content
        assert "**Estimated:**" in content
        assert "~2m 0s" in content
        assert "~3 tools" in content
        assert "reason: risky_action" in content
    
    def test_pause_request_with_many_todos(self):
        """Test content formatting when many todos are remaining."""
        adapter = LangGraphStreamingAdapter()
        
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                metadata={
                    "agent_pause_request": {
                        "reason": "many_todos_remaining (7 pending)",
                        "question": "I have 7 tasks remaining. Continue?",
                        "current_progress": {},
                        "remaining_todos": [
                            "Todo 1",
                            "Todo 2",
                            "Todo 3",
                            "Todo 4",
                            "Todo 5",
                            "Todo 6",
                            "Todo 7",
                        ],
                    }
                },
            ),
            trace=TraceState(),
        )
        
        event = adapter.build_agent_pause_request_event(state)
        
        content = event["content"]
        # Should show first 3 todos + "... and N more"
        assert "Todo 1" in content
        assert "Todo 2" in content
        assert "Todo 3" in content
        assert "... and 4 more" in content
    
    def test_pause_request_time_estimation_formatting(self):
        """Test time estimation formatting in event content."""
        adapter = LangGraphStreamingAdapter()
        
        # Test minutes and seconds
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="Test",
                metadata={
                    "agent_pause_request": {
                        "reason": "test",
                        "question": "Test?",
                        "current_progress": {},
                        "remaining_todos": [],
                        "estimated_time": 185,  # 3m 5s
                    }
                },
            ),
            trace=TraceState(),
        )
        
        event = adapter.build_agent_pause_request_event(state)
        assert "~3m 5s" in event["content"]
        
        # Test seconds only
        state.facts.metadata["agent_pause_request"]["estimated_time"] = 45
        event = adapter.build_agent_pause_request_event(state)
        assert "~45s" in event["content"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

