"""Approval-flow helpers extracted from tool-execution facade.

This module centralizes HITL approval response handling and skip-result
construction while preserving existing metadata and trace behavior.

Phase 7 Task 7.2 added :func:`extract_approved_call_ids` which derives the
surviving subset of a multi-call batch from a normalized approval response.
The orchestrator feeds the resulting ids into
:meth:`agent.tool_runtime.batch.validator.BatchValidator.validate_after_approval`
so the batch validator can downgrade ``parallel`` → ``sequential`` (with
``downgrade_reason="partial_approval"``) or reject the whole batch with
``rejected_reason="denied_aggregate"`` when every call was denied.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agent.tool_runtime.batch.types import (
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets

from ...infrastructure.state_models import GraphRuntimeContext
from ...nodes.hitl_helpers import (
    normalize_tool_approval_response,
    request_tool_approval,
    should_require_approval,
)
from ...state import InteractiveState, ToolExecutionRecord
from ...utils.event_identity import resolve_turn_sequence

_DISPATCH_CACHE_SENSITIVE_KEY_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "token",
        "secret",
        "private_key",
        "credential",
        "session_id",
    }
)


def get_tool_risk_level(
    tool_id: str,
    *,
    high_risk_prefixes: Sequence[str],
    medium_risk_prefixes: Sequence[str],
) -> str:
    """Resolve tool risk level from configured prefix lists."""
    for prefix in high_risk_prefixes:
        if tool_id.startswith(prefix):
            return "high"
    for prefix in medium_risk_prefixes:
        if tool_id.startswith(prefix):
            return "medium"
    return "low"


def build_skipped_tool_result(
    interactive: InteractiveState,
    tool_name: str,
    user_response: Dict[str, Any],
) -> dict:
    """Build rejected tool result payload when user skips execution."""
    user_note = user_response.get("user_note")
    if user_note:
        rejection_message = f"User declined to execute this tool. Reason: {user_note}"
    else:
        rejection_message = "User declined to execute this tool. The user chose to skip this tool execution."

    tool_result = {
        "status": "rejected",
        "success": False,
        "tool_name": tool_name,
        "stdout": rejection_message,
        "stderr": "",
        "exit_code": -1,
        "observation": rejection_message,
        "message": rejection_message,
    }

    interactive.facts.metadata["last_tool_result"] = tool_result
    interactive.facts.metadata["tool_skipped"] = True
    interactive.facts.metadata["skipped_tool"] = tool_name

    interactive.trace.reasoning.append(f"Tool {tool_name} skipped by user")
    interactive.trace.executed_tools.append(
        ToolExecutionRecord(
            tool_id=tool_name,
            args={},
            status="skipped",
            approval_granted=False,
            approval_reason="user_skipped",
            approval_metadata=dict(user_response or {}),
        )
    )

    return interactive.as_graph_update()


def handle_run_tool_execution_approval(
    *,
    interactive: InteractiveState,
    metadata: Dict[str, Any],
    gate_completed: bool,
    tool_name: str,
    tool_params: Dict[str, Any],
    context: Optional[GraphRuntimeContext],
    turn_id: Optional[str],
    approval_gate_completed_key: str,
    approval_gate_response_key: str,
    clear_tool_plan_prepared_flag_fn: Callable[[InteractiveState], None],
    clear_approval_gate_metadata_fn: Callable[[InteractiveState], None],
    get_tool_risk_level_fn: Callable[[str], str],
    normalize_tool_approval_response_fn: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]] = normalize_tool_approval_response,
    request_tool_approval_fn: Callable[..., Dict[str, Any]] = request_tool_approval,
    should_require_approval_fn: Callable[[Mapping[str, Any]], bool] = should_require_approval,
    resolve_turn_sequence_fn: Callable[[Optional[GraphRuntimeContext], Mapping[str, Any]], Optional[int]] = resolve_turn_sequence,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[dict]]:
    """Handle approval response branch in run_tool_execution.

    Returns:
    - approval_response
    - potentially edited tool_params
    - skipped graph update payload (or None)
    """
    facts = interactive.facts

    approval_response: Optional[Dict[str, Any]] = None
    if gate_completed:
        approval_response = normalize_tool_approval_response_fn(
            metadata.get(approval_gate_response_key)
            if isinstance(metadata.get(approval_gate_response_key), dict)
            else None
        )
    elif should_require_approval_fn(metadata):
        approval_response = normalize_tool_approval_response_fn(
            request_tool_approval_fn(
                tool_id=tool_name,
                tool_name=tool_name,
                parameters=tool_params,
                description=f"Execute {tool_name} on target",
                risk_level=get_tool_risk_level_fn(tool_name),
                metadata=metadata,
                turn_sequence=resolve_turn_sequence_fn(context, metadata),
                turn_id=turn_id,
                reserved_message_id=metadata.get("reserved_message_id"),
            )
        )
        metadata[approval_gate_completed_key] = True
        metadata[approval_gate_response_key] = dict(approval_response)
        facts.metadata = metadata

    if approval_response:
        action = approval_response.get("action", "approve")
        if action == "skip":
            clear_tool_plan_prepared_flag_fn(interactive)
            clear_approval_gate_metadata_fn(interactive)
            return (
                approval_response,
                tool_params,
                build_skipped_tool_result(interactive, tool_name, approval_response),
            )

        if action == "edit":
            tool_params = approval_response.get("edited_parameters", tool_params)

    return approval_response, tool_params, None


def apply_cached_dispatch_result(
    interactive: InteractiveState,
    cached: Dict[str, Any],
    tool_name: str,
) -> None:
    """Apply cached tool dispatch result for idempotent return.

    Phase 2.4 (re-audit fix): this function **rehydrates** previously-stored
    metadata from a dispatch cache entry; it is **not** a fresh authoring
    site for ``last_tool_result_compact``. The canonical author site is
    ``batch_runner.write_compact_batch_metadata`` (which populates the
    cache when a batch executes for the first time). This rehydration is
    only reached when the orchestrator detects an idempotent re-entry for
    a tool call that already produced a cached result, so the value being
    restored here was originally written by the canonical author site.
    Treat this assignment as cache-restore, not as a parallel writer.
    """
    facts = interactive.facts
    metadata = facts.metadata_copy()
    metadata["last_tool_result_compact"] = cached.get("last_tool_result_compact", {})
    metadata["last_tool_result"] = cached.get("last_tool_result", {})
    if cached.get("tool_history_entry"):
        metadata.setdefault("tool_history", []).append(cached["tool_history_entry"])
    if cached.get("action_record"):
        action_history = metadata.setdefault("action_history", [])
        action_history.append(cached["action_record"])
        if len(action_history) > 10:
            action_history.pop(0)
    if "tool_execution_history" in cached:
        metadata["tool_execution_history"] = cached["tool_execution_history"]
    if cached.get("current_scan_phase") is not None:
        metadata["current_scan_phase"] = cached["current_scan_phase"]
    if cached.get("tool_catalog"):
        metadata["tool_catalog"] = cached["tool_catalog"]
    if "validation_errors" in cached:
        metadata["validation_errors"] = cached["validation_errors"]
    elif "validation_errors" in metadata:
        del metadata["validation_errors"]
    facts.metadata = metadata
    obs = cached.get("observation_text", "")
    if obs:
        interactive.trace.observations.append(obs)
    for r in cached.get("reasoning_additions", []):
        interactive.trace.reasoning.append(r)
    exec_record = cached.get("exec_record")
    if exec_record:
        interactive.trace.executed_tools.append(
            ToolExecutionRecord(
                tool_id=tool_name,
                args=exec_record.get("args", {}),
                status=exec_record.get("status", "success"),
                observation=exec_record.get("observation"),
                reasoning=exec_record.get("reasoning"),
                approval_granted=exec_record.get("approval_granted", True),
                approval_reason=exec_record.get("approval_reason", "approve"),
                approval_metadata=dict(exec_record.get("approval_metadata") or {}),
            )
        )


def clear_tool_plan_prepared_flag(interactive: InteractiveState) -> None:
    """Clear one-shot preplan marker after tool node return."""
    metadata = interactive.facts.metadata_copy()
    metadata.pop("tool_plan_prepared", None)
    interactive.facts.metadata = metadata


def clear_approval_gate_metadata(
    interactive: InteractiveState,
    *,
    approval_gate_completed_key: str,
    approval_gate_response_key: str,
) -> None:
    """Clear one-shot approval gate markers after dispatch finishes."""
    metadata = interactive.facts.metadata_copy()
    metadata.pop(approval_gate_completed_key, None)
    metadata.pop(approval_gate_response_key, None)
    interactive.facts.metadata = metadata


def maybe_return_cached_dispatch_update(
    *,
    interactive: InteractiveState,
    metadata: Mapping[str, Any],
    tool_call_id: str,
    tool_name: str,
    tool_dispatch_cache_key: str,
    apply_cached_dispatch_result_fn: Callable[[InteractiveState, Dict[str, Any], str], None],
    clear_tool_plan_prepared_flag_fn: Callable[[InteractiveState], None],
    clear_approval_gate_metadata_fn: Callable[[InteractiveState], None],
    log_info_fn: Callable[[str, str], None],
) -> Optional[dict]:
    """Return graph update for idempotent cache hit, or None."""
    dispatch_cache = metadata.get(tool_dispatch_cache_key) or {}
    if isinstance(dispatch_cache, dict) and tool_call_id in dispatch_cache:
        cached = dispatch_cache[tool_call_id]
        if isinstance(cached, dict):
            log_info_fn(
                "[TOOL_EXECUTION] Idempotent dispatch hit for tool_call_id=%s (skipping re-execution)",
                tool_call_id,
            )
            apply_cached_dispatch_result_fn(interactive, cached, tool_name)
            clear_tool_plan_prepared_flag_fn(interactive)
            clear_approval_gate_metadata_fn(interactive)
            return interactive.as_graph_update()
    return None


def store_dispatch_cache_result(
    *,
    facts: Any,
    tool_dispatch_cache_key: str,
    tool_call_id: str,
    compact_result_dict: Dict[str, Any],
    result_for_metadata: Dict[str, Any],
    graph_metadata: Dict[str, Any],
    action_record: Dict[str, Any],
    observation_text: str,
    reasoning_additions: Sequence[str],
    outcome_parameters: Mapping[str, Any],
    outcome_success: bool,
    outcome_summary: str,
    approval_granted: Any,
    approval_reason: Any,
    approval_metadata: Mapping[str, Any],
    deterministic_compact_result_dict: Optional[Dict[str, Any]] = None,
) -> None:
    """Store replayable idempotent dispatch payload in metadata cache."""
    dispatch_cache = dict(facts.metadata.get(tool_dispatch_cache_key) or {})
    cache_entry = {
        "last_tool_result_compact": compact_result_dict,
        "last_tool_result_deterministic_compact": deterministic_compact_result_dict,
        "last_tool_result": result_for_metadata,
        "tool_history_entry": graph_metadata,
        "action_record": action_record,
        "tool_execution_history": facts.metadata.get("tool_execution_history", []),
        "current_scan_phase": facts.metadata.get("current_scan_phase"),
        "tool_catalog": facts.metadata.get("tool_catalog"),
        "validation_errors": facts.metadata.get("validation_errors"),
        "observation_text": observation_text,
        "reasoning_additions": list(reasoning_additions or []),
        "exec_record": {
            "args": dict(outcome_parameters),
            "status": "success" if outcome_success else "error",
            "observation": observation_text,
            "reasoning": outcome_summary,
            "approval_granted": approval_granted,
            "approval_reason": approval_reason,
            "approval_metadata": dict(approval_metadata or {}),
        },
    }
    secret_candidates = _collect_dispatch_cache_secret_candidates(cache_entry)
    masked_cache_entry = _replace_dispatch_cache_secret_candidates(
        mask_durable_secrets(cache_entry, source="tool_dispatch_cache"),
        secret_candidates,
    )
    dispatch_cache[tool_call_id] = (
        masked_cache_entry if isinstance(masked_cache_entry, dict) else {}
    )
    facts.metadata[tool_dispatch_cache_key] = dispatch_cache


def _store_dispatch_cache_entry(
    facts: Any,
    *,
    cache_key: str,
    tool_call_id: str,
    entry: Mapping[str, Any],
) -> None:
    dispatch_cache = dict(facts.metadata.get(cache_key) or {})
    dispatch_cache[tool_call_id] = dict(entry)
    facts.metadata[cache_key] = dispatch_cache


def _read_dispatch_cache_entry(
    metadata: Mapping[str, Any],
    cache_key: str,
    tool_call_id: str,
) -> Optional[Dict[str, Any]]:
    dispatch_cache = metadata.get(cache_key) or {}
    if not isinstance(dispatch_cache, Mapping):
        return None
    cached = dispatch_cache.get(tool_call_id)
    return dict(cached) if isinstance(cached, Mapping) else None


def _call_by_id(batch: ToolBatch, tool_call_id: str) -> Optional[ToolCall]:
    for call in batch.tool_calls:
        if call.tool_call_id == tool_call_id:
            return call
    return None


def _apply_approval_edits_to_batch(
    batch: ToolBatch,
    approval_response: Optional[Mapping[str, Any]],
    *,
    logger: Any,
) -> tuple[ToolBatch, list[ToolCallResult]]:
    if not isinstance(approval_response, Mapping):
        return batch, []

    edited_by_call_id = _approval_edited_parameters(approval_response, batch)
    if not edited_by_call_id:
        return batch, []

    try:
        from agent.tools.parameter_validation import validate_tool_parameters
    except Exception:
        return batch, [
            ToolCallResult(
                tool_call_id=call_id,
                tool_id=(_call_by_id(batch, call_id).tool_id if _call_by_id(batch, call_id) else ""),
                status=ToolCallStatus.FAILED,
                failure_category="invalid_edited_parameters",
                error_message="parameter_validator_unavailable",
            )
            for call_id in edited_by_call_id
        ]

    failed_rows: list[ToolCallResult] = []
    edited_calls: list[ToolCall] = []
    for call in batch.tool_calls:
        edited = edited_by_call_id.get(call.tool_call_id)
        if edited is None:
            edited_calls.append(call)
            continue
        validation = validate_tool_parameters(
            call.tool_id,
            dict(edited),
            validation_stage="execution",
            logger=logger,
        )
        if not validation.valid:
            failed_rows.append(
                ToolCallResult(
                    tool_call_id=call.tool_call_id,
                    tool_id=call.tool_id,
                    status=ToolCallStatus.FAILED,
                    failure_category="invalid_edited_parameters",
                    error_message=str(validation.reason or "invalid_edited_parameters"),
                )
            )
            continue
        edited_calls.append(
            ToolCall(
                tool_call_id=call.tool_call_id,
                tool_id=call.tool_id,
                parameters=dict(validation.normalized_parameters),
                intent=call.intent,
            )
        )

    return (
        ToolBatch(
            tool_batch_id=batch.tool_batch_id,
            tool_calls=tuple(edited_calls),
            requested_execution_strategy=batch.requested_execution_strategy,
            deferred_followups=batch.deferred_followups,
            selection_rationale=batch.selection_rationale,
        ),
        failed_rows,
    )


def _approval_edited_parameters(
    approval_response: Mapping[str, Any],
    batch: ToolBatch,
) -> Dict[str, Dict[str, Any]]:
    edited_by_call_id: Dict[str, Dict[str, Any]] = {}
    if (
        len(batch.tool_calls) == 1
        and approval_response.get("action") == "edit"
        and isinstance(approval_response.get("edited_parameters"), Mapping)
    ):
        edited_by_call_id[batch.tool_calls[0].tool_call_id] = dict(
            approval_response.get("edited_parameters") or {}
        )

    raw_decisions = approval_response.get("decisions")
    decisions: Sequence[Any]
    if isinstance(raw_decisions, Mapping):
        decisions = [
            {"tool_call_id": key, **dict(value)}
            for key, value in raw_decisions.items()
            if isinstance(value, Mapping)
        ]
    elif isinstance(raw_decisions, Sequence) and not isinstance(raw_decisions, (str, bytes)):
        decisions = raw_decisions
    else:
        decisions = ()

    for entry in decisions:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("action") or "").strip().lower() != "edit":
            continue
        call_id = entry.get("tool_call_id")
        edited = entry.get("edited_parameters")
        if isinstance(call_id, str) and isinstance(edited, Mapping):
            edited_by_call_id[call_id] = dict(edited)
    return edited_by_call_id


def _collect_dispatch_cache_secret_candidates(
    value: Any,
    *,
    parent_key: str = "",
) -> set[str]:
    """Collect explicit sensitive-field values for cache-local text masking."""
    if isinstance(value, Mapping):
        candidates: set[str] = set()
        for key, child in value.items():
            candidates.update(
                _collect_dispatch_cache_secret_candidates(child, parent_key=str(key))
            )
        return candidates
    if isinstance(value, (list, tuple)):
        candidates = set()
        for item in value:
            candidates.update(
                _collect_dispatch_cache_secret_candidates(item, parent_key=parent_key)
            )
        return candidates
    if (
        isinstance(value, str)
        and len(value) >= 4
        and _is_dispatch_cache_sensitive_key(parent_key)
    ):
        return {value}
    return set()


def _replace_dispatch_cache_secret_candidates(value: Any, candidates: set[str]) -> Any:
    """Replace known sensitive values in replayable cache text fields."""
    if not candidates:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _replace_dispatch_cache_secret_candidates(child, candidates)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_dispatch_cache_secret_candidates(item, candidates) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_dispatch_cache_secret_candidates(item, candidates) for item in value)
    if isinstance(value, str):
        masked = value
        for candidate in sorted(candidates, key=len, reverse=True):
            masked = masked.replace(candidate, "<DURABLE_SECRET_MASK:secret>")
        return masked
    return value


def _is_dispatch_cache_sensitive_key(key: str) -> bool:
    normalized = str(key or "").strip().lower()
    return any(part in normalized for part in _DISPATCH_CACHE_SENSITIVE_KEY_PARTS)


def extract_approved_call_ids(
    approval_response: Optional[Mapping[str, Any]],
    *,
    all_call_ids: Sequence[str],
) -> List[str]:
    """Return the subset of ``all_call_ids`` the user approved.

    Phase 7 Task 7.2 contract:

    - When ``approval_response`` is missing or its top-level ``action`` is
      ``approve`` / ``edit`` and there is no per-item override, every
      original call id survives (legacy single-tool behaviour).
    - When the top-level action is ``skip``, no call id survives — the
      validator will reject with ``rejected_reason="denied_aggregate"``.
    - When the response carries a ``decisions`` mapping or list keyed by
      ``tool_call_id``, only the entries whose decision is ``approve`` /
      ``edit`` survive. Items with action ``skip`` (or ``deny``) drop
      out, mirroring the multi-call approval surface.
    """
    ids = [str(call_id) for call_id in all_call_ids if call_id]
    if not ids:
        return []

    if not isinstance(approval_response, Mapping):
        return list(ids)

    top_action = approval_response.get("action")
    if isinstance(top_action, str) and top_action.strip().lower() == "skip":
        return []

    decisions: Dict[str, str] = {}
    raw_decisions = approval_response.get("decisions")
    if isinstance(raw_decisions, Mapping):
        for key, value in raw_decisions.items():
            if not isinstance(value, Mapping):
                action_text = str(value).strip().lower() if value is not None else ""
            else:
                action_text = str(value.get("action") or "").strip().lower()
            if action_text:
                decisions[str(key)] = action_text
    elif isinstance(raw_decisions, list):
        for entry in raw_decisions:
            if not isinstance(entry, Mapping):
                continue
            call_id = entry.get("tool_call_id")
            action_text = str(entry.get("action") or "").strip().lower()
            if isinstance(call_id, str) and call_id and action_text:
                decisions[call_id] = action_text

    if not decisions:
        return list(ids)

    survivors: List[str] = []
    approved_actions = {"approve", "edit", ""}
    for call_id in ids:
        action_text = decisions.get(call_id, "")
        if action_text in approved_actions:
            survivors.append(call_id)
    return survivors
