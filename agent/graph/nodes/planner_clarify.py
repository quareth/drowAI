"""Planner-side clarify contract helpers.

This module owns helper logic for planner-produced clarify contracts and
planner failure state. It does not raise user interrupts; ``clarify_gate.py``
owns the non-LLM interrupt flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..state import InteractiveState
from .hitl_helpers import build_interrupt_id, normalize_required_blockers

logger = logging.getLogger(__name__)

CLARIFY_PHASE_PENDING = "pending"
CLARIFY_PHASE_RESOLVED = "resolved"
CLARIFY_PHASE_FAILED = "failed"


@dataclass(frozen=True)
class PlannerClarifyOutcome:
    """Result of planner-side clarify preflight state handling."""

    handled: bool
    update: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ClarifyRequiredDecision:
    """Result of applying a planner-produced clarify contract."""

    is_clarify_required: bool
    update: Optional[Dict[str, Any]]


def normalize_planner_required_blockers(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Normalize and bound clarify blockers from planner output contract."""
    clarify_request = contract.get("clarify_request")
    blockers = (
        clarify_request.get("required_blockers", [])
        if isinstance(clarify_request, Mapping)
        else []
    )
    return normalize_required_blockers(blockers, max_questions=2)


def build_slots_signature(required_blockers: List[Dict[str, Any]]) -> str:
    """Build a stable slot signature for clarify loop detection."""
    slots = sorted(
        str(item.get("slot") or "").strip()
        for item in required_blockers
        if str(item.get("slot") or "").strip()
    )
    return "|".join(slots)


def fail_planner_due_to_clarify(
    interactive: InteractiveState,
    *,
    reason: str,
) -> Dict[str, Any]:
    """Set deterministic failure state when clarify policy is violated."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    facts.metadata = metadata
    metadata["planner_mode"] = "plan_failed"
    metadata["clarify_phase_status"] = CLARIFY_PHASE_FAILED
    metadata["clarify_failure_message"] = reason
    metadata["plan_rejected"] = True
    metadata.pop("pending_clarify_request", None)
    metadata.pop("clarify_retry_counts", None)
    facts.plan = []
    facts.todo_list = []
    facts.current_goal = ""
    facts.ensure_decision_history().append(f"finalize: {reason}")
    logger.warning("[PLANNER] Clarify failure: %s", reason)
    return interactive.as_graph_update()


def mark_clarify_plan_ready(metadata: Dict[str, Any]) -> None:
    """Clear pending planner-side clarify state after a plan-ready result."""
    metadata["planner_mode"] = "plan_ready"
    metadata.pop("pending_clarify_request", None)
    metadata.pop("clarify_retry_counts", None)
    metadata["clarify_phase_status"] = CLARIFY_PHASE_RESOLVED


def handle_existing_clarify_state(
    interactive: InteractiveState,
    *,
    stage: str = "all",
) -> PlannerClarifyOutcome:
    """Handle persisted planner-side clarify state before planner generation."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    facts.metadata = metadata

    clarified_context = metadata.get("clarified_context")
    if not isinstance(clarified_context, dict):
        clarified_context = {}
    metadata["clarified_context"] = clarified_context

    asked_slots = metadata.get("asked_slots")
    if not isinstance(asked_slots, list):
        asked_slots = []

    clarify_cycle_count = metadata.get("clarify_cycle_count")
    if isinstance(clarify_cycle_count, bool) or not isinstance(clarify_cycle_count, int):
        clarify_cycle_count = 0
    metadata["clarify_cycle_count"] = clarify_cycle_count

    if stage in {"all", "failed"} and metadata.get("clarify_phase_status") == CLARIFY_PHASE_FAILED:
        reason = str(metadata.get("clarify_failure_message") or "Clarification failed")
        return PlannerClarifyOutcome(
            handled=True,
            update=fail_planner_due_to_clarify(interactive, reason=reason),
        )

    if stage == "failed":
        return PlannerClarifyOutcome(handled=False, update=None)

    pending_clarify_request = metadata.get("pending_clarify_request")
    if isinstance(pending_clarify_request, Mapping):
        required_blockers = normalize_planner_required_blockers(
            {"clarify_request": pending_clarify_request}
        )
        required_slots = [str(item.get("slot")) for item in required_blockers if item.get("slot")]
        if required_slots:
            metadata["clarify_phase_status"] = CLARIFY_PHASE_PENDING
            metadata.setdefault("clarify_ticket_id", build_interrupt_id())
            metadata["clarify_slots_signature"] = build_slots_signature(required_blockers)
            metadata["planner_mode"] = "clarify_required"
            metadata["asked_slots"] = sorted(set(asked_slots + required_slots))
            missing_slots = [
                slot for slot in required_slots if not str(clarified_context.get(slot) or "").strip()
            ]
            if missing_slots:
                logger.info(
                    "[PLANNER] Waiting for clarify answers; skipping planner LLM call (missing slots=%s)",
                    ",".join(missing_slots),
                )
                return PlannerClarifyOutcome(handled=True, update=interactive.as_graph_update())

            logger.info(
                "[PLANNER] Clarify answers available for required slots; continuing planner generation"
            )
            mark_clarify_plan_ready(metadata)
        else:
            mark_clarify_plan_ready(metadata)

    return PlannerClarifyOutcome(handled=False, update=None)


