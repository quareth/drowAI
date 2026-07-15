"""Tests for TodoItem model and related enums."""

from datetime import datetime, timezone
from typing import List

import pytest

from agent.graph.state import (
    CompletionType,
    TodoItem,
    TodoStatus,
)


class TestTodoStatus:
    """Test TodoStatus enum values and behavior."""

    def test_all_statuses_defined(self):
        """Verify all required status values are defined."""
        assert TodoStatus.PENDING == "pending"
        assert TodoStatus.IN_PROGRESS == "in_progress"
        assert TodoStatus.COMPLETE_POSITIVE == "complete_positive"
        assert TodoStatus.COMPLETE_NEGATIVE == "complete_negative"
        assert TodoStatus.EXHAUSTED == "exhausted"

    def test_status_string_representation(self):
        """Verify status values are the expected strings."""
        assert TodoStatus.PENDING.value == "pending"
        assert TodoStatus.IN_PROGRESS.value == "in_progress"

    def test_status_comparison(self):
        """Verify status values can be compared."""
        status1 = TodoStatus.PENDING
        status2 = TodoStatus.PENDING
        status3 = TodoStatus.IN_PROGRESS
        
        assert status1 == status2
        assert status1 != status3


class TestCompletionType:
    """Test CompletionType enum values and behavior."""

    def test_all_completion_types_defined(self):
        """Verify all required completion types are defined."""
        assert CompletionType.POSITIVE == "positive"
        assert CompletionType.NEGATIVE == "negative"
        assert CompletionType.INCOMPLETE == "incomplete"
        assert CompletionType.EXHAUSTED == "exhausted"

    def test_completion_type_string_representation(self):
        """Verify completion types are the expected strings."""
        assert CompletionType.POSITIVE.value == "positive"
        assert CompletionType.NEGATIVE.value == "negative"


