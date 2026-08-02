"""Pure process-local agent-run query and retention policy.

This module owns filtering, ordering, cleanup eligibility, and wait-status
projections for immutable process-local registry snapshots. It does not own run
or claim storage, mutation, deletion, claim assignment, metrics, logging,
state-version increments, condition notification, clocks, or public registry
methods; those remain in ``registry.py`` until callers are migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

from .registry_contracts import (
    ACTIVE_AGENT_RUN_STATUSES,
    TERMINAL_AGENT_RUN_STATUSES,
    AgentRunKey,
    HandoffWaitStatus,
    LocalAgentRun,
)


@dataclass(frozen=True, slots=True)
class ReadyHandoffProjection:
    """Pure snapshot of scoped handoff candidates and active runs."""

    candidates: tuple[LocalAgentRun, ...]
    claimed_ready_count: int
    active_runs: tuple[LocalAgentRun, ...]


def datetime_sort_key(value: datetime | None) -> str:
    """Return the current nullable datetime sort key."""

    if value is None:
        return ""
    return value.isoformat()


def run_sort_key(entry: LocalAgentRun) -> tuple[str, str, str]:
    """Return the deterministic sort key for handoff and active-run projections."""

    return (
        datetime_sort_key(entry.completed_at or entry.started_at),
        datetime_sort_key(entry.created_at),
        entry.agent_run_id,
    )


def list_task_runs(
    entries: Iterable[LocalAgentRun],
    *,
    tenant_id: int,
    task_id: int,
) -> list[LocalAgentRun]:
    """Return task-scoped runs while preserving input insertion order."""

    return [
        entry
        for entry in entries
        if entry.tenant_id == tenant_id and entry.task_id == task_id
    ]


def find_active_by_graph_thread(
    entries: Iterable[LocalAgentRun],
    *,
    task_id: int,
    graph_thread_id: str,
    tenant_id: int | None = None,
) -> LocalAgentRun | None:
    """Return the unique active run for a child graph thread, or ``None``."""

    candidates = [
        entry
        for entry in entries
        if entry.task_id == task_id
        and entry.graph_thread_id == graph_thread_id
        and entry.status in ACTIVE_AGENT_RUN_STATUSES
        and (tenant_id is None or entry.tenant_id == tenant_id)
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def select_stale_finished_keys(
    runs: Mapping[AgentRunKey, LocalAgentRun],
    *,
    cutoff: datetime,
) -> list[AgentRunKey]:
    """Return cleanup-eligible keys using the current exact cutoff rules."""

    return [
        key
        for key, entry in runs.items()
        if entry.completed_at is not None
        and entry.completed_at <= cutoff
        and entry.result_claim_id is None
    ]


def handoff_wait_status(
    entries: Iterable[LocalAgentRun],
    *,
    tenant_id: int,
    task_id: int,
    conversation_id: str | None,
) -> HandoffWaitStatus | None:
    """Return scoped handoff readiness, inactivity, or active-wait state."""

    has_active = False
    for entry in entries:
        if (
            entry.tenant_id != tenant_id
            or entry.task_id != task_id
            or (
                conversation_id is not None
                and entry.conversation_id != conversation_id
            )
        ):
            continue
        if (
            entry.result is not None
            and not entry.result_consumed
            and entry.result_claim_id is None
            and entry.status in TERMINAL_AGENT_RUN_STATUSES
        ):
            return "ready"
        if entry.status in ACTIVE_AGENT_RUN_STATUSES:
            has_active = True
    if has_active:
        return None
    return "inactive"


def project_ready_handoffs(
    entries: Iterable[LocalAgentRun],
    *,
    tenant_id: int,
    task_id: int,
    conversation_id: str | None = None,
    max_results: int | None = None,
) -> ReadyHandoffProjection:
    """Return sorted ready terminal candidates and scoped active snapshots."""

    candidates: list[LocalAgentRun] = []
    claimed_ready_count = 0
    active_entries: list[LocalAgentRun] = []
    for entry in entries:
        if (
            entry.tenant_id != tenant_id
            or entry.task_id != task_id
            or (
                conversation_id is not None
                and entry.conversation_id != conversation_id
            )
        ):
            continue
        if entry.status in ACTIVE_AGENT_RUN_STATUSES:
            active_entries.append(entry)
        if (
            entry.result is None
            or entry.result_consumed
            or entry.status not in TERMINAL_AGENT_RUN_STATUSES
        ):
            continue
        if entry.result_claim_id is None:
            candidates.append(entry)
        else:
            claimed_ready_count += 1

    candidates.sort(key=run_sort_key)
    if max_results is not None:
        candidates = candidates[:max_results]
    return ReadyHandoffProjection(
        candidates=tuple(candidates),
        claimed_ready_count=claimed_ready_count,
        active_runs=tuple(sorted(active_entries, key=run_sort_key)),
    )


__all__ = [
    "ReadyHandoffProjection",
    "datetime_sort_key",
    "find_active_by_graph_thread",
    "handoff_wait_status",
    "list_task_runs",
    "project_ready_handoffs",
    "run_sort_key",
    "select_stale_finished_keys",
]
