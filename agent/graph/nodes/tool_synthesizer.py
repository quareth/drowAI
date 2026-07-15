"""Core tool output synthesis using LLM.

This node contains the PURE LLM processing logic that extracts structured
insights from tool outputs (findings, vulnerabilities, next actions).

It is the single source of truth for tool output processing and is reused by:
- Simple Tool Execution → finalize_results (user-facing)
- Deep Reasoning → observation_adapter (agent-facing)

Adheres to DRY principle and Single Responsibility Principle.
"""

from __future__ import annotations

import logging
import time
from typing import Mapping, Optional

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.post_tool_reasoning.core.retry_logic import get_retry_count
from agent.graph.state import InteractiveState
from agent.graph.utils.goal_tracker import update_achieved_goals
from agent.graph.utils.observation_deduplication import detect_tool_output_change
from backend.services.metrics.utils import safe_inc

logger = logging.getLogger(__name__)


def _tool_output_signature(
    *,
    compact_result: Mapping[str, object],
    status: str,
    summary: str,
    key_findings: list[str],
    errors: list[str],
) -> str:
    """Build a compact signature string for change detection history."""
    compact_summary = str(compact_result.get("summary") or "").strip()
    if compact_summary:
        summary = compact_summary

    signature_parts: list[str] = [f"status={status}", f"summary={summary}"]
    if key_findings:
        signature_parts.append("findings=" + " | ".join(key_findings))
    if errors:
        signature_parts.append("errors=" + " | ".join(errors[:5]))
    return "\n".join(part for part in signature_parts if part)