class TestTodoItem:
    """Test TodoItem model functionality."""

    def test_create_todo_with_defaults(self):
        """Test creating TodoItem with minimum required fields."""
        todo = TodoItem(description="Scan for hosts")
        
        assert todo.description == "Scan for hosts"
        assert todo.status == TodoStatus.PENDING
        assert todo.attempts == 0
        assert todo.actions_taken == []
        assert todo.results == []
        assert todo.started_at is None
        assert todo.completed_at is None
        assert todo.completion_type is None
        assert todo.completion_reasoning is None

    def test_create_todo_with_all_fields(self):
        """Test creating TodoItem with all fields specified."""
        started = datetime.now(timezone.utc)
        completed = datetime.now(timezone.utc)
        
        todo = TodoItem(
            description="Find vulnerabilities",
            status=TodoStatus.COMPLETE_POSITIVE,
            attempts=3,
            actions_taken=[{"tool": "nmap"}],
            results=[{"output": "Found CVE-2023-1234"}],
            started_at=started,
            completed_at=completed,
            completion_type=CompletionType.POSITIVE,
            completion_reasoning="Found specific vulnerabilities",
        )
        
        assert todo.description == "Find vulnerabilities"
        assert todo.status == TodoStatus.COMPLETE_POSITIVE
        assert todo.attempts == 3
        assert len(todo.actions_taken) == 1
        assert len(todo.results) == 1
        assert todo.started_at == started
        assert todo.completed_at == completed
        assert todo.completion_type == CompletionType.POSITIVE
        assert todo.completion_reasoning == "Found specific vulnerabilities"

    def test_add_attempt_increments_counter(self):
        """Test that add_attempt() increments attempts counter."""
        todo = TodoItem(description="Scan for hosts")
        
        assert todo.attempts == 0
        
        todo.add_attempt(
            action={"tool": "nmap", "target": "192.168.1.0/24"},
            result={"output": "3 hosts discovered"},
        )
        
        assert todo.attempts == 1
        assert len(todo.actions_taken) == 1
        assert len(todo.results) == 1

    def test_add_attempt_stores_action_and_result(self):
        """Test that add_attempt() stores action and result with timestamps."""
        todo = TodoItem(description="Scan for hosts")
        
        action = {"tool": "nmap", "target": "192.168.1.0/24"}
        result = {"output": "3 hosts discovered", "status": "success"}
        
        todo.add_attempt(action, result)
        
        # Verify action stored with timestamp
        stored_action = todo.actions_taken[0]
        assert stored_action["tool"] == "nmap"
        assert stored_action["target"] == "192.168.1.0/24"
        assert "timestamp" in stored_action
        
        # Verify result stored with timestamp
        stored_result = todo.results[0]
        assert stored_result["output"] == "3 hosts discovered"
        assert stored_result["status"] == "success"
        assert "timestamp" in stored_result

    def test_add_multiple_attempts(self):
        """Test multiple add_attempt() calls accumulate correctly."""
        todo = TodoItem(description="Find vulnerabilities")
        
        todo.add_attempt(
            action={"tool": "nmap", "scan_type": "quick"},
            result={"hosts": 3},
        )
        todo.add_attempt(
            action={"tool": "openvas", "depth": "deep"},
            result={"vulnerabilities": 2},
        )
        todo.add_attempt(
            action={"tool": "nmap", "scan_type": "full"},
            result={"services": 5},
        )
        
        assert todo.attempts == 3
        assert len(todo.actions_taken) == 3
        assert len(todo.results) == 3
        assert todo.actions_taken[0]["tool"] == "nmap"
        assert todo.actions_taken[1]["tool"] == "openvas"
        assert todo.actions_taken[2]["tool"] == "nmap"

    def test_mark_complete_positive(self):
        """Test mark_complete() with positive completion."""
        todo = TodoItem(description="Find vulnerabilities")
        
        todo.mark_complete(
            CompletionType.POSITIVE,
            "Found CVE-2023-1234 in PostgreSQL service",
        )
        
        assert todo.status == TodoStatus.COMPLETE_POSITIVE
        assert todo.completion_type == CompletionType.POSITIVE
        assert todo.completion_reasoning == "Found CVE-2023-1234 in PostgreSQL service"
        assert todo.completed_at is not None
        assert isinstance(todo.completed_at, datetime)

    def test_mark_complete_negative(self):
        """Test mark_complete() with negative completion."""
        todo = TodoItem(description="Find vulnerabilities")
        
        todo.mark_complete(
            CompletionType.NEGATIVE,
            "Ran nmap, OpenVAS, and manual checks. No vulnerabilities found.",
        )
        
        assert todo.status == TodoStatus.COMPLETE_NEGATIVE
        assert todo.completion_type == CompletionType.NEGATIVE
        assert todo.completion_reasoning == "Ran nmap, OpenVAS, and manual checks. No vulnerabilities found."
        assert todo.completed_at is not None

    def test_mark_complete_exhausted(self):
        """Test mark_complete() with exhausted completion."""
        todo = TodoItem(description="Find vulnerabilities")
        
        todo.mark_complete(
            CompletionType.EXHAUSTED,
            "Hit max_attempts guardrail (5 attempts)",
        )
        
        assert todo.status == TodoStatus.EXHAUSTED
        assert todo.completion_type == CompletionType.EXHAUSTED
        assert todo.completion_reasoning == "Hit max_attempts guardrail (5 attempts)"
        assert todo.completed_at is not None

    def test_is_complete_returns_false_for_pending(self):
        """Test is_complete() returns False for pending todos."""
        todo = TodoItem(description="Scan for hosts")
        assert not todo.is_complete()

    def test_is_complete_returns_false_for_in_progress(self):
        """Test is_complete() returns False for in-progress todos."""
        todo = TodoItem(description="Scan for hosts", status=TodoStatus.IN_PROGRESS)
        assert not todo.is_complete()

    def test_is_complete_returns_true_for_complete_positive(self):
        """Test is_complete() returns True for positive completion."""
        todo = TodoItem(description="Scan for hosts")
        todo.mark_complete(CompletionType.POSITIVE, "Found hosts")
        assert todo.is_complete()

    def test_is_complete_returns_true_for_complete_negative(self):
        """Test is_complete() returns True for negative completion."""
        todo = TodoItem(description="Scan for hosts")
        todo.mark_complete(CompletionType.NEGATIVE, "No hosts found")
        assert todo.is_complete()

    def test_is_complete_returns_true_for_exhausted(self):
        """Test is_complete() returns True for exhausted todos."""
        todo = TodoItem(description="Scan for hosts")
        todo.mark_complete(CompletionType.EXHAUSTED, "Max attempts reached")
        assert todo.is_complete()

    def test_serialization_to_dict(self):
        """Test TodoItem can be serialized to dict via model_dump()."""
        todo = TodoItem(description="Scan for hosts")
        todo.add_attempt(
            action={"tool": "nmap"},
            result={"output": "Success"},
        )
        
        data = todo.model_dump()
        
        assert isinstance(data, dict)
        assert data["description"] == "Scan for hosts"
        assert data["status"] == "pending"
        assert data["attempts"] == 1
        assert len(data["actions_taken"]) == 1
        assert len(data["results"]) == 1

    def test_deserialization_from_dict(self):
        """Test TodoItem can be deserialized from dict via model_validate()."""
        data = {
            "description": "Find vulnerabilities",
            "status": "in_progress",
            "attempts": 2,
            "actions_taken": [
                {"tool": "nmap", "timestamp": "2023-01-01T00:00:00Z"}
            ],
            "results": [
                {"output": "Success", "timestamp": "2023-01-01T00:00:00Z"}
            ],
        }
        
        todo = TodoItem.model_validate(data)
        
        assert todo.description == "Find vulnerabilities"
        assert todo.status == TodoStatus.IN_PROGRESS
        assert todo.attempts == 2
        assert len(todo.actions_taken) == 1
        assert len(todo.results) == 1

    def test_round_trip_serialization(self):
        """Test TodoItem can be serialized and deserialized without data loss."""
        original = TodoItem(description="Scan for hosts")
        original.add_attempt(
            action={"tool": "nmap", "target": "192.168.1.0/24"},
            result={"output": "3 hosts found"},
        )
        original.mark_complete(
            CompletionType.POSITIVE,
            "Successfully found hosts",
        )
        
        # Serialize
        data = original.model_dump()
        
        # Deserialize
        restored = TodoItem.model_validate(data)
        
        # Verify all fields match
        assert restored.description == original.description
        assert restored.status == original.status
        assert restored.attempts == original.attempts
        assert len(restored.actions_taken) == len(original.actions_taken)
        assert len(restored.results) == len(original.results)
        assert restored.completion_type == original.completion_type
        assert restored.completion_reasoning == original.completion_reasoning

    def test_from_string_creates_todo_with_defaults(self):
        """Test from_string() creates TodoItem from legacy string format."""
        todo = TodoItem.from_string("Scan for hosts")
        
        assert todo.description == "Scan for hosts"
        assert todo.status == TodoStatus.PENDING
        assert todo.attempts == 0
        assert todo.actions_taken == []
        assert todo.results == []

    def test_from_string_list_converts_multiple_strings(self):
        """Test from_string_list() converts list of strings to TodoItems."""
        strings = [
            "Scan for hosts",
            "Find vulnerabilities",
            "Exploit services",
        ]
        
        todos = TodoItem.from_string_list(strings)
        
        assert len(todos) == 3
        assert todos[0].description == "Scan for hosts"
        assert todos[1].description == "Find vulnerabilities"
        assert todos[2].description == "Exploit services"
        assert all(isinstance(todo, TodoItem) for todo in todos)
        assert all(todo.status == TodoStatus.PENDING for todo in todos)

    def test_from_string_list_empty_list(self):
        """Test from_string_list() handles empty list correctly."""
        todos = TodoItem.from_string_list([])
        assert todos == []

    def test_backward_compatibility_with_string_todos(self):
        """Test that TodoItem maintains backward compatibility with string format."""
        # Legacy string format
        legacy_todo = "Scan for hosts"
        
        # Convert to TodoItem
        todo = TodoItem.from_string(legacy_todo)
        
        # Should work with all TodoItem methods
        todo.add_attempt(
            action={"tool": "nmap"},
            result={"output": "Success"},
        )
        todo.mark_complete(CompletionType.POSITIVE, "Done")
        
        assert todo.is_complete()
        assert todo.attempts == 1

    def test_enum_validation_for_status(self):
        """Test that invalid status values are rejected."""
        with pytest.raises(ValueError):
            TodoItem(description="Test", status="invalid_status")  # type: ignore

    def test_enum_validation_for_completion_type(self):
        """Test that invalid completion types are rejected."""
        todo = TodoItem(description="Test")
        
        # Valid completion type should work
        todo.mark_complete(CompletionType.POSITIVE, "Success")
        
        # Invalid completion type should be caught by type system
        # (Pydantic will reject invalid values during validation)
        with pytest.raises((ValueError, TypeError)):
            todo.completion_type = "invalid_type"  # type: ignore
            todo.model_validate(todo.model_dump())


