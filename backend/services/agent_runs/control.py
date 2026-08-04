"""Process-local status and cancellation service for subagent runs.

This service projects safe live registry state for task-scoped HTTP handlers and
routes cancellation through the process-local launcher/registry boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.subagents.registry import SubagentDisplayMetadata, SubagentRegistry
from pydantic import BaseModel, ConfigDict

from .contracts import (
    AgentAssignmentProjection,
    AgentKind,
    AgentResultProjection,
    AgentRunStatus,
)
from .registry import ProcessLocalAgentRunRegistry
from .registry_contracts import (
    ACTIVE_AGENT_RUN_STATUSES,
    AgentRunNotFoundError,
    LocalAgentRun,
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


class PendingInterruptTicketService(Protocol):
    """Durable ticket operation required by the subagent cancellation path."""

    def fail_pending_for_graph_thread(
        self,
        *,
        tenant_id: int,
        task_id: int,
        graph_name: str,
        graph_thread_id: str,
    ) -> object | None:
        """Fail the pending ticket owned by one exact child graph thread."""


class AgentRunMissingError(LookupError):
    """Raised when no process-local run exists for the scoped key."""


class AgentRunNotActiveError(RuntimeError):
    """Raised when a process-local run exists but cannot be cancelled."""


class LocalAgentRunStatusProjection(BaseModel):
    """Safe process-local status payload for one subagent run."""

    model_config = ConfigDict(extra="forbid")

    agent_run_id: str
    agent_id: str
    agent_kind: AgentKind
    agent_display_name: str
    agent_icon_key: str
    status: AgentRunStatus
    lifecycle_version: int
    task_id: int
    conversation_id: str
    parent_turn_id: str
    assignment: AgentAssignmentProjection
    result: AgentResultProjection | None = None
    safe_error: str | None = None
    cancel_requested: bool
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_entry(
        cls,
        entry: LocalAgentRun,
        *,
        display_metadata: SubagentDisplayMetadata,
    ) -> "LocalAgentRunStatusProjection":
        """Project a registry entry without exposing live task handles."""
        return cls(
            agent_run_id=entry.agent_run_id,
            agent_id=entry.agent_id,
            agent_kind=entry.agent_kind,
            agent_display_name=display_metadata.display_name,
            agent_icon_key=display_metadata.icon,
            status=entry.status,
            lifecycle_version=entry.lifecycle_version,
            task_id=entry.task_id,
            conversation_id=entry.conversation_id,
            parent_turn_id=entry.parent_turn_id,
            assignment=AgentAssignmentProjection.from_assignment(entry.assignment),
            result=(
                None
                if entry.result is None
                else AgentResultProjection.from_result(
                    entry.result,
                    agent_display_name=display_metadata.display_name,
                )
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
        subagent_registry: SubagentRegistry,
        interrupt_ticket_service: PendingInterruptTicketService,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._subagent_registry = subagent_registry
        self._interrupt_ticket_service = interrupt_ticket_service

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
        return [self._project_entry(entry) for entry in entries]

    async def request_cancellation(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRunStatusProjection:
        """Request cancellation for one active process-local subagent run."""
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
        if entry.status == "waiting_for_approval" and cancelled_by_request:
            self._fail_waiting_approval_ticket(updated)
        return self._project_entry(updated)

    def _fail_waiting_approval_ticket(self, entry: LocalAgentRun) -> None:
        """Retire a durable approval that can no longer resume its child owner."""
        self._interrupt_ticket_service.fail_pending_for_graph_thread(
            tenant_id=entry.tenant_id,
            task_id=entry.task_id,
            graph_name=GRAPH_NAME_SUBAGENT,
            graph_thread_id=entry.graph_thread_id,
        )

    def _project_entry(self, entry: LocalAgentRun) -> LocalAgentRunStatusProjection:
        return LocalAgentRunStatusProjection.from_entry(
            entry,
            display_metadata=self._subagent_registry.display_metadata(entry.agent_id),
        )


__all__ = [
    "AgentRunControlService",
    "AgentRunMissingError",
    "AgentRunNotActiveError",
    "LocalAgentRunStatusProjection",
    "PendingInterruptTicketService",
    "ScopedAgentRunLauncher",
]
