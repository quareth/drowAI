"""Structural and budget validation for tool batches (Phase 4 Task 4.3).

Runs before any per-call execution. Owns the full decision matrix: enforces
the validator commit cap (``AgentConfig.max_committed_tools_per_batch``),
the per-turn tool-call budget pre-check (``FactsState.budgets.max_tool_calls
- FactsState.tool_calls_used``), and delegates compatibility decisions to
:class:`BatchCompatibilityChecker`. Records both ``requested`` and
``effective`` execution strategies plus a ``downgrade_reason`` (when the
two differ) for telemetry.

The validator does **not** decrement the budget — that happens per-call in
the orchestrator loop (Phase 5 Task 5.4). Pre-check semantics: reject the
batch upfront if it would not fit; the runtime never partially executes a
batch it cannot afford to finish.

The cap value comes from the caller via ``ctx`` so no numeric literal lives
in this file.

Phase 7 Task 7.2 adds :meth:`BatchValidator.validate_after_approval`. When
the user denies a subset of items in the approval surface, the orchestrator
re-feeds the survivors to this method which (a) rejects the batch with
``denied_aggregate`` if every item was denied, or (b) returns a survivor-only
batch downgraded ``parallel`` → ``sequential`` with
``downgrade_reason="partial_approval"`` so the executor never races the
remaining calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.compatibility import (
    BatchCompatibilityChecker,
    CompatibilityOutcome,
    find_exclusive_conflict,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tool_runtime.batch.validation_helpers import looks_like_placeholder


class BatchValidationError(Exception):
    """Raised when a tool batch fails structural validation.

    Carries a machine-readable ``rejected_reason`` so telemetry and the
    builder repair path can branch on the failure mode.
    """

    def __init__(self, rejected_reason: str, message: str = "") -> None:
        super().__init__(message or rejected_reason)
        self.rejected_reason = rejected_reason


@dataclass(frozen=True, slots=True)
class BatchValidationResult:
    """Outcome of validating a batch (without executing it).

    ``admitted`` is False when the batch must not run; ``rejected_reason``
    carries the machine-readable failure mode in that case. When admitted,
    ``effective_execution_strategy`` is the strategy the runtime should
    adopt (may differ from ``requested_execution_strategy`` after a
    compatibility downgrade).
    """

    admitted: bool
    batch: ToolBatch
    requested_execution_strategy: ExecutionStrategy
    effective_execution_strategy: ExecutionStrategy
    strategy_downgraded: bool
    downgrade_reason: Optional[str] = None
    rejected_reason: Optional[str] = None


class BatchValidator:
    """Validates a batch against config caps + per-turn budgets + compatibility.

    ``ctx`` is a mapping carrying:

    - ``max_committed_tools_per_batch``: from ``AgentConfig`` (caller-supplied).
    - ``max_tool_calls`` / ``tool_calls_used``: from ``FactsState`` (already
      on the graph state). When either is ``None``, the budget pre-check is
      skipped (legacy callers without budgets behave as before).
    """

    def __init__(self, compatibility: Optional[BatchCompatibilityChecker] = None) -> None:
        self._compatibility = compatibility or BatchCompatibilityChecker()

    def validate(self, batch: ToolBatch, ctx: Mapping[str, Any]) -> BatchValidationResult:
        """Return a :class:`BatchValidationResult` for ``batch``."""
        requested = batch.requested_execution_strategy

        if not batch.tool_calls:
            return self._reject(batch, requested, "empty_batch")

        max_committed = ctx.get("max_committed_tools_per_batch")
        if isinstance(max_committed, int) and max_committed >= 1:
            if len(batch.tool_calls) > max_committed:
                return self._reject(batch, requested, "tool_calls_above_max")

        budget_remaining = self._compute_budget_remaining(ctx)
        if budget_remaining is not None and len(batch.tool_calls) > budget_remaining:
            return self._reject(batch, requested, "tool_call_budget_exceeded")

        basic_rejection = self._validate_admission_authority(batch, ctx)
        if basic_rejection:
            return self._reject(batch, requested, basic_rejection)

        normalized_batch, parameter_rejection = self._normalize_parameters(batch, ctx)
        if parameter_rejection:
            return self._reject(batch, requested, parameter_rejection)

        conflict_reason = self._validate_exclusive_and_high_risk(normalized_batch, ctx)
        if conflict_reason:
            return self._reject(normalized_batch, requested, conflict_reason)

        # Compatibility verdict (and resulting effective strategy).
        verdict = self._compatibility.check(normalized_batch)
        if verdict.outcome is CompatibilityOutcome.REJECT:
            return self._reject(
                normalized_batch,
                requested,
                verdict.reason or "compatibility_rejected",
            )

        downgraded = verdict.effective_strategy != requested
        return BatchValidationResult(
            admitted=True,
            batch=normalized_batch,
            requested_execution_strategy=requested,
            effective_execution_strategy=verdict.effective_strategy,
            strategy_downgraded=downgraded,
            downgrade_reason=verdict.reason if downgraded else None,
        )

    def validate_after_approval(
        self,
        batch: ToolBatch,
        *,
        approved_call_ids: Iterable[str],
    ) -> BatchValidationResult:
        """Re-validate a batch after the approval response is in.

        Phase 7 Task 7.2 contract:

        - If every original call was denied, return a rejection with
          ``rejected_reason="denied_aggregate"`` so the orchestrator can
          short-circuit (no executor invocation, PTR runs once on the
          denial aggregate).
        - If a subset survives, return an admitted result whose ``batch``
          contains only the surviving :class:`ToolCall` rows in their
          original manifest order. The effective strategy is forced to
          ``SEQUENTIAL`` with ``downgrade_reason="partial_approval"`` —
          we never silently keep ``PARALLEL`` once the user has pruned
          the manifest.
        - If every original call survives the strategy is preserved
          (still SEQUENTIAL when the original requested SEQUENTIAL;
          PARALLEL stays PARALLEL — no spurious downgrade).
        """
        requested = batch.requested_execution_strategy
        approved_set = {str(call_id) for call_id in approved_call_ids if call_id}

        if not batch.tool_calls:
            return self._reject(batch, requested, "empty_batch")

        survivors: Sequence[ToolCall] = tuple(
            call for call in batch.tool_calls if call.tool_call_id in approved_set
        )
        if not survivors:
            return self._reject(batch, requested, "denied_aggregate")

        survivor_batch = ToolBatch(
            tool_batch_id=batch.tool_batch_id,
            tool_calls=survivors,
            requested_execution_strategy=requested,
            deferred_followups=batch.deferred_followups,
            selection_rationale=batch.selection_rationale,
        )

        partial = len(survivors) < len(batch.tool_calls)
        if partial and requested is ExecutionStrategy.PARALLEL:
            return BatchValidationResult(
                admitted=True,
                batch=survivor_batch,
                requested_execution_strategy=requested,
                effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
                strategy_downgraded=True,
                downgrade_reason="partial_approval",
            )

        return BatchValidationResult(
            admitted=True,
            batch=survivor_batch,
            requested_execution_strategy=requested,
            effective_execution_strategy=requested,
            strategy_downgraded=False,
            downgrade_reason=None,
        )

    @staticmethod
    def _reject(
        batch: ToolBatch,
        requested: ExecutionStrategy,
        reason: str,
    ) -> BatchValidationResult:
        return BatchValidationResult(
            admitted=False,
            batch=batch,
            requested_execution_strategy=requested,
            effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
            strategy_downgraded=False,
            downgrade_reason=None,
            rejected_reason=reason,
        )

    @staticmethod
    def _compute_budget_remaining(ctx: Mapping[str, Any]) -> Optional[int]:
        """Return ``max_tool_calls - tool_calls_used`` or ``None`` to skip the gate."""
        max_calls = ctx.get("max_tool_calls")
        used = ctx.get("tool_calls_used")
        if not isinstance(max_calls, int) or not isinstance(used, int):
            return None
        if max_calls < 0 or used < 0:
            return None
        return max(0, max_calls - used)

    def _validate_admission_authority(
        self,
        batch: ToolBatch,
        ctx: Mapping[str, Any],
    ) -> Optional[str]:
        """Run fail-closed structural and policy checks before compatibility."""
        call_ids = [call.tool_call_id for call in batch.tool_calls]
        if len(set(call_ids)) != len(call_ids):
            return "duplicate_tool_call_id"

        tool_ids = [call.tool_id for call in batch.tool_calls]
        candidate_ids = _coerce_string_set(ctx.get("candidate_tool_ids"))
        if candidate_ids:
            for tool_id in tool_ids:
                if tool_id not in candidate_ids:
                    return "tool_not_in_candidate_set"

        available_ids = _coerce_string_set(ctx.get("available_tool_ids"))
        for tool_id in tool_ids:
            if available_ids:
                if tool_id not in available_ids:
                    return "tool_not_available"
            elif not _tool_exists(tool_id):
                return "tool_not_available"

        for call in batch.tool_calls:
            if looks_like_placeholder(call.parameters):
                return "placeholder_parameters"

        return None

    def _normalize_parameters(
        self,
        batch: ToolBatch,
        ctx: Mapping[str, Any],
    ) -> tuple[ToolBatch, Optional[str]]:
        """Validate and normalize every committed call through the shared validator."""
        validator = ctx.get("validate_tool_parameters_fn")
        if not callable(validator):
            validator = _default_validate_tool_parameters

        normalized_calls: list[ToolCall] = []
        action_target = ctx.get("action_target")
        logger = ctx.get("logger")
        max_shell_command_chars = ctx.get("max_shell_command_chars")
        for call in batch.tool_calls:
            kwargs: dict[str, Any] = {
                "validation_stage": "execution",
                "action_target": str(action_target) if action_target else None,
                "logger": logger,
            }
            if isinstance(max_shell_command_chars, int) and max_shell_command_chars > 0:
                kwargs["max_shell_command_chars"] = max_shell_command_chars
            result = validator(call.tool_id, dict(call.parameters), **kwargs)
            if not getattr(result, "valid", False):
                return batch, f"invalid_parameters:{call.tool_id}"
            normalized_calls.append(
                ToolCall(
                    tool_call_id=call.tool_call_id,
                    tool_id=call.tool_id,
                    parameters=dict(getattr(result, "normalized_parameters", {}) or {}),
                    intent=call.intent,
                )
            )

        return (
            ToolBatch(
                tool_batch_id=batch.tool_batch_id,
                tool_calls=tuple(normalized_calls),
                requested_execution_strategy=batch.requested_execution_strategy,
                deferred_followups=batch.deferred_followups,
                selection_rationale=batch.selection_rationale,
            ),
            None,
        )

    @staticmethod
    def _validate_exclusive_and_high_risk(
        batch: ToolBatch,
        ctx: Mapping[str, Any],
    ) -> Optional[str]:
        """Reject high-risk or explicitly exclusive multi-call combinations."""
        if len(batch.tool_calls) <= 1:
            return None

        high_risk_ids = _coerce_string_set(ctx.get("high_risk_tool_ids"))
        high_risk_prefixes = tuple(
            str(prefix)
            for prefix in (ctx.get("high_risk_tool_prefixes") or ())
            if isinstance(prefix, str) and prefix
        )
        for call in batch.tool_calls:
            if call.tool_id in high_risk_ids or (
                high_risk_prefixes and call.tool_id.startswith(high_risk_prefixes)
            ):
                return "high_risk_tool_in_batch"

        return find_exclusive_conflict([call.tool_id for call in batch.tool_calls])


def _coerce_string_set(value: Any) -> set[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return set()
    return {str(item) for item in value if str(item or "").strip()}


def _tool_exists(tool_id: str) -> bool:
    try:
        from agent.tools.tool_registry import tool_exists
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        return bool(tool_exists(tool_id))
    except Exception:  # pragma: no cover - defensive
        return False


def _default_validate_tool_parameters(
    tool_id: str,
    parameters: dict[str, Any],
    **kwargs: Any,
) -> Any:
    from agent.tools.parameter_validation import validate_tool_parameters

    return validate_tool_parameters(tool_id, parameters, **kwargs)


__all__ = [
    "BatchValidator",
    "BatchValidationError",
    "BatchValidationResult",
]
