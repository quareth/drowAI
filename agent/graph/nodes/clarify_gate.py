"""Non-LLM clarify gate node for deep reasoning interrupt routing.

This node checks persisted planner clarify state and only raises a clarify
interrupt when mandatory blockers are still unanswered. It never calls an LLM.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from .hitl_helpers import (
    normalize_clarify_response,
    normalize_required_blockers,
    request_clarify_answers,
)

_MAX_RETRIES_PER_SLOT = 1


def _normalize_retry_counts(raw_value: Any) -> Dict[str, int]:
    """Normalize retry counts map used for clarify validation retries."""
    if not isinstance(raw_value, dict):
        return {}

    normalized: Dict[str, int] = {}
    for slot, value in raw_value.items():
        slot_name = str(slot).strip()
        if not slot_name:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        normalized[slot_name] = max(0, value)
    return normalized


async def clarify_gate_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Any = None,
) -> Dict[str, Any]:
    """Process persisted clarify requirements and collect missing mandatory answers."""
    del context, config, writer  # clarify gate is a pure control node
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    facts.metadata = metadata

    pending = metadata.get("pending_clarify_request")
    if not isinstance(pending, Mapping):
        return interactive.as_graph_update()

    blockers = normalize_required_blockers(pending.get("required_blockers", []), max_questions=2)
    if not blockers:
        metadata.pop("pending_clarify_request", None)
        metadata.pop("clarify_retry_counts", None)
        metadata["clarify_phase_status"] = "resolved"
        metadata["planner_mode"] = "plan_ready"
        return interactive.as_graph_update()

    clarified_context = metadata.get("clarified_context")
    if not isinstance(clarified_context, dict):
        clarified_context = {}
    metadata["clarified_context"] = clarified_context

    asked_slots = metadata.get("asked_slots")
    if not isinstance(asked_slots, list):
        asked_slots = []
    blocker_slots = [item["slot"] for item in blockers]
    metadata["asked_slots"] = sorted(set(asked_slots + blocker_slots))
    retry_counts = _normalize_retry_counts(metadata.get("clarify_retry_counts"))
    metadata["clarify_retry_counts"] = retry_counts

    unanswered = [
        blocker
        for blocker in blockers
        if not str(clarified_context.get(blocker["slot"]) or "").strip()
    ]
    if not unanswered:
        metadata.pop("pending_clarify_request", None)
        metadata.pop("clarify_retry_counts", None)
        metadata["clarify_phase_status"] = "resolved"
        metadata["planner_mode"] = "plan_ready"
        return interactive.as_graph_update()

    metadata["planner_mode"] = "clarify_required"
    metadata["clarify_phase_status"] = "pending"
    response = request_clarify_answers(
        required_blockers=unanswered,
        context_metadata={"source": "clarify_gate"},
        metadata=metadata,
    )
    normalized = normalize_clarify_response(response)
    answers = normalized.get("answers", {})
    if isinstance(answers, dict):
        unanswered_by_slot = {item["slot"]: item for item in unanswered}
        for slot, blocker in unanswered_by_slot.items():
            value = str(answers.get(slot) or "").strip()
            options = blocker.get("options", [])
            if value and isinstance(options, list) and value in options:
                clarified_context[slot] = value
                retry_counts.pop(slot, None)
            else:
                retry_counts[slot] = retry_counts.get(slot, 0) + 1

    remaining = [
        blocker
        for blocker in blockers
        if not str(clarified_context.get(blocker["slot"]) or "").strip()
    ]
    if not remaining:
        metadata.pop("pending_clarify_request", None)
        metadata.pop("clarify_retry_counts", None)
        metadata["clarify_phase_status"] = "resolved"
        metadata["planner_mode"] = "plan_ready"
    else:
        metadata["clarify_retry_counts"] = retry_counts
        exceeded_slots = [
            blocker["slot"]
            for blocker in remaining
            if retry_counts.get(blocker["slot"], 0) > _MAX_RETRIES_PER_SLOT
        ]
        if exceeded_slots:
            failure_message = (
                "Clarification failed after retry limit for: "
                + ", ".join(sorted(set(exceeded_slots)))
            )
            metadata["clarify_phase_status"] = "failed"
            metadata["clarify_failure_message"] = failure_message
            metadata.pop("pending_clarify_request", None)
            metadata["planner_mode"] = "plan_failed"
            metadata["plan_rejected"] = True
            facts.ensure_decision_history().append(
                f"finalize: {failure_message}"
            )
        else:
            metadata["planner_mode"] = "clarify_required"
            metadata["clarify_phase_status"] = "pending"

    return interactive.as_graph_update()


__all__ = ["clarify_gate_node"]