async def synthesize_tool_output(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """
    Process tool output with LLM to extract structured insights.
    
    This is the CORE synthesis logic that:
    1. Reads compact tool envelope from metadata
    2. Maps compact fields to synthesized output shape
    3. Stores structured data in metadata for downstream nodes
    
    **IMPORTANT**: This node does NOT format output for display.
    It only processes and stores structured data.
    
    Downstream nodes (finalize_results, observation_adapter) handle formatting.
    
    Args:
        state: Current interactive state
        context: Optional runtime context with API key/model
    
    Returns:
        Graph update dict with synthesized data in metadata
    """
    
    interactive = InteractiveState.from_mapping(state)
    
    # Extract tool output from metadata
    metadata = interactive.facts.ensure_metadata()
    last_result = metadata.get("last_tool_result") or {}
    
    success = last_result.get("success", True)  # Default to True for backward compat
    # Check for user rejection FIRST (before success-based status override)
    # User rejection has its own status that should not be overwritten
    raw_status = last_result.get("status", "success")
    
    # Trust the tool's success flag rather than re-interpreting exit codes.
    # Success is resolved centrally via informational exit codes plus hard CLI
    # failure detection in ``execution_outcome.resolve_execution_success``.
    if raw_status == "rejected":
        status = "rejected"  # Preserve rejection status
    elif not success:
        status = "failed"
    else:
        status = raw_status
    
    tool_name = interactive.facts.selected_tool or "unknown_tool"
    
    # Handle user rejection - skip LLM, use canned response
    # This allows the DR loop to continue reasoning without wasting an LLM call
    if status == "rejected":
        logger.info(f"[SYNTHESIS] Tool {tool_name} was rejected by user, using canned response")
        
        user_message = last_result.get("message") or "User declined to execute this tool."
        
        metadata["synthesized_output"] = {
            "tool": tool_name,
            "summary": f"Tool '{tool_name}' was not executed - user declined.",
            "key_findings": [user_message],
            "vulnerabilities": [],
            "next_actions": [
                "Ask user if they'd like to try a different approach",
                "Offer alternative tools or methods",
                "Proceed with remaining plan steps if possible",
            ],
            "status": "user_rejected",
            "importance_score": 5,
            "token_count": 0,
            "success": False,
            "user_rejected": True,
        }
        interactive.facts.metadata = metadata
        
        interactive.trace.reasoning.append(
            f"⏭️ Tool {tool_name} skipped by user - no analysis needed"
        )
        
        return interactive.as_graph_update()
    validation_errors = last_result.get("validation_errors") or metadata.get("validation_errors") or []
    
    compact_result = metadata.get("last_tool_result_compact")
    if not isinstance(compact_result, Mapping):
        raise RuntimeError(
            "Missing last_tool_result_compact in compact-only mode. "
            "Tool execution must persist a compact envelope before synthesis."
        )

    def _list_of_strings(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    compact_status = str(compact_result.get("status") or status)
    compact_success = bool(compact_result.get("success", compact_status not in ("failed", "error")))
    compact_summary = str(compact_result.get("summary") or "").strip()
    if not compact_summary:
        compact_summary = f"Tool {tool_name} completed without compact summary."

    compact_findings = _list_of_strings(compact_result.get("key_findings"))
    compact_errors = _list_of_strings(compact_result.get("errors"))
    compact_recommendations = _list_of_strings(
        compact_result.get("report_recommendations")
    )
    compact_structured_signals = (
        list(compact_result.get("structured_signals"))
        if isinstance(compact_result.get("structured_signals"), list)
        else []
    )
    compact_decision_evidence = _list_of_strings(
        compact_result.get("decision_evidence")
    )
    compact_lossiness_risk = str(
        compact_result.get("lossiness_risk") or "medium"
    ).strip() or "medium"

    # DR.6.5: Detect tool output changes using compact signature only
    previous_outputs = metadata.get("tool_output_history", {})
    compact_signature = _tool_output_signature(
        compact_result=compact_result,
        status=compact_status,
        summary=compact_summary,
        key_findings=compact_findings,
        errors=compact_errors,
    )

    if compact_signature:
        has_change, change_summary = detect_tool_output_change(
            tool_name, compact_signature, previous_outputs
        )
        
        if not has_change:
            logger.debug(
                f"[TOOL] No meaningful change detected in tool output for {tool_name}: {change_summary}"
            )
            # Store in metadata for debugging
            metadata["last_tool_output_change"] = {
                "tool_id": tool_name,
                "has_meaningful_change": False,
                "summary": change_summary,
            }
        else:
            logger.info(
                f"[TOOL] Meaningful changes detected in tool output for {tool_name}: {change_summary}"
            )
            metadata["last_tool_output_change"] = {
                "tool_id": tool_name,
                "has_meaningful_change": True,
                "summary": change_summary,
            }
        
        # Update tool output history (limit to last 5 per tool)
        previous_outputs[tool_name] = compact_signature
        
        # Limit history size
        if len(previous_outputs) > 10:
            # Remove oldest entries (simple FIFO)
            oldest_tool = next(iter(previous_outputs))
            del previous_outputs[oldest_tool]
        
        metadata["tool_output_history"] = previous_outputs

    if status == "validation_error" or validation_errors:
        validation_errors = list(validation_errors)
        readable_errors = []
        for err in validation_errors:
            field = err.get("field") or err.get("loc") or "parameter"
            error_msg = err.get("error") or err.get("msg") or "invalid value"
            suggestion = err.get("suggested_fix")
            if suggestion:
                readable_errors.append(f"{field}: {error_msg} ({suggestion})")
            else:
                readable_errors.append(f"{field}: {error_msg}")

        summary = "Tool input validation failed. Review parameters and retry."
        if readable_errors:
            summary = readable_errors[0]

        metadata["synthesized_output"] = {
            "tool": tool_name,
            "summary": summary,
            "key_findings": [],
            "vulnerabilities": [],
            "next_actions": [
                "Revise tool parameters using the suggested fixes",
                "Retry execution after correcting the invalid arguments",
            ],
            "status": "validation_error",
            "importance_score": 10,
            "token_count": 0,
            "success": False,
            "validation_errors": validation_errors,
            "fallback": True,
        }
        interactive.facts.metadata = metadata

        interactive.trace.reasoning.append(
            "⚠️ Tool execution aborted due to input validation errors."
        )

        return interactive.as_graph_update()

    synthesized_data = {
        "tool": tool_name,
        "summary": compact_summary,
        "key_findings": compact_findings,
        # Keep existing output shape for downstream consumers.
        "vulnerabilities": compact_errors,
        "next_actions": compact_recommendations,
        "structured_signals": compact_structured_signals,
        "decision_evidence": compact_decision_evidence,
        "lossiness_risk": compact_lossiness_risk,
        "status": compact_status,
        "importance_score": 0,
        "token_count": 0,
        "success": compact_success,
    }
    metadata["synthesized_output"] = synthesized_data
    
    # Store per-attempt results for aggregation (before overwrite)
    retry_attempts = metadata.get("retry_attempts", [])
    
    # Get current retry count for attempt tracking
    attempt_number = get_retry_count(metadata)
    
    # Build attempt record with full synthesized output
    attempt_record = {
        "attempt_number": attempt_number,
        "tool_id": tool_name,
        "params": interactive.facts.tool_parameters or {},
        "timestamp": time.time(),
        "synthesized_output": dict(synthesized_data),  # Full copy
    }
    
    retry_attempts.append(attempt_record)
    
    # Limit array to 10 entries (FIFO eviction)
    if len(retry_attempts) > 10:
        retry_attempts.pop(0)
    
    metadata["retry_attempts"] = retry_attempts
    interactive.facts.metadata = metadata
    
    logger.info(f"[SYNTHESIS] Stored attempt {attempt_number} results for aggregation")

    # Increment metrics for attempt storage
    safe_inc("simple_tool_attempt_stored")
    
    # Track synthesis success in reasoning
    interactive.trace.reasoning.append(
        f"✅ Tool synthesis: reused compact envelope for {tool_name} "
        f"({len(compact_findings)} findings, {len(compact_errors)} errors)"
    )
    
    # DR.5.2: Update achieved goals after synthesizing
    update_achieved_goals(interactive)
    
    return interactive.as_graph_update()


__all__ = ["synthesize_tool_output"]
