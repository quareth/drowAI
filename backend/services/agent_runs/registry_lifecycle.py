"""Pure process-local agent-run lifecycle construction.

This module owns immutable next-snapshot construction and terminal fallback
result construction for process-local registry entries. It does not own run
storage, validation order, clocks, task cancellation, metrics, logging,
state-version increments, condition notification, or public registry mutation;
the registry facade remains the mutation authority.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .contracts import AgentAssignment, AgentResult, AgentRunStatus
from .registry_contracts import (
    TERMINAL_AGENT_RUN_STATUSES,
    LocalAgentRun,
)


def build_queued_entry(
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    created_at: datetime,
) -> LocalAgentRun:
    """Return the immutable queued snapshot for a newly registered run."""

    return LocalAgentRun(
        graph_thread_id=graph_thread_id,
        assignment=assignment,
        status="queued",
        lifecycle_version=1,
        created_at=created_at,
        started_at=None,
        completed_at=None,
        result=None,
        safe_error=None,
        task_handle=None,
        cancel_requested=False,
        result_consumed=False,
        result_claim_id=None,
        accounted_usage_record_count=0,
    )


def build_running_entry(
    entry: LocalAgentRun,
    *,
    started_at: datetime,
) -> LocalAgentRun:
    """Return the running snapshot, preserving any existing start timestamp."""

    return replace(
        entry,
        status="running",
        lifecycle_version=entry.lifecycle_version + 1,
        started_at=entry.started_at or started_at,
    )


def build_waiting_for_approval_entry(
    entry: LocalAgentRun,
    *,
    accounted_usage_record_count: int | None,
) -> LocalAgentRun:
    """Return the approval-wait snapshot with current usage count semantics."""

    accounted_count = (
        entry.accounted_usage_record_count
        if accounted_usage_record_count is None
        else max(0, int(accounted_usage_record_count))
    )
    return replace(
        entry,
        status="waiting_for_approval",
        lifecycle_version=entry.lifecycle_version + 1,
        accounted_usage_record_count=accounted_count,
    )


def build_cancellation_requested_entry(entry: LocalAgentRun) -> LocalAgentRun:
    """Return the non-terminal cancellation-requested snapshot."""

    return replace(entry, cancel_requested=True)


def build_completed_entry(
    entry: LocalAgentRun,
    *,
    result: AgentResult,
    completed_at: datetime,
) -> LocalAgentRun:
    """Return the completed terminal snapshot."""

    return build_terminal_entry(
        entry,
        status="completed",
        completed_at=completed_at,
        result=result,
    )


def build_failed_entry(
    entry: LocalAgentRun,
    *,
    safe_error: str,
    completed_at: datetime,
) -> LocalAgentRun:
    """Return the failed terminal snapshot and fallback result."""

    return build_terminal_entry(
        entry,
        status="failed",
        completed_at=completed_at,
        safe_error=safe_error,
    )


def build_cancelled_entry(
    entry: LocalAgentRun,
    *,
    completed_at: datetime,
    cancel_requested: bool | None = True,
) -> LocalAgentRun:
    """Return the cancelled terminal snapshot and fallback result."""

    return build_terminal_entry(
        entry,
        status="cancelled",
        completed_at=completed_at,
        cancel_requested=cancel_requested,
    )


def build_terminal_entry(
    entry: LocalAgentRun,
    *,
    status: AgentRunStatus,
    completed_at: datetime,
    result: AgentResult | None = None,
    safe_error: str | None = None,
    cancel_requested: bool | None = None,
) -> LocalAgentRun:
    """Return a terminal snapshot using the registry's current semantics."""

    terminal_result = result
    if terminal_result is None and status in {"failed", "cancelled"}:
        terminal_result = fallback_terminal_result(
            entry,
            status=status,
            safe_error=safe_error,
        )
    return replace(
        entry,
        status=status,
        lifecycle_version=entry.lifecycle_version + 1,
        completed_at=completed_at,
        result=terminal_result,
        safe_error=safe_error,
        task_handle=None,
        cancel_requested=entry.cancel_requested
        if cancel_requested is None
        else cancel_requested,
    )


def is_terminal(entry: LocalAgentRun) -> bool:
    """Return whether a registry snapshot is in a terminal lifecycle state."""

    return entry.status in TERMINAL_AGENT_RUN_STATUSES


def fallback_terminal_result(
    entry: LocalAgentRun,
    *,
    status: AgentRunStatus,
    safe_error: str | None,
) -> AgentResult:
    """Return the current fallback result for failed and cancelled snapshots."""

    if status == "failed":
        summary = f"Subagent run failed: {safe_error or 'Subagent worker failed'}"
        limitations = (safe_error or "Subagent worker failed",)
        recommended_next_steps = (
            "Review the failure and decide whether a new bounded assignment is needed.",
        )
    elif status == "cancelled":
        summary = "Subagent run was cancelled before completing its assignment."
        limitations = ("Subagent run was cancelled.",)
        recommended_next_steps = (
            "Decide whether the cancelled assignment is still required.",
        )
    else:
        raise ValueError(
            f"fallback result is only supported for terminal status: {status}"
        )
    return AgentResult(
        agent_run_id=entry.agent_run_id,
        agent_id=entry.agent_id,
        agent_kind=entry.agent_kind,
        outcome=status,
        summary=summary,
        limitations=limitations,
        recommended_next_steps=recommended_next_steps,
    )


__all__ = [
    "build_cancelled_entry",
    "build_cancellation_requested_entry",
    "build_completed_entry",
    "build_failed_entry",
    "build_queued_entry",
    "build_running_entry",
    "build_terminal_entry",
    "build_waiting_for_approval_entry",
    "fallback_terminal_result",
    "is_terminal",
]