def apply_clarify_required_contract(
    interactive: InteractiveState,
    parsed_contract: Mapping[str, Any],
) -> ClarifyRequiredDecision:
    """Persist planner clarify-required contract state or deterministic failure."""
    if parsed_contract.get("mode") != "clarify_required":
        return ClarifyRequiredDecision(is_clarify_required=False, update=None)

    facts = interactive.facts
    metadata = facts.ensure_metadata()
    facts.metadata = metadata
    required_blockers = normalize_planner_required_blockers(parsed_contract)
    if not required_blockers:
        return ClarifyRequiredDecision(
            is_clarify_required=True,
            update=fail_planner_due_to_clarify(
                interactive,
                reason=(
                    "Planner returned an invalid clarification contract "
                    "(select options required)."
                ),
            ),
        )

    slots_signature = build_slots_signature(required_blockers)
    previous_signature = metadata.get("clarify_slots_signature")
    if not isinstance(previous_signature, str):
        previous_signature = ""
    previous_phase_status = str(metadata.get("clarify_phase_status") or "").strip().lower()

    if (
        previous_phase_status == CLARIFY_PHASE_RESOLVED
        and previous_signature
        and previous_signature == slots_signature
    ):
        return ClarifyRequiredDecision(
            is_clarify_required=True,
            update=fail_planner_due_to_clarify(
                interactive,
                reason=(
                    "Clarification loop detected for the same required inputs."
                ),
            ),
        )

    metadata["planner_mode"] = "clarify_required"
    metadata["pending_clarify_request"] = {
        "required_blockers": required_blockers
    }
    metadata["clarify_ticket_id"] = build_interrupt_id()
    metadata["clarify_phase_status"] = CLARIFY_PHASE_PENDING
    metadata["clarify_slots_signature"] = slots_signature
    prior_cycle_count = metadata.get("clarify_cycle_count")
    if isinstance(prior_cycle_count, bool) or not isinstance(prior_cycle_count, int):
        prior_cycle_count = 0
    metadata["clarify_cycle_count"] = prior_cycle_count + 1
    metadata["clarify_retry_counts"] = {}
    clarified_context_map = metadata.get("clarified_context")
    if not isinstance(clarified_context_map, dict):
        clarified_context_map = {}
    metadata["clarified_context"] = clarified_context_map
    metadata["asked_slots"] = sorted(
        set(
            list(metadata.get("asked_slots", []))
            + [item["slot"] for item in required_blockers]
        )
    )
    facts.plan = []
    facts.todo_list = []
    facts.current_goal = ""
    logger.info(
        "[PLANNER] Persisted clarify_required decision with %d blocker(s)",
        len(required_blockers),
    )
    return ClarifyRequiredDecision(
        is_clarify_required=True,
        update=interactive.as_graph_update(),
    )
