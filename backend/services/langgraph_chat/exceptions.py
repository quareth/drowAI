"""Custom exceptions for LangGraph chat and persistence workflows."""

from typing import Optional


class HITLError(RuntimeError):
    """Raised when HITL interrupt or resume operations fail."""


class PersistenceError(RuntimeError):
    """Raised when event persistence fails.
    
    This exception is raised when database writes fail. It includes the
    task_id for context.
    
    Attributes:
        task_id: The task ID that failed to persist (if known)
        message: Error description
    """
    
    def __init__(self, message: str, task_id: Optional[int] = None) -> None:
        """Initialize persistence error.
        
        Args:
            message: Error description
            task_id: Task ID that failed (optional)
        """
        self.task_id = task_id
        super().__init__(message)
    
    def __str__(self) -> str:
        """Return string representation with task context."""
        if self.task_id is not None:
            return f"[task={self.task_id}] {super().__str__()}"
        return super().__str__()


class PlanModeUnavailableError(RuntimeError):
    """Raised when an `agent_mode=plan` turn cannot be served.

    Phase 5 fail-closed contract: when the user explicitly selects the
    Plan tier but deep reasoning is disabled at the deployment level,
    the facade rejects the turn instead of silently downgrading to
    normal chat. Silent downgrade reproduces the original Plan-tier
    bug in a harder-to-debug way (the user thinks they got planner
    output; they actually got a chat reply).
    """


__all__ = [
    "HITLError",
    "PersistenceError",
    "PlanModeUnavailableError",
]
