"""
Tests for task status enums, transition rules, and convenience helpers.

This suite validates the current task lifecycle contract in backend.models.
"""

import pytest

from backend.domain.task_lifecycle import TaskStateTransition, TaskStatus, TaskStatusValidator, get_status_metadata, validate_status_change

pytestmark = pytest.mark.execution_plane_non_dind_regression


class TestTaskStatus:
    """TaskStatus enum expectations."""

    def test_status_enum_values(self) -> None:
        expected_statuses = {
            "created",
            "queued",
            "starting",
            "running",
            "pausing",
            "paused",
            "resuming",
            "stopping",
            "stopped",
            "completed",
            "failed",
            "timeout",
        }
        assert set(TaskStatus.get_all_statuses()) == expected_statuses

    def test_active_statuses(self) -> None:
        assert set(TaskStatus.get_active_statuses()) == {"queued", "starting", "running"}

    def test_terminal_statuses(self) -> None:
        assert set(TaskStatus.get_terminal_statuses()) == {"completed", "failed", "timeout", "stopped"}

    def test_status_string_conversion(self) -> None:
        assert str(TaskStatus.RUNNING) == "running"
        assert str(TaskStatus.COMPLETED) == "completed"


class TestTaskStateTransition:
    """State transition rule validation."""

    def test_valid_transitions_from_created(self) -> None:
        assert TaskStateTransition.VALID_TRANSITIONS[TaskStatus.CREATED] == {
            TaskStatus.QUEUED,
            TaskStatus.FAILED,
        }

    def test_valid_transitions_from_running(self) -> None:
        assert TaskStateTransition.VALID_TRANSITIONS[TaskStatus.RUNNING] == {
            TaskStatus.PAUSING,
            TaskStatus.STOPPING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TIMEOUT,
        }

    def test_invalid_transition_validation(self) -> None:
        assert not TaskStateTransition.is_valid_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
        assert not TaskStateTransition.is_valid_transition(TaskStatus.CREATED, TaskStatus.RUNNING)

    def test_valid_transition_validation(self) -> None:
        assert TaskStateTransition.is_valid_transition(TaskStatus.CREATED, TaskStatus.QUEUED)
        assert TaskStateTransition.is_valid_transition(TaskStatus.QUEUED, TaskStatus.STARTING)
        assert TaskStateTransition.is_valid_transition(TaskStatus.STARTING, TaskStatus.RUNNING)

    def test_transition_validation_with_strings(self) -> None:
        is_valid, message = TaskStateTransition.validate_transition("created", "queued")
        assert is_valid
        assert message == ""

        is_valid, message = TaskStateTransition.validate_transition("completed", "running")
        assert not is_valid
        assert "Invalid transition" in message


class TestTaskStatusValidator:
    """Task operation validators."""

    def test_can_start_validation(self) -> None:
        assert TaskStatusValidator.can_start("created")
        assert TaskStatusValidator.can_start("stopped")
        assert TaskStatusValidator.can_start("failed")
        assert TaskStatusValidator.can_start("timeout")
        assert not TaskStatusValidator.can_start("running")
        assert not TaskStatusValidator.can_start("completed")

    def test_can_pause_validation(self) -> None:
        assert TaskStatusValidator.can_pause("running")
        assert not TaskStatusValidator.can_pause("created")
        assert not TaskStatusValidator.can_pause("paused")
        assert not TaskStatusValidator.can_pause("completed")

    def test_can_resume_validation(self) -> None:
        assert TaskStatusValidator.can_resume("paused")
        assert not TaskStatusValidator.can_resume("running")
        assert not TaskStatusValidator.can_resume("stopped")

    def test_can_stop_validation(self) -> None:
        assert TaskStatusValidator.can_stop("queued")
        assert TaskStatusValidator.can_stop("starting")
        assert TaskStatusValidator.can_stop("running")
        assert TaskStatusValidator.can_stop("pausing")
        assert TaskStatusValidator.can_stop("paused")
        assert TaskStatusValidator.can_stop("resuming")
        assert not TaskStatusValidator.can_stop("completed")
        assert not TaskStatusValidator.can_stop("stopped")

    def test_is_active_validation(self) -> None:
        assert TaskStatusValidator.is_active("queued")
        assert TaskStatusValidator.is_active("starting")
        assert TaskStatusValidator.is_active("running")
        assert TaskStatusValidator.is_active("pausing")
        assert TaskStatusValidator.is_active("resuming")
        assert not TaskStatusValidator.is_active("created")
        assert not TaskStatusValidator.is_active("paused")
        assert not TaskStatusValidator.is_active("completed")

    def test_is_terminal_validation(self) -> None:
        assert TaskStatusValidator.is_terminal("completed")
        assert TaskStatusValidator.is_terminal("failed")
        assert TaskStatusValidator.is_terminal("timeout")
        assert TaskStatusValidator.is_terminal("stopped")
        assert not TaskStatusValidator.is_terminal("created")
        assert not TaskStatusValidator.is_terminal("running")


class TestConvenienceFunctions:
    """Convenience helper validation."""

    def test_validate_status_change_success(self) -> None:
        is_valid, reason = validate_status_change("created", "queued", user_id=1)
        assert is_valid
        assert "queued for execution" in reason.lower()

    def test_validate_status_change_failure(self) -> None:
        is_valid, error = validate_status_change("completed", "running", user_id=1)
        assert not is_valid
        assert "Invalid transition" in error

    def test_get_status_metadata_valid(self) -> None:
        metadata = get_status_metadata("running")
        assert metadata["status"] == "running"
        assert metadata["is_active"] is True
        assert metadata["is_terminal"] is False
        assert metadata["can_pause"] is True
        assert metadata["can_stop"] is True
        assert "pausing" in metadata["valid_next_states"]

    def test_get_status_metadata_invalid(self) -> None:
        metadata = get_status_metadata("invalid_status")
        assert metadata["status"] == "invalid_status"
        assert "error" in metadata
        assert metadata["is_active"] is False
        assert metadata["valid_next_states"] == []


class TestTaskLifecycleScenarios:
    """Representative lifecycle flows."""

    def test_normal_task_lifecycle(self) -> None:
        is_valid, _ = validate_status_change("created", "queued")
        assert is_valid
        is_valid, _ = validate_status_change("queued", "starting")
        assert is_valid
        is_valid, _ = validate_status_change("starting", "running")
        assert is_valid
        is_valid, _ = validate_status_change("running", "completed")
        assert is_valid

    def test_pause_resume_lifecycle(self) -> None:
        assert TaskStatusValidator.can_pause("running")
        is_valid, _ = validate_status_change("running", "pausing")
        assert is_valid
        is_valid, _ = validate_status_change("pausing", "paused")
        assert is_valid
        assert TaskStatusValidator.can_resume("paused")
        is_valid, _ = validate_status_change("paused", "resuming")
        assert is_valid
        is_valid, _ = validate_status_change("resuming", "running")
        assert is_valid

    def test_failure_and_retry_lifecycle(self) -> None:
        is_valid, _ = validate_status_change("running", "failed")
        assert is_valid
        assert TaskStatusValidator.can_start("failed")
        is_valid, _ = validate_status_change("failed", "queued")
        assert is_valid

    def test_manual_stop_lifecycle(self) -> None:
        assert TaskStatusValidator.can_stop("running")
        is_valid, _ = validate_status_change("running", "stopping")
        assert is_valid
        is_valid, _ = validate_status_change("stopping", "stopped")
        assert is_valid
        assert TaskStatusValidator.can_start("stopped")
