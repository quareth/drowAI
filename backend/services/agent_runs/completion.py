"""Internal completion envelope for process-local subagent runs.

This module keeps the safe public ``AgentResult`` separate from child LLM
usage records captured inside the child graph. The envelope is backend-local:
stream/lifecycle projections still expose only the bounded result, while the
subagent handler can merge child and parent usage exactly once before the
normal persistence path records usage rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.services.agent_runs.contracts import AgentAssignment, AgentResult


@dataclass(frozen=True, slots=True)
class AgentRunCompletion:
    """Terminal child result plus canonical child usage and run identity."""

    result: AgentResult
    usage_records: tuple[dict[str, Any], ...]
    tenant_id: int
    task_id: int
    user_id: int | None
    conversation_id: str
    provider: str | None
    model: str | None
    agent_id: str
    agent_kind: str
    agent_run_id: str
    graph_thread_id: str
    parent_turn_id: str
    parent_run_id: str | None


def build_agent_run_completion(
    *,
    result: AgentResult,
    assignment: AgentAssignment,
    graph_thread_id: str,
    final_state: Mapping[str, Any] | None = None,
    skip_usage_records: int = 0,
) -> AgentRunCompletion:
    """Build an internal completion envelope for one child graph run."""

    return AgentRunCompletion(
        result=result,
        usage_records=_child_usage_records(
            final_state,
            assignment=assignment,
            graph_thread_id=graph_thread_id,
            skip_usage_records=skip_usage_records,
        ),
        tenant_id=assignment.tenant_id,
        task_id=assignment.task_id,
        user_id=assignment.runtime_identity.user_id,
        conversation_id=assignment.conversation_id,
        provider=assignment.runtime_identity.provider,
        model=assignment.runtime_identity.model,
        agent_id=assignment.agent_id,
        agent_kind=assignment.agent_kind,
        agent_run_id=assignment.agent_run_id,
        graph_thread_id=graph_thread_id,
        parent_turn_id=assignment.parent_turn_id,
        parent_run_id=_optional_string(assignment.relevant_context.get("parent_run_id")),
    )


def usage_envelopes_from_child_records(
    usage_records: tuple[dict[str, Any], ...],
    *,
    execution_branch: str,
    turn_index: int | None,
) -> list[Any]:
    """Convert child trace usage dicts into canonical persistence envelopes."""

    from backend.services.langgraph_chat.handlers.turn_runtime import (
        extract_usage_records,
    )

    return (
        extract_usage_records(
            usage_records,
            execution_branch=execution_branch,
            turn_index=turn_index,
        )
        or []
    )


def child_usage_records_from_state(
    final_state: Mapping[str, Any] | None,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    skip_usage_records: int = 0,
) -> tuple[dict[str, Any], ...]:
    """Return identity-rich raw usage records from a child graph state."""

    return _child_usage_records(
        final_state,
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        skip_usage_records=skip_usage_records,
    )


def _child_usage_records(
    final_state: Mapping[str, Any] | None,
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    skip_usage_records: int = 0,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(final_state, Mapping):
        return ()
    trace = final_state.get("trace")
    raw_records = trace.get("usage_records") if isinstance(trace, Mapping) else None
    if not isinstance(raw_records, list | tuple):
        return ()

    records: list[dict[str, Any]] = []
    start = max(0, int(skip_usage_records))
    for raw_record in raw_records[start:]:
        if not isinstance(raw_record, Mapping):
            continue
        record = dict(raw_record)
        record["tenant_id"] = assignment.tenant_id
        record["task_id"] = assignment.task_id
        record["user_id"] = assignment.runtime_identity.user_id
        record["conversation_id"] = assignment.conversation_id
        record.setdefault("provider", assignment.runtime_identity.provider or "unknown")
        record.setdefault("model", assignment.runtime_identity.model or "unknown")
        record["agent_id"] = assignment.agent_id
        record["agent_kind"] = assignment.agent_kind
        record["agent_run_id"] = assignment.agent_run_id
        record["graph_thread_id"] = graph_thread_id
        record["parent_turn_id"] = assignment.parent_turn_id
        parent_run_id = _optional_string(assignment.relevant_context.get("parent_run_id"))
        if parent_run_id is not None:
            record["parent_run_id"] = parent_run_id
        turn_sequence = assignment.relevant_context.get("turn_sequence")
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
            record["turn_sequence"] = turn_sequence
        records.append(record)
    return tuple(records)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "AgentRunCompletion",
    "build_agent_run_completion",
    "child_usage_records_from_state",
    "usage_envelopes_from_child_records",
]
