"""Subagent HITL continuation checks for process-local runs.

This module bridges existing task-bound interrupt tickets to the process-local
subagent run registry. It does not own approval payloads, routes, persistence
schema, or graph execution; it only verifies that a canonical subagent ticket still
maps to a live local child thread before checkpoint continuation resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from backend.database import SessionLocal
from backend.models.hitl import InterruptTicket
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    normalize_graph_thread_id,
)

from .registry import (
    ACTIVE_AGENT_RUN_STATUSES,
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)


SUBAGENT_RECOVERY_ERROR = (
    "Subagent approval cannot be resumed because the live process-local registry "
    "entry is missing. Start a new subagent run."
)
_LEGACY_SCOUT_GRAPH_NAME = "scout_recon"
_SUBAGENT_GRAPH_NAMES = frozenset({GRAPH_NAME_SUBAGENT, _LEGACY_SCOUT_GRAPH_NAME})


@dataclass(frozen=True, slots=True)
class SubagentContinuationContext:
    """Verified continuation identity for one live subagent approval resume."""

    entry: LocalAgentRun
    graph_thread_id: str
    checkpoint_id: str | None


@dataclass(frozen=True, slots=True)
class SubagentInterruptTicketSnapshot:
    """Minimal canonical ticket identity needed to resume a subagent child graph."""

    graph_name: str
    thread_id: str | None
    checkpoint_id: str | None


class SubagentContinuationError(RuntimeError):
    """Raised when a subagent approval cannot be safely resumed in this process."""


async def prepare_subagent_resume(
    *,
    registry: ProcessLocalAgentRunRegistry,
    task_id: int,
    tenant_id: int | None,
    graph_name: str,
    interrupt_id: str | None,
    checkpoint_id: int | str | None,
    session_factory: Callable[[], Any] = SessionLocal,
) -> SubagentContinuationContext | None:
    """Verify a subagent resume and return the canonical child checkpoint identity."""

    if not is_subagent_graph_name(graph_name):
        return None
    if tenant_id is None:
        raise SubagentContinuationError("Subagent resume requires tenant identity")
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        raise SubagentContinuationError(
            "Subagent resume requires canonical interrupt identity"
        )

    ticket = _load_subagent_resume_ticket(
        session_factory=session_factory,
        task_id=task_id,
        interrupt_id=interrupt_id.strip(),
    )
    if ticket is None or not is_subagent_graph_name(ticket.graph_name):
        raise SubagentContinuationError(SUBAGENT_RECOVERY_ERROR)

    graph_thread_id = _normalize_ticket_thread_id(ticket.thread_id)
    if graph_thread_id is None:
        raise SubagentContinuationError(
            "Subagent resume ticket is missing child thread identity"
        )

    entry = await _find_live_subagent_entry(
        registry=registry,
        tenant_id=int(tenant_id),
        task_id=int(task_id),
        graph_thread_id=graph_thread_id,
    )
    if entry is None:
        raise SubagentContinuationError(SUBAGENT_RECOVERY_ERROR)

    canonical_checkpoint = ticket.checkpoint_id
    if canonical_checkpoint is None and checkpoint_id is not None:
        canonical_checkpoint = str(checkpoint_id)
    return SubagentContinuationContext(
        entry=entry,
        graph_thread_id=graph_thread_id,
        checkpoint_id=canonical_checkpoint,
    )


async def mark_subagent_running(
    *,
    registry: ProcessLocalAgentRunRegistry,
    context: SubagentContinuationContext | None,
) -> None:
    """Move a verified live subagent run out of waiting state for resume work."""

    if context is None:
        return
    await registry.mark_running(
        tenant_id=context.entry.tenant_id,
        task_id=context.entry.task_id,
        agent_run_id=context.entry.agent_run_id,
    )


async def mark_subagent_waiting_for_approval(
    *,
    registry: ProcessLocalAgentRunRegistry,
    context: SubagentContinuationContext | None,
) -> None:
    """Record that a continued subagent run paused again on the shared HITL gate."""

    if context is None:
        return
    await registry.mark_waiting_for_approval(
        tenant_id=context.entry.tenant_id,
        task_id=context.entry.task_id,
        agent_run_id=context.entry.agent_run_id,
    )


def _load_subagent_resume_ticket(
    *,
    session_factory: Callable[[], Any],
    task_id: int,
    interrupt_id: str,
) -> SubagentInterruptTicketSnapshot | None:
    db = session_factory()
    try:
        ticket = (
            db.query(InterruptTicket)
            .filter(
                InterruptTicket.task_id == int(task_id),
                InterruptTicket.interrupt_id == interrupt_id,
            )
            .order_by(InterruptTicket.updated_at.desc(), InterruptTicket.id.desc())
            .first()
        )
        if ticket is None:
            return None
        return SubagentInterruptTicketSnapshot(
            graph_name=str(ticket.graph_name or ""),
            thread_id=ticket.thread_id,
            checkpoint_id=ticket.checkpoint_id,
        )
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


async def _find_live_subagent_entry(
    *,
    registry: ProcessLocalAgentRunRegistry,
    tenant_id: int,
    task_id: int,
    graph_thread_id: str,
) -> LocalAgentRun | None:
    entries = await registry.list_task_runs(tenant_id=tenant_id, task_id=task_id)
    for entry in entries:
        if (
            entry.graph_thread_id == graph_thread_id
            and entry.status in ACTIVE_AGENT_RUN_STATUSES
        ):
            return entry
    return None


def _normalize_ticket_thread_id(thread_id: str | None) -> str | None:
    if not isinstance(thread_id, str):
        return None
    normalized = thread_id.strip()
    if normalized.startswith("graph-"):
        normalized = normalized.removeprefix("graph-")
    return normalize_graph_thread_id(normalized)


def is_subagent_graph_name(graph_name: str | None) -> bool:
    """Return whether a graph name refers to the generic subagent child graph."""

    return isinstance(graph_name, str) and graph_name.strip() in _SUBAGENT_GRAPH_NAMES


__all__ = [
    "SUBAGENT_RECOVERY_ERROR",
    "SubagentContinuationContext",
    "SubagentContinuationError",
    "SubagentInterruptTicketSnapshot",
    "is_subagent_graph_name",
    "mark_subagent_running",
    "mark_subagent_waiting_for_approval",
    "prepare_subagent_resume",
]
