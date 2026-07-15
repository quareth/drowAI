"""Finalizer node responsible for producing the assistant response."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState

logger = logging.getLogger(__name__)


def finalize_turn(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """Ensure the interactive state has final text populated.
    
    DR.4.5: Includes tool gap reporting in final text when tools were unavailable.
    """

    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.safe_metadata
    
    # DR.4.5: Add tool gap reporting to final text
    tool_gaps = metadata.get("tool_gaps", [])
    limitations = metadata.get("limitations", [])
    capability_fallbacks = metadata.get("capability_fallbacks", [])
    
    # Build final text with tool gap information
    if interactive.trace.final_text is None:
        summaries = metadata.get("tool_summaries") or []
        if summaries:
            final_text = summaries[-1].get("summary") or interactive.facts.message
        elif (interactive.facts.capability or "").lower() == "deep_reasoning":
            # Deep reasoning: derive a user-facing conclusion from observations/plan.
            final_text = _build_deep_reasoning_final_text(interactive)
        else:
            final_text = interactive.facts.message
    else:
        final_text = interactive.trace.final_text
    
    # Append tool gap and limitation information
    gap_notes = []
    if tool_gaps:
        gap_notes.append("\n\n**Tool Availability Notes:**")
        for gap in tool_gaps:
            gap_notes.append(f"- {gap}")
    
    if capability_fallbacks:
        gap_notes.append("\n**Capability Fallbacks:**")
        for fallback in capability_fallbacks:
            gap_notes.append(f"- {fallback}")
    
    if limitations:
        gap_notes.append("\n**Limitations:**")
        for limitation in limitations:
            gap_notes.append(f"- {limitation}")
    
    if gap_notes:
        final_text += "\n".join(gap_notes)
        # Also add suggestions for manual steps
        if tool_gaps:
            final_text += "\n\n**Suggestions:** Consider installing additional tools or performing manual steps to complete the requested tasks."
    
    # Add retry summary if multiple attempts were made
    retry_attempts = metadata.get("retry_attempts", [])
    if len(retry_attempts) > 1:
        logger.info(f"[FINALIZE] Adding retry summary for {len(retry_attempts)} attempts")
        retry_narrative = _format_retry_narrative(retry_attempts)
        if retry_narrative:
            final_text += f"\n\n**Retry Summary:**\n{retry_narrative}"
    
    interactive.trace.final_text = final_text
    
    interactive.trace.history.append(
        {"type": "final_text", "content": interactive.trace.final_text or ""}
    )
    if context:
        interactive.trace.history.append(
            {
                "type": "context_summary",
                "content": f"Finalized with runtime context for task {context.task_id}.",
            }
        )

    # Fire-and-forget memory extraction (non-blocking, failures are silent)
    try:
        from backend.services.memory.extraction_trigger import enqueue_memory_extraction

        enqueue_memory_extraction(
            user_message=interactive.facts.message or "",
            assistant_response=interactive.trace.final_text or "",
            user_id=metadata.get("user_id"),
            task_id=interactive.facts.task_id,
            conversation_id=interactive.facts.conversation_id,
            turn_id=None,
            llm_runtime_selection=_runtime_selection_snapshot(metadata, context),
        )
    except Exception:
        logger.debug("[FINALIZE] Memory extraction enqueue skipped", exc_info=True)

    return interactive.as_graph_update()


def _runtime_selection_snapshot(
    metadata: Mapping[str, Any],
    context: Optional[GraphRuntimeContext],
) -> dict[str, Any] | None:
    """Extract non-secret provider/model/credential-ref data for memory workers."""

    existing = metadata.get("llm_runtime_selection")
    if isinstance(existing, Mapping):
        credential_ref = existing.get("credential_ref")
        if isinstance(credential_ref, Mapping):
            return {
                "provider": str(existing.get("provider") or ""),
                "model": str(existing.get("model") or ""),
                "credential_ref": dict(credential_ref),
                "reasoning_effort": existing.get("reasoning_effort"),
            }

    provider = metadata.get("provider") or getattr(context, "provider", None)
    model = metadata.get("model") or getattr(context, "model", None)
    credential_ref = metadata.get("credential_ref") or getattr(context, "credential_ref", None)
    reasoning_effort = metadata.get("reasoning_effort") or getattr(context, "reasoning_effort", None)
    if not provider or not model or not isinstance(credential_ref, Mapping):
        return None
    return {
        "provider": str(provider),
        "model": str(model),
        "credential_ref": dict(credential_ref),
        "reasoning_effort": reasoning_effort,
    }


def _build_deep_reasoning_final_text(interactive: InteractiveState) -> str:
    """Best-effort final text for deep reasoning runs.

    Content quality is handled elsewhere; this ensures the final assistant
    message reflects DR findings rather than simply echoing the user query.
    """
    facts = interactive.facts
    trace = interactive.trace

    # Prefer the latest synthesized observation summary if available.
    observations = list(trace.observations or [])
    if observations:
        # Use the most recent observation as the primary conclusion.
        return observations[-1]

    # Fallback: if we have a plan, surface it as a simple conclusion.
    plan = list(facts.plan or [])
    if plan:
        joined = "\n".join(f"- {step}" for step in plan)
        return f"Planned and partially executed the following steps:\n{joined}"

    # Last resort: fall back to user message (kept for safety).
    return facts.message


def _format_retry_narrative(retry_attempts: List[Dict[str, Any]]) -> str:
    """Generate human-readable retry history narrative.
    
    Args:
        retry_attempts: List of attempt records with synthesized_output
        
    Returns:
        Formatted string describing retry history
    """
    if not retry_attempts or len(retry_attempts) <= 1:
        return ""
    
    narrative_lines = []
    for attempt in retry_attempts:
        attempt_num = attempt.get("attempt_number", 0) + 1  # 1-indexed for display
        tool_id = attempt.get("tool_id", "unknown")
        synth = attempt.get("synthesized_output", {})
        status = synth.get("status", "unknown")
        summary = synth.get("summary", "")
        
        # Truncate summary to first 100 chars
        summary_excerpt = summary[:100] if summary else status
        
        # Format as bullet list
        if status in ["failed", "error", "validation_error"]:
            narrative_lines.append(f"- Attempt {attempt_num}: {tool_id} (failed) - {summary_excerpt}")
        else:
            narrative_lines.append(f"- Attempt {attempt_num}: {tool_id} (success) - {summary_excerpt}")
    
    return "\n".join(narrative_lines)


__all__ = ["finalize_turn"]
