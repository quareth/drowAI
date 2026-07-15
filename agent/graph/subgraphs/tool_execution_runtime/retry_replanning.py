"""Invalidate stale tool plans during checkpoint retry replanning.

This module owns retry-context metadata projection, planner hint merging, and
stale tool-plan cleanup for the tool-execution runtime. It does not execute
tools or coordinate batch dispatch.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ...utils.retry_context import RetryContext, read_retry_context


_CHECKPOINT_RETRY_CONTEXT_KEY = "checkpoint_retry_context"
_CHECKPOINT_RETRY_REPLAN_APPLIED_KEY = "checkpoint_retry_tool_replan_applied"
_CHECKPOINT_RETRY_STALE_PLAN_KEYS = (
    "planner_plan",
    "planner_context_snapshot",
    "plan_context",
    "tool_plan_prepared",
)


def _retry_replan_marker(retry_context: RetryContext) -> str:
    """Return a stable marker so retry replanning is applied once per attempt."""
    failure = retry_context.previous_failure or {}
    marker_parts = [
        f"attempt={retry_context.retry_attempt if retry_context.retry_attempt is not None else ''}",
        f"max={retry_context.retry_max_attempts if retry_context.retry_max_attempts is not None else ''}",
    ]
    for key in ("error_code", "failure_stage", "tool_name", "tool_call_id"):
        value = failure.get(key)
        if isinstance(value, str) and value:
            marker_parts.append(f"{key}={value}")
    return "|".join(marker_parts)


def _serialize_retry_context(retry_context: RetryContext) -> Dict[str, Any]:
    """Project sanitized retry context into metadata for runtime consumers."""
    payload: Dict[str, Any] = {}
    if retry_context.retry_attempt is not None:
        payload["retry_attempt"] = retry_context.retry_attempt
    if retry_context.retry_max_attempts is not None:
        payload["retry_max_attempts"] = retry_context.retry_max_attempts
    if retry_context.previous_failure:
        payload["previous_failure"] = dict(retry_context.previous_failure)
    return payload


def _build_retry_tool_hint(retry_context: RetryContext) -> str:
    """Build a compact planner hint from sanitized previous-failure context."""
    failure = retry_context.previous_failure or {}
    descriptor_parts = []
    for key in ("error_code", "failure_stage"):
        value = failure.get(key)
        if isinstance(value, str) and value:
            descriptor_parts.append(value)
    tool_name = failure.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        descriptor_parts.append(f"tool={tool_name}")
    descriptor = "/".join(descriptor_parts) if descriptor_parts else "previous attempt"

    summary = failure.get("summary")
    summary_text = str(summary).strip() if isinstance(summary, str) else ""
    if summary_text:
        return (
            f"Checkpoint retry: previous attempt failed ({descriptor}): {summary_text}. "
            "Choose corrected or alternate tool parameters/path; do not repeat the "
            "same failing call unchanged."
        )
    return (
        f"Checkpoint retry: previous attempt failed ({descriptor}). Choose corrected "
        "or alternate tool parameters/path; do not repeat the same failing call unchanged."
    )


def _merge_retry_tool_hint(existing_hint: Any, retry_hint: str) -> str:
    """Append retry guidance without duplicating it on the same checkpoint retry run."""
    existing = str(existing_hint or "").strip()
    if not existing:
        return retry_hint
    if retry_hint in existing or "Checkpoint retry:" in existing:
        return existing
    return f"{existing}\n\n{retry_hint}"


def _apply_checkpoint_retry_tool_replanning_context(
    interactive: Any,
    *,
    config: Optional[Mapping[str, Any]],
    metadata: Dict[str, Any],
    deps: Mapping[str, Any],
) -> Dict[str, Any]:
    """Invalidate stale tool plans and surface retry guidance to the planner."""
    retry_context = read_retry_context(config)
    if not retry_context.is_retry:
        return metadata

    marker = _retry_replan_marker(retry_context)
    if metadata.get(_CHECKPOINT_RETRY_REPLAN_APPLIED_KEY) == marker:
        interactive.facts.metadata = metadata
        return metadata

    retry_payload = _serialize_retry_context(retry_context)
    if retry_payload:
        metadata[_CHECKPOINT_RETRY_CONTEXT_KEY] = retry_payload
    metadata[_CHECKPOINT_RETRY_REPLAN_APPLIED_KEY] = marker

    retry_hint = _build_retry_tool_hint(retry_context)
    combined_hint = _merge_retry_tool_hint(
        interactive.facts.next_tool_hint or metadata.get("next_tool_hint"),
        retry_hint,
    )
    metadata["next_tool_hint"] = combined_hint
    interactive.facts.next_tool_hint = combined_hint

    for key in _CHECKPOINT_RETRY_STALE_PLAN_KEYS:
        metadata.pop(key, None)

    dispatch_cache_key = deps.get("_TOOL_DISPATCH_CACHE_KEY")
    if isinstance(dispatch_cache_key, str):
        metadata.pop(dispatch_cache_key, None)
    tool_call_id_key = deps.get("_TOOL_CALL_ID_KEY")
    if isinstance(tool_call_id_key, str):
        metadata.pop(tool_call_id_key, None)
    # Keep retry attempts from reusing stale direct-call identity markers when
    # callers or tests injected them outside the canonical deps key wiring.
    metadata.pop("tool_call_id", None)
    metadata.pop("tool_batch_id", None)
    approval_completed_key = deps.get("_APPROVAL_GATE_COMPLETED_KEY")
    if isinstance(approval_completed_key, str):
        metadata.pop(approval_completed_key, None)
    approval_response_key = deps.get("_APPROVAL_GATE_RESPONSE_KEY")
    if isinstance(approval_response_key, str):
        metadata.pop(approval_response_key, None)

    failure = retry_context.previous_failure or {}
    failed_tool = str(failure.get("tool_name") or "").strip() or None
    if failed_tool:
        if hasattr(interactive.facts, "tool_parameters") and isinstance(
            interactive.facts.tool_parameters,
            dict,
        ):
            interactive.facts.tool_parameters.pop(failed_tool, None)
        if getattr(interactive.facts, "selected_tool", None) == failed_tool:
            interactive.facts.selected_tool = None
        metadata_tool_parameters = metadata.get("tool_parameters")
        if isinstance(metadata_tool_parameters, dict):
            metadata_tool_parameters.pop(failed_tool, None)
            if not metadata_tool_parameters:
                metadata.pop("tool_parameters", None)
        if metadata.get("selected_tool") == failed_tool:
            metadata.pop("selected_tool", None)

    interactive.facts.metadata = metadata
    safe_inc_fn = deps.get("safe_inc")
    if callable(safe_inc_fn):
        safe_inc_fn("checkpoint_retry_tool_plan_invalidated")
    logger = deps.get("logger")
    if logger is not None:
        try:
            logger.info(
                "[CHECKPOINT_RETRY] Invalidated stale tool plan for retry replanning "
                "(attempt=%s, failed_tool=%s)",
                retry_context.retry_attempt,
                failed_tool,
            )
        except Exception:
            pass
    return metadata