class TestTodoItemEdgeCases:
    """Test edge cases and error conditions for TodoItem."""

    def test_attempts_counter_cannot_be_negative(self):
        """Test that attempts counter rejects negative values."""
        with pytest.raises(ValueError):
            TodoItem(description="Test", attempts=-1)

    def test_completed_at_is_set_automatically(self):
        """Test that completed_at is set automatically by mark_complete()."""
        todo = TodoItem(description="Test")
        assert todo.completed_at is None
        
        before_completion = datetime.now(timezone.utc)
        todo.mark_complete(CompletionType.POSITIVE, "Done")
        after_completion = datetime.now(timezone.utc)
        
        assert todo.completed_at is not None
        assert before_completion <= todo.completed_at <= after_completion

    def test_mark_complete_can_be_called_multiple_times(self):
        """Test that mark_complete() can update completion status."""
        todo = TodoItem(description="Test")
        
        # First completion
        todo.mark_complete(CompletionType.INCOMPLETE, "Not done yet")
        first_completed_at = todo.completed_at
        
        # Update completion
        todo.mark_complete(CompletionType.POSITIVE, "Actually done now")
        second_completed_at = todo.completed_at
        
        assert todo.status == TodoStatus.COMPLETE_POSITIVE
        assert todo.completion_type == CompletionType.POSITIVE
        assert second_completed_at >= first_completed_at

    def test_empty_description_is_allowed(self):
        """Test that empty description is technically allowed (though not recommended)."""
        todo = TodoItem(description="")
        assert todo.description == ""

    def test_large_number_of_attempts(self):
        """Test handling of many attempts."""
        todo = TodoItem(description="Test")
        
        # Add many attempts
        for i in range(100):
            todo.add_attempt(
                action={"iteration": i},
                result={"output": f"Result {i}"},
            )
        
        assert todo.attempts == 100
        assert len(todo.actions_taken) == 100
        assert len(todo.results) == 100

    def test_complex_nested_action_data(self):
        """Test handling of complex nested data in actions and results."""
        todo = TodoItem(description="Test")
        
        complex_action = {
            "tool": "nmap",
            "params": {
                "target": "192.168.1.0/24",
                "ports": [80, 443, 8080],
                "options": {
                    "aggressive": True,
                    "service_detection": True,
                },
            },
        }
        
        complex_result = {
            "hosts": [
                {"ip": "192.168.1.1", "ports": [80, 443]},
                {"ip": "192.168.1.2", "ports": [8080]},
            ],
            "metadata": {
                "scan_duration": 45.2,
                "timestamp": "2023-01-01T00:00:00Z",
            },
        }
        
        todo.add_attempt(complex_action, complex_result)
        
        assert todo.attempts == 1
        assert todo.actions_taken[0]["tool"] == "nmap"
        assert todo.actions_taken[0]["params"]["ports"] == [80, 443, 8080]
        assert len(todo.results[0]["hosts"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

