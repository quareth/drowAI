"""Process-local status and cancellation service for subagent runs.

This service projects safe live registry state for task-scoped HTTP handlers and
routes cancellation through the process-local launcher/registry boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import (
    AgentAssignment,
    AgentKind,
    AgentResultProjection,
    AgentRunStatus,
    agent_display_name,
)
from .registry import (
    ACTIVE_AGENT_RUN_STATUSES,
    AgentRunNotFoundError,
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)


class ScopedAgentRunLauncher(Protocol):
    """Launcher interface needed by status/cancel control paths."""

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        """Signal cancellation for one scoped process-local run."""


class AgentRunMissingError(LookupError):
    """Raised when no process-local run exists for the scoped key."""


class AgentRunNotActiveError(RuntimeError):
    """Raised when a process-local run exists but cannot be cancelled."""


class LocalAgentRunStatusProjection(BaseModel):
    """Safe process-local status payload for one subagent run."""

    model_config = ConfigDict(extra="forbid")

    agent_run_id: str
    agent_kind: AgentKind
    agent_display_name: str
    status: AgentRunStatus
    lifecycle_version: int
    task_id: int
    conversation_id: str
    parent_turn_id: str
    assignment: AgentAssignment
    result: AgentResultProjection | None = None
    safe_error: str | None = None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_entry(cls, entry: LocalAgentRun) -> "LocalAgentRunStatusProjection":
        """Project a registry entry without exposing live task handles."""
        return cls(
            agent_run_id=entry.agent_run_id,
            agent_kind=entry.agent_kind,
            agent_display_name=agent_display_name(entry.agent_kind),
            status=entry.status,
            lifecycle_version=entry.lifecycle_version,
            task_id=entry.task_id,
            conversation_id=entry.conversation_id,
            parent_turn_id=entry.parent_turn_id,
            assignment=entry.assignment,
            result=(
                None
                if entry.result is None
                else AgentResultProjection.from_result(entry.result)
            ),
            safe_error=entry.safe_error,
            cancel_requested=entry.cancel_requested,
            created_at=entry.created_at,
            started_at=entry.started_at,
            completed_at=entry.completed_at,
        )


class AgentRunControlService:
    """Task-scoped process-local status/cancel facade."""

    def __init__(
        self,
        *,
        registry: ProcessLocalAgentRunRegistry,
        launcher: ScopedAgentRunLauncher,
    ) -> None:
        self._registry = registry
        self._launcher = launcher

    async def list_local_runs(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> list[LocalAgentRunStatusProjection]:
        """Return process-local runs visible in one authorized task scope."""
        entries = await self._registry.list_task_runs(
            tenant_id=tenant_id,
            task_id=task_id,
        )
        entries.sort(key=lambda entry: (entry.created_at, entry.agent_run_id))
        return [LocalAgentRunStatusProjection.from_entry(entry) for entry in entries]

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRunStatusProjection:
        """Request cancellation for one active process-local Scout run."""
        entry = await self._registry.get(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if entry is None:
            raise AgentRunMissingError(agent_run_id)
        if entry.status not in ACTIVE_AGENT_RUN_STATUSES:
            raise AgentRunNotActiveError(agent_run_id)

        try:
            updated = await self._launcher.request_cancellation(
                tenant_id=tenant_id,
                task_id=task_id,
                agent_run_id=agent_run_id,
            )
        except AgentRunNotFoundError as exc:
            raise AgentRunMissingError(agent_run_id) from exc
        cancelled_by_request = (
            updated.status == "cancelled" and updated.cancel_requested is True
        )
        if updated.status not in ACTIVE_AGENT_RUN_STATUSES and not cancelled_by_request:
            raise AgentRunNotActiveError(agent_run_id)
        return LocalAgentRunStatusProjection.from_entry(updated)


__all__ = [
    "AgentRunControlService",
    "AgentRunMissingError",
    "AgentRunNotActiveError",
    "LocalAgentRunStatusProjection",
    "ScopedAgentRunLauncher",
]
