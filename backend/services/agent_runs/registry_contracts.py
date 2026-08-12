"""Process-local agent-run registry contracts.

This module owns immutable process-local registry snapshots, status constants,
type aliases, and registry-specific errors. It does not own run storage,
claim storage, lifecycle mutation, task cancellation, metrics, logging, or
state-change signaling; those responsibilities are split across the registry
facade and the focused lifecycle, query, handoff, and signaling modules.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, TypeAlias

from .contracts import AgentAssignment, AgentKind, AgentResult, AgentRunStatus


AgentRunKey: TypeAlias = tuple[int, int, str]
HandoffWaitStatus: TypeAlias = Literal["ready", "inactive"]

ACTIVE_AGENT_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {"queued", "running", "waiting_for_approval"}
)
TERMINAL_AGENT_RUN_STATUSES: frozenset[AgentRunStatus] = frozenset(
    {"completed", "interrupted", "cancelled"}
)
DEFAULT_FINISHED_RETENTION = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class LocalAgentRun:
    """Immutable snapshot of one process-local subagent run."""

    graph_thread_id: str
    assignment: AgentAssignment
    status: AgentRunStatus
    lifecycle_version: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: AgentResult | None
    safe_error: str | None
    task_handle: asyncio.Task[Any] | None
    cancel_requested: bool
    result_consumed: bool
    result_claim_id: str | None
    accounted_usage_record_count: int

    @property
    def agent_run_id(self) -> str:
        return self.assignment.agent_run_id

    @property
    def agent_id(self) -> str:
        return self.assignment.agent_id

    @property
    def tenant_id(self) -> int:
        return self.assignment.tenant_id

    @property
    def task_id(self) -> int:
        return self.assignment.task_id

    @property
    def conversation_id(self) -> str:
        return self.assignment.conversation_id

    @property
    def parent_turn_id(self) -> str:
        return self.assignment.parent_turn_id

    @property
    def agent_kind(self) -> AgentKind:
        return self.assignment.agent_kind


@dataclass(frozen=True, slots=True)
class ClaimedHandoffBatch:
    """Process-local claim over ready terminal results for one parent task."""

    claim_id: str
    tenant_id: int
    task_id: int
    agent_run_ids: tuple[str, ...]
    results: tuple[AgentResult, ...]
    active_runs: tuple[LocalAgentRun, ...]


@dataclass(frozen=True, slots=True)
class AgentRunTransition:
    """Result of one lifecycle transition attempt against a local run."""

    entry: LocalAgentRun
    changed: bool


class ActiveAgentRunExistsError(RuntimeError):
    """Legacy error retained for callers that handle old singleton conflicts."""

    def __init__(
        self,
        *,
        tenant_id: int,
        task_id: int,
        active_agent_run_id: str,
    ) -> None:
        super().__init__(
            "An active process-local subagent run already exists for "
            f"tenant_id={tenant_id}, task_id={task_id}: {active_agent_run_id}"
        )
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.active_agent_run_id = active_agent_run_id


class AgentRunNotFoundError(KeyError):
    """Raised when a process-local subagent run key is not present."""


class AgentRunIdentityCollisionError(RuntimeError):
    """Raised when a scoped run id is reused for different immutable identity."""

    def __init__(self, *, tenant_id: int, task_id: int, agent_run_id: str) -> None:
        super().__init__(
            "Agent run identity collision for "
            f"tenant_id={tenant_id}, task_id={task_id}, agent_run_id={agent_run_id}"
        )
        self.tenant_id = tenant_id
        self.task_id = task_id
        self.agent_run_id = agent_run_id


class HandoffClaimNotFoundError(KeyError):
    """Raised when a process-local handoff claim is not present."""


__all__ = [
    "ACTIVE_AGENT_RUN_STATUSES",
    "DEFAULT_FINISHED_RETENTION",
    "TERMINAL_AGENT_RUN_STATUSES",
    "ActiveAgentRunExistsError",
    "AgentRunKey",
    "AgentRunIdentityCollisionError",
    "AgentRunNotFoundError",
    "AgentRunTransition",
    "ClaimedHandoffBatch",
    "HandoffClaimNotFoundError",
    "HandoffWaitStatus",
    "LocalAgentRun",
]
