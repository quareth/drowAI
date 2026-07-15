"""
Task State Management Service (Step 1.4)

Implements comprehensive state transition validation and enforcement with
automatic state transitions, logging, and history tracking.
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models import (
    Task,
    TaskHistory,
    TaskStateTransition,
    TaskStatus,
    TaskStatusValidator,
    get_status_metadata,
)
from backend.core.time_utils import utc_now


class TaskStateService:
    """
    Service for managing task state transitions with validation and history tracking.

    Provides centralized state management with proper validation, audit trails,
    and automatic state transitions based on system events.
    """

    def __init__(self, db: Session):
        self.db = db

    def change_task_status(
        self,
        task_id: int,
        new_status: str,
        user_id: Optional[int] = None,
        reason: Optional[str] = None,
        change_source: str = "manual",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[TaskHistory]]:
        """
        Change task status with validation and history tracking.

        Args:
            task_id: ID of the task to update
            new_status: New status to apply
            user_id: ID of user making the change (optional for system changes)
            reason: Human-readable reason for the change
            change_source: Source of change (manual, automatic, system, error)
            metadata: Additional context data

        Returns:
            Tuple of (success, message, history_entry)
        """
        # Ensure we have a fresh session that isn't closed
        if self.db.is_active is False:
            from backend.database import SessionLocal

            self.db = SessionLocal()

        try:
            # Get the task
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False, f"Task {task_id} not found", None

            current_status = str(task.status)

            # Validate the transition
            is_valid, validation_message = TaskStateTransition.validate_transition(current_status, new_status)
            if not is_valid:
                return False, validation_message, None

            # Create history entry before making the change
            history_entry = TaskHistory(
                task_id=task_id,
                tenant_id=task.tenant_id,
                user_id=user_id,
                old_status=current_status,
                new_status=new_status,
                transition_reason=reason or validation_message,
                change_source=change_source,
                change_metadata=metadata,
            )

            # Update task status using setattr to handle SQLAlchemy columns properly
            setattr(task, "status", new_status)

            # Update relevant timestamps based on status
            self._update_task_timestamps(task, new_status)

            # Save changes
            self.db.add(history_entry)
            self.db.commit()

            try:
                from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

                hub = get_in_memory_stream_hub()
                hub.set_task_running(task_id, str(new_status).lower() == "running")
            except Exception:
                pass

            return True, f"Status changed from {current_status} to {new_status}", history_entry

        except SQLAlchemyError as e:
            self.db.rollback()
            return False, f"Database error: {str(e)}", None
        except Exception as e:
            self.db.rollback()
            return False, f"Unexpected error: {str(e)}", None

    def stage_task_status_change(
        self,
        task_id: int,
        new_status: str,
        user_id: Optional[int] = None,
        reason: Optional[str] = None,
        change_source: str = "manual",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[TaskHistory]]:
        """Stage a status transition without committing or rolling back.

        This is used by higher-level transaction owners (for example, admission
        control) that need status writes to remain in the current transaction
        window. The caller owns commit/rollback, so this method must never swap
        or recreate the session — doing so would move the staged write out of
        the admission transaction and break its advisory-lock guarantee.
        """
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False, f"Task {task_id} not found", None

            current_status = str(task.status)
            is_valid, validation_message = TaskStateTransition.validate_transition(current_status, new_status)
            if not is_valid:
                return False, validation_message, None

            history_entry = TaskHistory(
                task_id=task_id,
                tenant_id=task.tenant_id,
                user_id=user_id,
                old_status=current_status,
                new_status=new_status,
                transition_reason=reason or validation_message,
                change_source=change_source,
                change_metadata=metadata,
            )

            setattr(task, "status", new_status)
            self._update_task_timestamps(task, new_status)
            self.db.add(history_entry)
            self.db.flush()

            return True, f"Status staged from {current_status} to {new_status}", history_entry
        except SQLAlchemyError as e:
            return False, f"Database error: {str(e)}", None
        except Exception as e:
            return False, f"Unexpected error: {str(e)}", None

    def get_task_status_metadata(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Get comprehensive status metadata for a task."""
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return None

            base_metadata = get_status_metadata(task.status)

            # Add task-specific information
            task_metadata = {
                **base_metadata,
                "task_id": task_id,
                "current_status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "paused_at": task.paused_at.isoformat() if task.paused_at else None,
                "stopped_at": task.stopped_at.isoformat() if task.stopped_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "retry_count": task.retry_count,
                "progress_percentage": task.progress_percentage,
                "current_step": task.current_step,
                "error_message": task.error_message,
                "failure_reason": task.failure_reason,
            }

            return task_metadata

        except Exception as e:
            return {"error": f"Failed to get metadata: {str(e)}"}

    def get_task_history(self, task_id: int, limit: int = 50) -> List[TaskHistory]:
        """Get status change history for a task."""
        try:
            history = (
                self.db.query(TaskHistory)
                .filter(TaskHistory.task_id == task_id)
                .order_by(TaskHistory.timestamp.desc())
                .limit(limit)
                .all()
            )
            return history
        except Exception:
            return []

    def auto_transition_to_failed(
        self,
        task_id: int,
        error_message: str,
        failure_reason: str = "system_error",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Automatically transition task to failed state due to system error."""
        # Update error information
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if task:
            setattr(task, "error_message", error_message)
            setattr(task, "failure_reason", failure_reason)
            current_retry_count = getattr(task, "retry_count", 0) or 0
            setattr(task, "retry_count", current_retry_count + 1)

        # Transition to failed
        success, message, _ = self.change_task_status(
            task_id=task_id,
            new_status=TaskStatus.FAILED.value,
            user_id=None,
            reason=f"System error: {error_message}",
            change_source="error",
            metadata=metadata,
        )

        return success, message

    def auto_transition_to_timeout(
        self,
        task_id: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Automatically transition task to timeout state."""
        success, message, _ = self.change_task_status(
            task_id=task_id,
            new_status=TaskStatus.TIMEOUT.value,
            user_id=None,
            reason="Task execution exceeded timeout limit",
            change_source="system",
            metadata=metadata,
        )

        return success, message

    def auto_transition_to_completed(
        self,
        task_id: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Automatically transition task to completed state."""
        success, message, _ = self.change_task_status(
            task_id=task_id,
            new_status=TaskStatus.COMPLETED.value,
            user_id=None,
            reason="Task execution completed successfully",
            change_source="automatic",
            metadata=metadata,
        )

        return success, message

    def validate_operation(self, task_id: int, operation: str) -> Tuple[bool, str]:
        """
        Validate if an operation can be performed on a task.

        Args:
            task_id: Task ID
            operation: Operation to validate (start, pause, resume, stop)

        Returns:
            Tuple of (is_valid, message)
        """
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False, f"Task {task_id} not found"

            status = str(task.status)

            if operation == "start":
                return (
                    TaskStatusValidator.can_start(status),
                    "Task can be started"
                    if TaskStatusValidator.can_start(status)
                    else f"Cannot start task in {status} status",
                )
            elif operation == "pause":
                return (
                    TaskStatusValidator.can_pause(status),
                    "Task can be paused"
                    if TaskStatusValidator.can_pause(status)
                    else f"Cannot pause task in {status} status",
                )
            elif operation == "resume":
                return (
                    TaskStatusValidator.can_resume(status),
                    "Task can be resumed"
                    if TaskStatusValidator.can_resume(status)
                    else f"Cannot resume task in {status} status",
                )
            elif operation == "stop":
                return (
                    TaskStatusValidator.can_stop(status),
                    "Task can be stopped"
                    if TaskStatusValidator.can_stop(status)
                    else f"Cannot stop task in {status} status",
                )
            else:
                return False, f"Unknown operation: {operation}"

        except Exception as e:
            return False, f"Error validating operation: {str(e)}"

    def get_tasks_by_status(self, status: str, user_id: Optional[int] = None) -> List[Task]:
        """Get all tasks with a specific status."""
        try:
            query = self.db.query(Task).filter(Task.status == status)
            if user_id:
                query = query.filter(Task.user_id == user_id)
            return query.all()
        except Exception:
            return []

    def get_active_tasks(self, user_id: Optional[int] = None) -> List[Task]:
        """Get all active tasks."""
        active_statuses = TaskStatus.get_active_statuses()
        try:
            query = self.db.query(Task).filter(Task.status.in_(active_statuses))
            if user_id:
                query = query.filter(Task.user_id == user_id)
            return query.all()
        except Exception:
            return []

    def cleanup_terminal_tasks(self, older_than_days: int = 30) -> int:
        """Clean up old terminal tasks."""
        # This would implement cleanup logic for old completed/failed tasks
        # For now, just return 0 as placeholder
        return 0

    def _update_task_timestamps(self, task: Task, new_status: str) -> None:
        """Update task timestamps based on status transition."""
        now = utc_now()

        if new_status == TaskStatus.RUNNING.value and getattr(task, "started_at", None) is None:
            setattr(task, "started_at", now)
        elif new_status == TaskStatus.PAUSED.value:
            setattr(task, "paused_at", now)
        elif new_status == TaskStatus.STOPPED.value:
            setattr(task, "stopped_at", now)
        elif new_status == TaskStatus.COMPLETED.value:
            setattr(task, "completed_at", now)
        elif new_status in [TaskStatus.FAILED.value, TaskStatus.TIMEOUT.value]:
            if new_status == TaskStatus.FAILED.value:
                setattr(task, "stopped_at", now)
            else:  # TIMEOUT
                setattr(task, "completed_at", now)

    def get_transition_statistics(self, task_id: Optional[int] = None) -> Dict[str, Any]:
        """Get statistics about status transitions."""
        try:
            query = self.db.query(TaskHistory)
            if task_id:
                query = query.filter(TaskHistory.task_id == task_id)

            history = query.all()

            # Calculate statistics
            total_transitions = len(history)
            transitions_by_status = {}
            transitions_by_source = {}

            for entry in history:
                # Count by new status
                if entry.new_status not in transitions_by_status:
                    transitions_by_status[entry.new_status] = 0
                transitions_by_status[entry.new_status] += 1

                # Count by source
                if entry.change_source not in transitions_by_source:
                    transitions_by_source[entry.change_source] = 0
                transitions_by_source[entry.change_source] += 1

            return {
                "total_transitions": total_transitions,
                "transitions_by_status": transitions_by_status,
                "transitions_by_source": transitions_by_source,
            }

        except Exception as e:
            return {"error": f"Failed to get statistics: {str(e)}"}


def get_task_state_service(db: Session = None) -> TaskStateService:
    """Factory function to get TaskStateService instance."""
    if db is None:
        db = next(get_db())
    return TaskStateService(db)
