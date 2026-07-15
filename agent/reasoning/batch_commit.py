"""Committed tool-call envelope → ToolBatch parser.

Single home for translating the builder's committed tool calls into the
typed :class:`agent.tool_runtime.batch.types.ToolBatch` shared by the rest
of the runtime.

Validation here is strictly **structural**: count bound, candidate
membership, parameter shape, and execution-strategy parsing. Semantic
validation (compatibility, parameter validity, scope, runtime policy) lives
in :mod:`agent.tool_runtime.batch.validator` (Phase 4). The commit cap is
supplied by the caller via ``max_calls`` (sourced from
``AgentConfig.max_committed_tools_per_batch``); no numeric cap literal
lives here.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.ids import mint_tool_batch_id, mint_tool_call_id
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tool_runtime.batch.validation_helpers import looks_like_placeholder


class BatchCommitError(ValueError):
    """Raised when the builder envelope cannot be parsed into a ToolBatch.

    Carries a machine-readable ``reason`` so telemetry and the structured-
    contract repair path can branch on the failure mode without scraping
    error messages.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _parse_execution_strategy(raw: Any) -> ExecutionStrategy:
    """Map selector-owned ``execution_strategy`` input to the leaf enum."""
    if isinstance(raw, ExecutionStrategy):
        return raw
    text = str(raw or "").strip().lower()
    if text == "parallel":
        return ExecutionStrategy.PARALLEL
    if text == "sequential":
        return ExecutionStrategy.SEQUENTIAL
    raise BatchCommitError(
        "invalid_execution_strategy",
        f"execution_strategy must be 'sequential' or 'parallel'; got {raw!r}",
    )


def _normalize_followups(raw: Any) -> tuple[str, ...]:
    """Coerce ``deferred_followups`` into an immutable tuple of strings."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise BatchCommitError(
            "invalid_deferred_followups",
            "deferred_followups must be a list of strings",
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise BatchCommitError(
                "invalid_deferred_followups",
                "deferred_followups entries must be strings",
            )
        out.append(item)
    return tuple(out)


def commit_tool_batch(
    envelope: Mapping[str, Any],
    *,
    candidate_tool_ids: Sequence[str],
    max_calls: int,
    requested_execution_strategy: Any,
) -> ToolBatch:
    """Parse and id-mint a :class:`ToolBatch` from the builder envelope.

    Each committed ``tool_id`` must come from ``candidate_tool_ids``. The
    cap (``max_calls``) is sourced by the caller from
    ``AgentConfig.max_committed_tools_per_batch``. The requested execution
    strategy is selector-owned and passed separately from the builder
    envelope. Raises :class:`BatchCommitError` on any structural problem.
    """
    if not isinstance(envelope, Mapping):
        raise BatchCommitError("envelope_not_mapping", "envelope must be a mapping")
    if not isinstance(max_calls, int) or max_calls < 1:
        raise BatchCommitError(
            "invalid_max_calls",
            "max_calls must be a positive integer (sourced from AgentConfig)",
        )

    raw_calls = envelope.get("tool_calls")
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        raise BatchCommitError(
            "tool_calls_not_list",
            "envelope.tool_calls must be a list",
        )
    if len(raw_calls) < 1:
        raise BatchCommitError(
            "empty_tool_calls",
            "envelope.tool_calls must contain at least one call",
        )
    if len(raw_calls) > max_calls:
        raise BatchCommitError(
            "tool_calls_above_max",
            f"envelope committed {len(raw_calls)} calls > max_calls={max_calls}",
        )

    candidate_set = {str(tid) for tid in candidate_tool_ids if tid}

    parsed_calls: list[ToolCall] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, Mapping):
            raise BatchCommitError(
                "tool_call_not_mapping",
                f"tool_calls[{index}] must be a mapping",
            )
        tool_id = raw_call.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id.strip():
            raise BatchCommitError(
                "missing_tool_id",
                f"tool_calls[{index}].tool_id must be a non-empty string",
            )
        tool_id = tool_id.strip()
        if candidate_set and tool_id not in candidate_set:
            raise BatchCommitError(
                "unknown_tool_id",
                f"tool_calls[{index}].tool_id={tool_id!r} not in candidate set",
            )
        # Native tool calls provide arguments as JSON strings, while local
        # tests and compatibility callers may provide decoded mappings.
        # Accept both shapes at this structural boundary.
        params_raw = raw_call.get("parameters")
        if isinstance(params_raw, str):
            try:
                params = json.loads(params_raw) if params_raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise BatchCommitError(
                    "parameters_not_json",
                    f"tool_calls[{index}].parameters is a string but not valid JSON: {exc}",
                ) from exc
            if not isinstance(params, Mapping):
                raise BatchCommitError(
                    "parameters_not_mapping",
                    f"tool_calls[{index}].parameters JSON must decode to an object",
                )
        elif isinstance(params_raw, Mapping):
            params = params_raw
        else:
            raise BatchCommitError(
                "parameters_not_mapping",
                f"tool_calls[{index}].parameters must be an object or JSON-encoded string",
            )
        if looks_like_placeholder(params):
            raise BatchCommitError(
                "placeholder_parameters",
                f"tool_calls[{index}].parameters contain a placeholder value; "
                "commit only calls with concrete parameters",
            )
        intent_raw = raw_call.get("intent", "")
        intent = intent_raw if isinstance(intent_raw, str) else ""

        parsed_calls.append(
            ToolCall(
                tool_call_id=mint_tool_call_id(),
                tool_id=tool_id,
                parameters=dict(params),
                intent=intent,
            )
        )

    strategy = _parse_execution_strategy(requested_execution_strategy)
    deferred_followups = _normalize_followups(envelope.get("deferred_followups"))
    rationale_raw = envelope.get("selection_rationale", "")
    selection_rationale = rationale_raw if isinstance(rationale_raw, str) else ""

    return ToolBatch(
        tool_batch_id=mint_tool_batch_id(),
        tool_calls=tuple(parsed_calls),
        requested_execution_strategy=strategy,
        deferred_followups=deferred_followups,
        selection_rationale=selection_rationale,
    )


__all__ = ["BatchCommitError", "commit_tool_batch"]
