"""
Task management services package.

This package contains service classes for task lifecycle management,
state transitions, and business logic enforcement.
"""

from importlib import import_module

def __getattr__(name):
    if name == "WorkspaceManager":
        from .workspace.manager import WorkspaceManager

        return WorkspaceManager
    if name == "TaskStateService":
        from .task.state_service import TaskStateService

        return TaskStateService
    try:
        return import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "WorkspaceManager",
    "TaskStateService"
]
