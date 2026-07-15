"""Parallel-execution compatibility checks for tool batches (Phase 4 Task 4.2).

Inspects the tools committed by the builder and decides whether they may
run in parallel given explicit transport requests plus each tool's
``parallel_compatible``, ``avoid_with``, and ``max_concurrent_per_target``
fields on ``EnhancedToolMetadata``.

Single-call batches always run sequentially (parallel of one is moot).

This module is the *single source of truth* for compatibility decisions.
``BatchValidator`` (Task 4.3) delegates here; no other call site re-derives
these rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import Optional, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.types import ToolBatch
from agent.tools.enhanced_metadata import EnhancedToolMetadata


class CompatibilityOutcome(str, Enum):
    """High-level compatibility decision."""

    PARALLEL_OK = "parallel_ok"
    DOWNGRADE_TO_SEQUENTIAL = "downgrade_to_sequential"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class CompatibilityVerdict:
    """Result of a compatibility check across a batch.

    ``effective_strategy`` is the strategy the runtime should adopt;
    ``outcome`` is the higher-level decision (``REJECT`` means the batch
    must not execute under any strategy). ``reason`` is short and
    machine-readable for telemetry.
    """

    outcome: CompatibilityOutcome
    effective_strategy: ExecutionStrategy
    reason: Optional[str] = None


def _metadata_for(tool_id: str) -> Optional[EnhancedToolMetadata]:
    """Resolve ``EnhancedToolMetadata`` for ``tool_id`` or ``None`` if absent."""
    try:
        from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return get_enhanced_tool_metadata(tool_id)
    except Exception:  # pragma: no cover - defensive
        return None


def find_exclusive_conflict(tool_ids: Sequence[str]) -> Optional[str]:
    """Return a machine-readable reason when two tools are exclusive.

    This reads the existing compatibility matrix directly and only treats
    explicit ``EXCLUSIVE`` entries as admission blockers. Missing matrix entries
    do not become implicit approval or rejection here; parallel safety remains
    governed by ``BatchCompatibilityChecker`` and runtime metadata.
    """
    try:
        from agent.tools.compatibility import CompatibilityLevel, ToolCompatibilityAnalyzer
    except Exception:  # pragma: no cover - defensive
        return None

    try:
        matrix = ToolCompatibilityAnalyzer().compatibility_matrix
    except Exception:  # pragma: no cover - defensive
        return None

    for left, right in combinations([str(tid) for tid in tool_ids if tid], 2):
        if (
            matrix.get((left, right)) is CompatibilityLevel.EXCLUSIVE
            or matrix.get((right, left)) is CompatibilityLevel.EXCLUSIVE
        ):
            return f"exclusive_tool_conflict:{left},{right}"
    return None


class BatchCompatibilityChecker:
    """Decides the effective execution strategy for a batch."""

    def check(self, batch: ToolBatch) -> CompatibilityVerdict:
        """Return the compatibility verdict for ``batch``.

        Rules (in order):

        1. Single-call batches → ``SEQUENTIAL`` (parallel is moot).
        2. The builder requested ``SEQUENTIAL`` → honor it as-is.
        3. Missing metadata or ``parallel_compatible=False`` → downgrade.
        4. Any pair with one tool listing the other in ``avoid_with`` → downgrade.
        5. Same tool + same target exceeds ``max_concurrent_per_target`` → downgrade.
        6. Otherwise → ``PARALLEL_OK``.
        """
        if not batch.tool_calls:
            # Empty batches should be rejected upstream by the validator.
            return CompatibilityVerdict(
                outcome=CompatibilityOutcome.REJECT,
                effective_strategy=ExecutionStrategy.SEQUENTIAL,
                reason="empty_batch",
            )

        if len(batch.tool_calls) == 1:
            return CompatibilityVerdict(
                outcome=CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL
                if batch.requested_execution_strategy is ExecutionStrategy.PARALLEL
                else CompatibilityOutcome.PARALLEL_OK,
                effective_strategy=ExecutionStrategy.SEQUENTIAL,
                reason="single_call_batch"
                if batch.requested_execution_strategy is ExecutionStrategy.PARALLEL
                else None,
            )

        if batch.requested_execution_strategy is ExecutionStrategy.SEQUENTIAL:
            return CompatibilityVerdict(
                outcome=CompatibilityOutcome.PARALLEL_OK,
                effective_strategy=ExecutionStrategy.SEQUENTIAL,
            )

        tool_ids = [call.tool_id for call in batch.tool_calls]
        metadata_by_id = {tid: _metadata_for(tid) for tid in tool_ids}

        # Rule 3: metadata must explicitly allow parallel execution. The older
        # ``batch_audited`` rollout marker is intentionally not a runtime gate.
        for tid in tool_ids:
            meta = metadata_by_id.get(tid)
            if meta is None:
                return CompatibilityVerdict(
                    outcome=CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL,
                    effective_strategy=ExecutionStrategy.SEQUENTIAL,
                    reason="missing_tool_metadata",
                )
            if not getattr(meta, "parallel_compatible", False):
                return CompatibilityVerdict(
                    outcome=CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL,
                    effective_strategy=ExecutionStrategy.SEQUENTIAL,
                    reason="parallel_compatible_false",
                )

        # Rule 4: avoid_with conflicts.
        tool_id_set = set(tool_ids)
        for tid in tool_ids:
            meta = metadata_by_id[tid]
            avoid = set(getattr(meta, "avoid_with", []) or [])
            conflicts = (tool_id_set & avoid) - {tid}
            if conflicts:
                return CompatibilityVerdict(
                    outcome=CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL,
                    effective_strategy=ExecutionStrategy.SEQUENTIAL,
                    reason="avoid_with_conflict",
                )

        # Rule 5: enforce per-tool same-target concurrency caps.
        per_target_counts: dict[tuple[str, str], int] = {}
        for call in batch.tool_calls:
            target_key = _target_key(call.parameters)
            if target_key is None:
                continue
            key = (call.tool_id, target_key)
            per_target_counts[key] = per_target_counts.get(key, 0) + 1

        for (tid, _target), count in per_target_counts.items():
            meta = metadata_by_id[tid]
            try:
                limit = int(getattr(meta, "max_concurrent_per_target", 1) or 1)
            except (TypeError, ValueError):
                limit = 1
            if count > max(1, limit):
                return CompatibilityVerdict(
                    outcome=CompatibilityOutcome.DOWNGRADE_TO_SEQUENTIAL,
                    effective_strategy=ExecutionStrategy.SEQUENTIAL,
                    reason="max_concurrent_per_target_exceeded",
                )

        return CompatibilityVerdict(
            outcome=CompatibilityOutcome.PARALLEL_OK,
            effective_strategy=ExecutionStrategy.PARALLEL,
        )


def _target_key(parameters: object) -> Optional[str]:
    if not isinstance(parameters, dict):
        return None
    for key in ("target", "url", "host"):
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


__all__ = [
    "BatchCompatibilityChecker",
    "CompatibilityOutcome",
    "CompatibilityVerdict",
    "find_exclusive_conflict",
]
