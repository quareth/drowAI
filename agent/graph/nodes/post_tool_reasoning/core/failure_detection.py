"""Capability-agnostic tool failure detection.

This module contains pure functions for detecting and classifying tool failures.
No capability-specific logic, no streaming, no state mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Tuple

from core.prompts.builders.post_tool.evidence import read_compact_evidence

if TYPE_CHECKING:
    from ...state import InteractiveState


@dataclass
class FailureContext:
    """Container for failure detection context.
    
    All data needed to detect and classify tool failures, extracted from state.
    """
    
    success_flag: Optional[bool]
    status: Optional[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    summary: str
    key_findings: list
    
    
def detect_failure(context: FailureContext) -> Tuple[bool, Optional[str]]:
    """Detect if tool execution failed and classify failure type.
    
    Pure function with no side effects. Uses multiple indicators to determine
    if a tool execution failed and classifies the failure type.
    
    Args:
        context: Failure detection context with tool output data
        
    Returns:
        Tuple of (failure_detected, failure_category)
        - failure_detected: True if failure was detected
        - failure_category: Classification string or None if no failure
    """
    # Primary failure indicators (aligned with original logic)
    # Note: success_flag comes from last_tool_result.success OR synthesized_output.success
    failure_conditions = [
        context.success_flag is False,
        context.status in ["failed", "error", "validation_error"],
    ]
    
    # Check for output presence
    has_raw_output = bool(context.stdout or context.stderr)
    has_synthesized_content = bool(context.summary or context.key_findings)
    
    # Empty output is a failure indicator
    if not has_raw_output and not has_synthesized_content:
        failure_conditions.append(True)
    
    # If no failure conditions met, tool succeeded
    if not any(failure_conditions):
        return False, None
    
    # Classify the failure type
    category = classify_failure_category(context.stderr, context.exit_code)
    
    return True, category


def classify_failure_category(stderr: str, exit_code: Optional[int]) -> str:
    """Classify failure into category based on stderr and exit code.
    
    Pure function with no side effects. Uses heuristics to determine
    the most likely failure category.
    
    Args:
        stderr: Standard error output from tool
        exit_code: Exit code from tool execution
        
    Returns:
        Failure category string (one of: network_error, permission_denied,
        timeout, tool_unavailable, invalid_params, empty_output, unknown)
    """
    lowered_stderr = stderr.lower()
    
    # Network-related errors
    if "connection refused" in lowered_stderr or "network unreachable" in lowered_stderr:
        return "network_error"
    
    # Permission errors
    if "permission denied" in lowered_stderr or "operation not permitted" in lowered_stderr:
        return "permission_denied"
    
    # Timeout errors
    if exit_code == 124 or "timeout" in lowered_stderr:
        return "timeout"
    
    # Tool not found errors
    if "not found" in lowered_stderr or "command not found" in lowered_stderr:
        return "tool_unavailable"
    
    # Invalid parameter errors
    if "invalid" in lowered_stderr or "error" in lowered_stderr or "failed" in lowered_stderr:
        return "invalid_params"
    
    # Empty output (no stderr but also no stdout)
    if not stderr:
        return "empty_output"
    
    # Fallback category
    return "unknown"


def build_failure_context_from_state(state: InteractiveState) -> FailureContext:
    """Build FailureContext from InteractiveState.
    
    Helper function to extract relevant data from state and package it
    into a FailureContext for failure detection.
    
    Args:
        state: Current InteractiveState
        
    Returns:
        FailureContext with extracted data
    """
    metadata = state.facts.safe_metadata
    synthesized_output = metadata.get("synthesized_output", {}) or {}
    last_tool_result = metadata.get("last_tool_result", {}) or {}
    evidence = read_compact_evidence(metadata)
    compact_result = _primary_compact_from_evidence(evidence, metadata)
    batch_failure_text = _batch_failure_text(evidence)
    
    # Compact-only mode: prefer compact and synthesized signals.
    synth_success = synthesized_output.get("success")
    compact_success = compact_result.get("success")
    tool_success = last_tool_result.get("success")
    
    # If either explicitly says False, use False; otherwise use tool_success
    batch_success = evidence.success if evidence is not None and evidence.source == "batch" else None
    if (
        synth_success is False
        or compact_success is False
        or tool_success is False
        or batch_success is False
    ):
        success_flag = False
    else:
        success_flag = (
            compact_success
            if compact_success is not None
            else tool_success if tool_success is not None else synth_success
        )

    compact_errors = compact_result.get("errors")
    compact_error_text = _compact_errors_to_text(compact_errors).strip()
    structured_error_text = _structured_error_context_to_text(
        compact_result.get("structured_signals")
    ).strip()
    stderr_value = batch_failure_text or compact_error_text or structured_error_text
    
    return FailureContext(
        success_flag=success_flag,
        status=compact_result.get("status")
        or (evidence.status if evidence is not None else None)
        or synthesized_output.get("status")
        or last_tool_result.get("status"),
        exit_code=compact_result.get("exit_code", last_tool_result.get("exit_code")),
        stdout="",
        stderr=stderr_value,
        summary=str(compact_result.get("summary") or synthesized_output.get("summary") or "").strip(),
        key_findings=compact_result.get("key_findings") or synthesized_output.get("key_findings") or [],
    )


def _primary_compact_from_evidence(evidence: Any, metadata: dict) -> dict:
    if evidence is not None and getattr(evidence, "rows", None):
        first = evidence.rows[0]
        if isinstance(first, dict):
            compact = first.get("compact_tool_result")
            if isinstance(compact, dict):
                return compact
    compact = metadata.get("last_tool_result_compact", {}) or {}
    return compact if isinstance(compact, dict) else {}


def _batch_failure_text(evidence: Any) -> str:
    if evidence is None or getattr(evidence, "source", "") != "batch":
        return ""
    rendered: list[str] = []
    for row in getattr(evidence, "failed_rows", ()) or ():
        if not isinstance(row, dict):
            continue
        parts = [
            str(row.get("tool_id") or "unknown_tool"),
            str(row.get("status") or "failed"),
        ]
        failure_category = row.get("failure_category")
        if failure_category:
            parts.append(str(failure_category))
        error_message = row.get("error_message")
        if error_message:
            parts.append(str(error_message))
        rendered.append(": ".join(parts))
    return "\n".join(rendered)


def _compact_errors_to_text(errors: Any) -> str:
    """Convert compact `errors` field to a single stderr-like string."""
    if errors is None:
        return ""
    if isinstance(errors, str):
        return errors
    if isinstance(errors, list):
        rendered: list[str] = []
        for item in errors:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    rendered.append(text)
            elif isinstance(item, dict):
                message = str(item.get("message") or item.get("error") or "").strip()
                code = str(item.get("code") or "").strip()
                if message and code:
                    rendered.append(f"{code}: {message}")
                elif message:
                    rendered.append(message)
                elif code:
                    rendered.append(code)
            else:
                text = str(item).strip()
                if text:
                    rendered.append(text)
        return "\n".join(rendered)
    return str(errors)


def _structured_error_context_to_text(signals: Any) -> str:
    """Render error-context structured signals into a stderr-like string."""
    if not isinstance(signals, list):
        return ""

    rendered: list[str] = []
    for item in signals:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "error_context":
            continue

        message = str(item.get("message") or item.get("error") or "").strip()
        code = str(item.get("code") or "").strip()
        if message and code:
            rendered.append(f"{code}: {message}")
        elif message:
            rendered.append(message)
        elif code:
            rendered.append(code)

    return "\n".join(rendered)


__all__ = [
    "FailureContext",
    "detect_failure",
    "classify_failure_category",
    "build_failure_context_from_state",
]
