"""Task-scoped runtime context for direct tool execution.

This module provides a context-local channel for execution metadata (for
example active `task_id`) that must be injected by the runtime rather than
accepted from model-visible tool arguments.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    """Execution metadata bound to one direct tool invocation."""

    task_id: int
    tenant_id: Optional[int] = None
    workspace_path: Optional[str] = None
    host_workspace_path: Optional[str] = None
    container_workspace_path: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_batch_id: Optional[str] = None
    artifact_stamp: Optional[int] = None
    interrupt_id: Optional[str] = None


_ACTIVE_RUNTIME_CONTEXT: ContextVar[Optional[ToolRuntimeContext]] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def coerce_task_id(task_id: object) -> Optional[int]:
    """Return a positive integer task id or ``None`` when unavailable/invalid."""
    try:
        parsed = int(task_id)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def build_tool_runtime_context(
    *,
    task_id: object,
    tenant_id: object = None,
    workspace_path: Optional[str] = None,
    host_workspace_path: Optional[str] = None,
    container_workspace_path: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    tool_batch_id: Optional[str] = None,
    artifact_stamp: Optional[int] = None,
    interrupt_id: Optional[str] = None,
) -> Optional[ToolRuntimeContext]:
    """Build a validated runtime context object or ``None`` when task scope is absent."""
    parsed_task_id = coerce_task_id(task_id)
    if parsed_task_id is None:
        return None
    parsed_tenant_id = coerce_task_id(tenant_id)
    return ToolRuntimeContext(
        task_id=parsed_task_id,
        tenant_id=parsed_tenant_id,
        workspace_path=workspace_path,
        host_workspace_path=host_workspace_path,
        container_workspace_path=container_workspace_path,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        artifact_stamp=artifact_stamp,
        interrupt_id=interrupt_id,
    )


@contextmanager
def bind_tool_runtime_context(context: Optional[ToolRuntimeContext]) -> Iterator[None]:
    """Bind runtime context for one tool call and always restore previous state."""
    token = _ACTIVE_RUNTIME_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_RUNTIME_CONTEXT.reset(token)


def get_tool_runtime_context() -> Optional[ToolRuntimeContext]:
    """Return the active runtime context for the current execution flow."""
    return _ACTIVE_RUNTIME_CONTEXT.get()


__all__ = [
    "ToolRuntimeContext",
    "bind_tool_runtime_context",
    "build_tool_runtime_context",
    "coerce_task_id",
    "get_tool_runtime_context",
]
