"""Task lifecycle domain rules: status enum, state machine, and transition validators.

Scope:
- Defines TaskStatus enum.
- Enforces valid state transitions via TaskStateTransition.
- Provides stateless validation helpers (TaskStatusValidator, validate_status_change,
  get_status_metadata).

Boundaries:
- Pure domain logic only. No database I/O, no HTTP concerns, no service orchestration.
- Consumers that need DB-aware status changes use backend.services.task_lifecycle_service.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class TaskStatus(Enum):
    """
    Comprehensive task status enumeration with all possible states.

    Defines the complete lifecycle of a penetration testing task from creation
    to completion, including error states and manual interventions.
    """

    CREATED = "created"  # Initial state after task creation
    QUEUED = "queued"  # Waiting for resources or scheduling
    STARTING = "starting"  # Container initialization in progress
    RUNNING = "running"  # Agent actively executing penetration tests
    PAUSING = "pausing"  # Transition state while pausing
    PAUSED = "paused"  # Execution temporarily stopped by user
    RESUMING = "resuming"  # Transition state while resuming
    STOPPING = "stopping"  # Graceful shutdown in progress
    STOPPED = "stopped"  # Manually stopped by user
    COMPLETED = "completed"  # Successfully finished with results
    FAILED = "failed"  # Error or crash occurred during execution
    TIMEOUT = "timeout"  # Execution time limit exceeded

    def __str__(self):
        return self.value

    @classmethod
    def get_all_statuses(cls) -> List[str]:
        """Get list of all possible status values."""
        return [status.value for status in cls]

    @classmethod
    def get_active_statuses(cls) -> List[str]:
        """Get statuses considered active for generic list/read contexts."""
        return list(cls.ui_active_statuses())

    @classmethod
    def active_task_statuses(cls) -> frozenset[str]:
        """Single source of truth for statuses that consume concurrency.

        Used by quota and capacity counting. Other active-status helpers
        should delegate here instead of re-listing statuses.
        """
        return frozenset(
            {
                cls.CREATED.value,
                cls.QUEUED.value,
                cls.STARTING.value,
                cls.RUNNING.value,
                cls.PAUSING.value,
                cls.PAUSED.value,
                cls.RESUMING.value,
                cls.STOPPING.value,
            }
        )

    @classmethod
    def ui_active_statuses(cls) -> tuple[str, ...]:
        """Statuses shown as active in existing UI/read surfaces."""
        return cls._ordered_subset_from_active_task_statuses((cls.QUEUED, cls.STARTING, cls.RUNNING))

    @classmethod
    def runtime_active_statuses(cls) -> tuple[str, ...]:
        """Statuses representing runtime activity for operation/guard checks."""
        return cls._ordered_subset_from_active_task_statuses(
            (cls.QUEUED, cls.STARTING, cls.RUNNING, cls.PAUSING, cls.RESUMING)
        )

    @classmethod
    def create_name_reservation_statuses(cls) -> tuple[str, ...]:
        """Statuses that reserve task names during create-time dedup checks."""
        return cls._ordered_subset_from_active_task_statuses((cls.CREATED, cls.QUEUED, cls.STARTING, cls.RUNNING))

    @classmethod
    def engagement_archive_block_statuses(cls) -> tuple[str, ...]:
        """Statuses that block engagement archive while runtime state is active."""
        return cls._ordered_subset_from_active_task_statuses(
            (cls.QUEUED, cls.STARTING, cls.RUNNING, cls.PAUSING, cls.PAUSED, cls.RESUMING, cls.STOPPING)
        )

    @classmethod
    def _ordered_subset_from_active_task_statuses(cls, statuses: tuple["TaskStatus", ...]) -> tuple[str, ...]:
        """Filter an ordered status tuple through the canonical counted-active set."""
        active_statuses = cls.active_task_statuses()
        return tuple(status.value for status in statuses if status.value in active_statuses)

    @classmethod
    def get_terminal_statuses(cls) -> List[str]:
        """Get list of final/terminal statuses."""
        return [cls.COMPLETED.value, cls.FAILED.value, cls.TIMEOUT.value, cls.STOPPED.value]


class TaskStateTransition:
    """
    State machine validator for task status transitions.

    Defines valid state transitions and provides validation logic to ensure
    proper task lifecycle management and prevent invalid state changes.
    """

    # Define valid state transitions
    VALID_TRANSITIONS = {
        TaskStatus.CREATED: {TaskStatus.QUEUED, TaskStatus.FAILED},
        TaskStatus.QUEUED: {TaskStatus.STARTING, TaskStatus.STOPPED, TaskStatus.FAILED},
        TaskStatus.STARTING: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.STOPPED},
        TaskStatus.RUNNING: {TaskStatus.PAUSING, TaskStatus.STOPPING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT},
        TaskStatus.PAUSING: {TaskStatus.PAUSED, TaskStatus.RUNNING, TaskStatus.STOPPING},
        TaskStatus.PAUSED: {TaskStatus.RESUMING, TaskStatus.STOPPING, TaskStatus.STOPPED, TaskStatus.FAILED},
        TaskStatus.RESUMING: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.STOPPING},
        TaskStatus.STOPPING: {TaskStatus.STOPPED, TaskStatus.FAILED},
        TaskStatus.STOPPED: {TaskStatus.QUEUED},
        TaskStatus.COMPLETED: set(),  # Terminal state
        TaskStatus.FAILED: {TaskStatus.QUEUED},
        TaskStatus.TIMEOUT: {TaskStatus.QUEUED},
    }

    @classmethod
    def is_valid_transition(cls, current_status: TaskStatus, new_status: TaskStatus) -> bool:
        """Check if a state transition is valid."""
        return new_status in cls.VALID_TRANSITIONS.get(current_status, set())

    @classmethod
    def validate_transition(cls, current_status: str, new_status: str) -> Tuple[bool, str]:
        """Validate a status transition and return detailed result."""
        try:
            current_enum = TaskStatus(current_status)
            new_enum = TaskStatus(new_status)

            if cls.is_valid_transition(current_enum, new_enum):
                return True, ""

            valid_states = [s.value for s in cls.VALID_TRANSITIONS.get(current_enum, set())]
            return (
                False,
                f"Invalid transition from '{current_status}' to '{new_status}'. Valid transitions: {valid_states}",
            )

        except ValueError as e:
            return False, f"Invalid status value: {str(e)}"


class TaskStatusValidator:
    """Utility class for validating task status operations."""

    @staticmethod
    def can_start(status: str) -> bool:
        """Check if task can be started from current status."""
        try:
            current = TaskStatus(status)
            return current in {TaskStatus.CREATED, TaskStatus.STOPPED, TaskStatus.FAILED, TaskStatus.TIMEOUT}
        except ValueError:
            return False

    @staticmethod
    def can_pause(status: str) -> bool:
        """Check if task can be paused from current status."""
        try:
            current = TaskStatus(status)
            return current == TaskStatus.RUNNING
        except ValueError:
            return False

    @staticmethod
    def can_resume(status: str) -> bool:
        """Check if task can be resumed from current status."""
        try:
            current = TaskStatus(status)
            return current == TaskStatus.PAUSED
        except ValueError:
            return False

    @staticmethod
    def can_stop(status: str) -> bool:
        """Check if task can be stopped from current status."""
        try:
            current = TaskStatus(status)
            return current in {
                TaskStatus.QUEUED,
                TaskStatus.STARTING,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
                TaskStatus.PAUSING,
                TaskStatus.RESUMING,
            }
        except ValueError:
            return False

    @staticmethod
    def is_active(status: str) -> bool:
        """Check if task is in an active/running state."""
        try:
            current = TaskStatus(status)
            return current.value in TaskStatus.runtime_active_statuses()
        except ValueError:
            return False

    @staticmethod
    def is_terminal(status: str) -> bool:
        """Check if task is in a terminal/final state."""
        try:
            current = TaskStatus(status)
            return current in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.STOPPED}
        except ValueError:
            return False


def validate_status_change(current_status: str, new_status: str, user_id: Optional[int] = None) -> Tuple[bool, str]:
    """Convenience function to validate a status change with context."""
    is_valid, message = TaskStateTransition.validate_transition(current_status, new_status)

    if is_valid:
        # Get transition reason for successful validations
        transition_reasons = {
            ("created", "queued"): "Task queued for execution",
            ("queued", "starting"): "Initializing execution environment",
            ("starting", "running"): "Agent execution started",
            ("running", "pausing"): "User requested pause",
            ("pausing", "paused"): "Execution paused by user",
            ("pausing", "running"): "Pause cancelled",
            ("paused", "resuming"): "User requested resume",
            ("resuming", "running"): "Execution resumed",
            ("resuming", "paused"): "Resume failed",
            ("running", "stopping"): "Graceful shutdown initiated",
            ("stopping", "stopped"): "Task stopped successfully",
            ("running", "completed"): "Task completed successfully",
            ("running", "failed"): "Task execution failed",
            ("running", "timeout"): "Task execution timed out",
        }
        reason = transition_reasons.get((current_status, new_status), f"Status changed from {current_status} to {new_status}")
        return True, reason

    return is_valid, message


def get_status_metadata(status: str) -> Dict[str, Any]:
    """Get metadata about a specific status."""
    try:
        status_enum = TaskStatus(status)
        valid_next_states = [s.value for s in TaskStateTransition.VALID_TRANSITIONS.get(status_enum, set())]

        return {
            "status": status,
            "is_active": TaskStatusValidator.is_active(status),
            "is_terminal": TaskStatusValidator.is_terminal(status),
            "can_start": TaskStatusValidator.can_start(status),
            "can_pause": TaskStatusValidator.can_pause(status),
            "can_resume": TaskStatusValidator.can_resume(status),
            "can_stop": TaskStatusValidator.can_stop(status),
            "valid_next_states": valid_next_states,
        }
    except ValueError:
        return {
            "status": status,
            "error": "Invalid status value",
            "is_active": False,
            "is_terminal": False,
            "can_start": False,
            "can_pause": False,
            "can_resume": False,
            "can_stop": False,
            "valid_next_states": [],
        }
