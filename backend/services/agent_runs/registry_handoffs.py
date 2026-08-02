"""Pure process-local agent-run handoff claim policy.

This module owns handoff candidate selection decisions and immutable replacement
snapshots for claim, acknowledge, release, and one-shot consumption paths. It
does not own the registry's claim map, claim id generation, claim sequence,
run storage, mutation, metrics, logging, condition notification, or public
registry methods; the registry facade commits claim and run replacements.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .contracts import AgentResult
from .registry_contracts import (
    ACTIVE_AGENT_RUN_STATUSES,
    TERMINAL_AGENT_RUN_STATUSES,
    AgentRunKey,
    ClaimedHandoffBatch,
    LocalAgentRun,
)
from .registry_queries import run_sort_key


@dataclass(frozen=True, slots=True)
class ReadyHandoffProjection:
    """Pure snapshot of scoped handoff candidates and active runs."""

    candidates: tuple[LocalAgentRun, ...]
    claimed_ready_count: int
    active_runs: tuple[LocalAgentRun, ...]


@dataclass(frozen=True, slots=True)
class RunReplacement:
    """Pure replacement for one registry key that the facade may commit."""

    key: AgentRunKey
    entry: LocalAgentRun


@dataclass(frozen=True, slots=True)
class HandoffClaimDecision:
    """Pure claim decision with replacements and the public claim snapshot."""

    claim_keys: tuple[AgentRunKey, ...]
    replacements: tuple[RunReplacement, ...]
    batch: ClaimedHandoffBatch


@dataclass(frozen=True, slots=True)
class SettlementDecision:
    """Pure acknowledge/release replacements and affected entry count."""

    replacements: tuple[RunReplacement, ...]
    affected_count: int


@dataclass(frozen=True, slots=True)
class ConsumptionDecision:
    """Pure one-shot consumption decision for a single terminal result."""

    result: AgentResult | None
    replacement: RunReplacement | None


def agent_run_key(entry: LocalAgentRun) -> AgentRunKey:
    """Return the registry key for one immutable run snapshot."""

    return (entry.tenant_id, entry.task_id, entry.agent_run_id)


def select_ready_handoffs(
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


def build_claim_decision(
    projection: ReadyHandoffProjection,
    *,
    claim_id: str,
    tenant_id: int,
    task_id: int,
) -> HandoffClaimDecision:
    """Return claim replacements and public batch for selected candidates."""

    keys: list[AgentRunKey] = []
    results: list[AgentResult] = []
    replacements: list[RunReplacement] = []
    for entry in projection.candidates:
        key = agent_run_key(entry)
        keys.append(key)
        assert entry.result is not None
        results.append(entry.result)
        replacements.append(
            RunReplacement(
                key=key,
                entry=replace(entry, result_claim_id=claim_id),
            )
        )
    batch = ClaimedHandoffBatch(
        claim_id=claim_id,
        tenant_id=tenant_id,
        task_id=task_id,
        agent_run_ids=tuple(entry.agent_run_id for entry in projection.candidates),
        results=tuple(results),
        active_runs=projection.active_runs,
    )
    return HandoffClaimDecision(
        claim_keys=tuple(keys),
        replacements=tuple(replacements),
        batch=batch,
    )


def build_acknowledge_replacements(
    runs: Mapping[AgentRunKey, LocalAgentRun],
    *,
    claim_keys: tuple[AgentRunKey, ...],
    claim_id: str,
) -> SettlementDecision:
    """Return replacements for entries acknowledged by the matching claim."""

    replacements: list[RunReplacement] = []
    for key in claim_keys:
        entry = runs.get(key)
        if entry is None or entry.result_claim_id != claim_id:
            continue
        replacements.append(
            RunReplacement(
                key=key,
                entry=replace(entry, result_consumed=True, result_claim_id=None),
            )
        )
    return SettlementDecision(
        replacements=tuple(replacements),
        affected_count=len(replacements),
    )


def build_release_replacements(
    runs: Mapping[AgentRunKey, LocalAgentRun],
    *,
    claim_keys: tuple[AgentRunKey, ...],
    claim_id: str,
) -> SettlementDecision:
    """Return replacements for entries released by the matching claim."""

    replacements: list[RunReplacement] = []
    for key in claim_keys:
        entry = runs.get(key)
        if (
            entry is None
            or entry.result_claim_id != claim_id
            or entry.result_consumed
        ):
            continue
        replacements.append(
            RunReplacement(
                key=key,
                entry=replace(entry, result_claim_id=None),
            )
        )
    return SettlementDecision(
        replacements=tuple(replacements),
        affected_count=len(replacements),
    )


def build_consumption_decision(
    entry: LocalAgentRun | None,
) -> ConsumptionDecision:
    """Return one-shot result consumption eligibility and replacement."""

    if (
        entry is None
        or entry.result is None
        or entry.result_consumed
        or entry.result_claim_id is not None
        or entry.status not in TERMINAL_AGENT_RUN_STATUSES
    ):
        return ConsumptionDecision(result=None, replacement=None)
    return ConsumptionDecision(
        result=entry.result,
        replacement=RunReplacement(
            key=agent_run_key(entry),
            entry=replace(entry, result_consumed=True),
        ),
    )


__all__ = [
    "ConsumptionDecision",
    "HandoffClaimDecision",
    "ReadyHandoffProjection",
    "RunReplacement",
    "SettlementDecision",
    "agent_run_key",
    "build_acknowledge_replacements",
    "build_claim_decision",
    "build_consumption_decision",
    "build_release_replacements",
    "select_ready_handoffs",
]
