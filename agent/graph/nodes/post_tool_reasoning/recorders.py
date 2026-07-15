"""State recording functions for post-tool reasoning.

This module handles recording observations and decisions to state,
maintaining context continuity across reasoning iterations.

In addition to the legacy prose compatibility writes
(``trace.observations`` and ``metadata["synthesized_output"]``), the
observation recorder appends one structured PTR record to the shared
current-turn phase ledger owned by :mod:`agent.graph.utils.iteration_memory`.
The PTR record's prompt-facing sections are derived deterministically from
validated ``PostToolReasoningOutput`` fields; identity fields
(``turn_sequence``, ``phase_sequence``, ``source``) are stamped by the shared
helper from runtime metadata.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PostToolReasoningOutput, ToolIntent
    from ...state import InteractiveState

from ...utils import iteration_memory as _iteration_memory

logger = logging.getLogger(__name__)


# =============================================================================
# Decision Recorder
# =============================================================================


def record_decision(
    interactive: "InteractiveState",
    output: "PostToolReasoningOutput",
) -> None:
    """Record the decision to state for graph routing and debugging.
    
    Updates multiple state locations for consistency with existing patterns:
    - facts.decision_history: Appends "action: reasoning" entry for routing
    - trace.decision_log: Appends structured record for debugging
    - trace.reasoning: Appends visibility entry for logging
    
    Also updates the stuck_counter to track repeated actions.
    
    Args:
        interactive: The InteractiveState to update (mutated in place).
        output: The PostToolReasoningOutput containing the decision.
    """
    from ...builders.common_edges import increment_stuck_counter
    
    action = output.next_action
    reasoning = output.action_reasoning
    
    # Update stuck counter BEFORE appending to history
    # This allows increment_stuck_counter to compare previous action with current
    state_dict = interactive.as_graph_state()
    stuck_update = increment_stuck_counter(state_dict, action)
    
    # Apply stuck counter update
    if "facts" in stuck_update:
        interactive.facts.stuck_counter = stuck_update["facts"].get("stuck_counter", 0)
    
    # Record to decision_history (used by routing and guardrails)
    decision_entry = f"{action}: {reasoning}"
    decision_history = interactive.facts.ensure_decision_history()
    decision_history.append(decision_entry)
    _write_router_candidate_decision(
        interactive,
        next_action=action,
        reasoning=reasoning,
    )
    
    # CRITICAL DEBUG: Log decision_history update
    logger.info(
        f"[RECORD_DECISION] ✓ Appended to decision_history: '{decision_entry}' "
        f"(total entries: {len(decision_history)})"
    )
    logger.info(f"[RECORD_DECISION] Full decision_history: {decision_history}")
    
    # Record to decision_log (structured, for debugging)
    decision_record = {
        "iteration": interactive.facts.iterations,
        "action": action,
        "reasoning": reasoning,
        "observation_preview": output.observation[:100] + "..." if len(output.observation) > 100 else output.observation,
        "stuck_counter": interactive.facts.stuck_counter,
        "source": "post_tool_reasoning",
    }
    if interactive.trace.decision_log is None:
        interactive.trace.decision_log = []
    interactive.trace.decision_log.append(decision_record)
    
    # Record to reasoning trace (for visibility/logging)
    trace_entry = f"[POST_TOOL_REASONING] Decision: {action} - {reasoning}"
    if interactive.trace.reasoning is None:
        interactive.trace.reasoning = []
    interactive.trace.reasoning.append(trace_entry)
    
    logger.info(
        f"[POST_TOOL_REASONING] Recorded decision: {action} "
        f"(stuck_counter={interactive.facts.stuck_counter}, "
        f"iteration={interactive.facts.iterations})"
    )


def _write_router_candidate_decision(
    interactive: "InteractiveState",
    *,
    next_action: str,
    reasoning: str,
) -> None:
    """Write router candidate_decision contract for PTR → router handoff."""
    metadata = interactive.facts.ensure_metadata()
    turn_sequence = metadata.get("turn_sequence")
    phase_sequence = _resolve_candidate_phase_sequence(metadata)
    normalized_action = str(next_action or "").strip().lower().replace(" ", "_")
    if not isinstance(turn_sequence, int) or not isinstance(phase_sequence, int):
        interactive.facts.set_candidate_decision(None)
        logger.debug(
            "[POST_TOOL_REASONING] Skipped candidate_decision write due to missing "
            "turn/phase binding (turn=%r, phase=%r)",
            turn_sequence,
            phase_sequence,
        )
        return

    candidate_payload = {
        "next_action": normalized_action,
        "action_reasoning": str(reasoning or ""),
        "decision_source": "ptr",
        "candidate_id": (
            f"ptr-{turn_sequence}-{phase_sequence}-"
            f"{interactive.facts.iterations}-{len(interactive.facts.safe_decision_history)}"
        ),
        "producer_node": "post_tool_reasoning",
        "turn_sequence": turn_sequence,
        "phase_sequence": phase_sequence,
    }
    interactive.facts.set_candidate_decision(candidate_payload)


def _resolve_candidate_phase_sequence(metadata: Mapping[str, Any]) -> Optional[int]:
    """Resolve phase sequence for candidate_decision invocation binding."""
    direct_phase = metadata.get("phase_sequence")
    if isinstance(direct_phase, int):
        return direct_phase

    current_ptr_phase = metadata.get("current_ptr_phase_sequence")
    if isinstance(current_ptr_phase, int):
        return current_ptr_phase

    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return None

    current_turn_phases = working_memory.get("current_turn_phases")
    if not isinstance(current_turn_phases, list) or not current_turn_phases:
        return None

    latest_record = current_turn_phases[-1]
    if not isinstance(latest_record, Mapping):
        return None

    latest_phase = latest_record.get("phase_sequence")
    if isinstance(latest_phase, int):
        return latest_phase
    return None


# =============================================================================
# Observation Recorder
# =============================================================================


def record_observation(
    interactive: "InteractiveState",
    output: "PostToolReasoningOutput",
) -> None:
    """Record the observation to state for context continuity.

    Updates:
    - trace.observations: Appends the full observation text
    - metadata["synthesized_output"]["observation_text"]: For downstream nodes
    - metadata["working_memory"]["current_turn_phases"]: Appends one
      structured PTR phase record via
      :func:`agent.graph.utils.iteration_memory.append`. Runtime identity
      fields (``turn_sequence``, ``phase_sequence``, ``source``) are stamped
      by the helper using ``metadata["turn_sequence"]``; PTR output only
      supplies validated decision/observation fields that are rendered here
      as prompt-readable sections.

    Args:
        interactive: The InteractiveState to update (mutated in place).
        output: The PostToolReasoningOutput containing the observation.
    """
    observation = output.observation

    # Record to trace.observations
    if interactive.trace.observations is None:
        interactive.trace.observations = []
    interactive.trace.observations.append(observation)

    # Update synthesized_output with observation_text (for observation_adapter)
    metadata = interactive.facts.ensure_metadata()

    synthesized = metadata.get("synthesized_output")
    if not isinstance(synthesized, dict):
        synthesized = {}
    synthesized["observation_text"] = observation
    metadata["synthesized_output"] = synthesized

    # Dual-write: append one structured PTR phase record to the shared
    # current-turn phase ledger so later PTR iterations can see this step
    # as ordered structured memory rather than re-parsing prose history.
    # The append is delegated to the shared helper so schema, ordering,
    # and identity stamping are not hand-shaped here.
    _append_ptr_phase_record(metadata, output)

    # Log observation preview
    preview = observation[:160]
    logger.info(
        f"[POST_TOOL_REASONING] Recorded observation: {preview}"
        f"{'…' if len(observation) > 160 else ''}"
    )


def _append_ptr_phase_record(
    metadata: Dict[str, Any],
    output: "PostToolReasoningOutput",
) -> None:
    """Append a deterministic PTR record to the iteration-memory ledger.

    This helper isolates the dual-write so the main recorder keeps its
    prose-compat responsibilities obvious and the ledger contract stays
    fully delegated to :mod:`agent.graph.utils.iteration_memory`.

    The append is a no-op only when ``metadata["turn_sequence"]`` is missing
    or non-integer. Runtime is the only authority for ``turn_sequence``, so
    we do not fabricate one here.
    """
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        logger.debug(
            "[POST_TOOL_REASONING] Skipping iteration_memory append: "
            "metadata['turn_sequence'] missing or not an int "
            "(value=%r)",
            turn_sequence,
        )
        return

    payload = {"sections": _build_ptr_phase_sections(output)}

    record = _iteration_memory.append(
        metadata,
        turn_sequence=turn_sequence,
        source="ptr",
        payload=payload,
    )
    logger.debug(
        "[POST_TOOL_REASONING] Appended ptr iteration_memory record: "
        "turn=%s phase=%s sections=%s",
        record.get("turn_sequence"),
        record.get("phase_sequence"),
        len(record.get("sections", [])),
    )


def _build_ptr_phase_sections(
    output: "PostToolReasoningOutput",
) -> list[dict[str, str]]:
    """Render prompt-readable PTR phase sections from validated output."""
    sections = [
        {"heading": "PTR Decision", "body": _render_ptr_decision(output)},
        {
            "heading": "Action Reasoning",
            "body": _compact_block(getattr(output, "action_reasoning", "")),
        },
    ]

    tool_intent = getattr(output, "tool_intent", None)
    tool_intent_body = _render_tool_intent_section(tool_intent)
    if tool_intent_body:
        sections.append({"heading": "Tool Intent", "body": tool_intent_body})

    effective_next_goal = _compact_block(getattr(output, "effective_next_goal", ""))
    if effective_next_goal:
        sections.append(
            {"heading": "Effective Next Goal", "body": effective_next_goal}
        )

    sections.append(
        {
            "heading": "Todo Progress",
            "body": _render_todo_progress(getattr(output, "todo_progress", None)),
        }
    )
    sections.append(
        {
            "heading": "Observation",
            "body": _compact_block(getattr(output, "observation", "")),
        }
    )

    candidate_body = _render_candidate_observations(
        getattr(output, "candidate_observations", None)
    )
    if candidate_body:
        sections.append(
            {"heading": "Candidate Observations", "body": candidate_body}
        )

    return sections


def _render_ptr_decision(output: "PostToolReasoningOutput") -> str:
    """Render the routing decision fields as labeled lines."""
    lines = [
        f"next_action: {_single_line(getattr(output, 'next_action', ''))}",
        (
            "user_goal_achieved: "
            f"{_bool_label(getattr(output, 'user_goal_achieved', False))}"
        ),
        (
            "failure_detected: "
            f"{_bool_label(getattr(output, 'failure_detected', False))}"
        ),
        (
            "failure_category: "
            f"{_single_line(getattr(output, 'failure_category', None)) or 'none'}"
        ),
        (
            "retry_suggested: "
            f"{_bool_label(getattr(output, 'retry_suggested', False))}"
        ),
    ]
    return "\n".join(lines)


def _render_tool_intent_section(tool_intent: Any) -> str:
    """Render structured tool intent as labeled lines."""
    if tool_intent is None:
        return ""

    lines = []
    description = _single_line(getattr(tool_intent, "description", ""))
    target = _single_line(getattr(tool_intent, "target", ""))
    focus = _single_line(getattr(tool_intent, "focus", ""))
    if description:
        lines.append(f"description: {description}")
    if target:
        lines.append(f"target: {target}")
    if focus:
        lines.append(f"focus: {focus}")
    return "\n".join(lines)


def _render_todo_progress(todo_progress: Any) -> str:
    """Render changed todo status updates as compact labeled lines."""
    if not todo_progress:
        return "No todo progress updates reported."

    lines: list[str] = []
    for position, item in enumerate(todo_progress):
        index = getattr(item, "index", position)
        prefix = f"todo[{index if isinstance(index, int) else position}]"
        status = _single_line(getattr(item, "status", ""))
        completion_type = _single_line(getattr(item, "completion_type", ""))
        completion_reason = _single_line(getattr(item, "completion_reason", ""))

        if status:
            lines.append(f"{prefix}.status: {status}")
        if completion_type:
            lines.append(f"{prefix}.completion_type: {completion_type}")
        if completion_reason:
            lines.append(f"{prefix}.completion_reason: {completion_reason}")

    return "\n".join(lines) or "No todo progress updates reported."


def _render_candidate_observations(candidate_observations: Any) -> str:
    """Render candidate observations only when they fit a compact prompt block."""
    if not candidate_observations:
        return ""

    candidates = list(candidate_observations)
    if len(candidates) > 3:
        return ""

    lines: list[str] = []
    for position, candidate in enumerate(candidates):
        prefix = f"candidate[{position}]"
        observation_type = _single_line(getattr(candidate, "observation_type", ""))
        subject_type = _single_line(getattr(candidate, "subject_type", ""))
        subject_key = _single_line(getattr(candidate, "subject_key_hint", ""))
        rationale = _single_line(getattr(candidate, "rationale", ""), limit=220)

        if not (observation_type and subject_type and subject_key and rationale):
            continue

        lines.extend(
            [
                f"{prefix}.observation_type: {observation_type}",
                f"{prefix}.subject: {subject_type} {subject_key}",
                f"{prefix}.confidence: {_single_line(getattr(candidate, 'confidence', ''))}",
                f"{prefix}.rationale: {rationale}",
            ]
        )

        attribute_lines = _render_candidate_attributes(
            getattr(candidate, "attributes", None)
        )
        if attribute_lines:
            lines.append(f"{prefix}.attributes: {attribute_lines}")

        evidence_lines = _render_candidate_evidence(
            getattr(candidate, "evidence_refs", None)
        )
        if evidence_lines:
            lines.append(f"{prefix}.evidence: {evidence_lines}")

        vulnerability = getattr(candidate, "vulnerability", None)
        if vulnerability is not None:
            title = _single_line(getattr(vulnerability, "title", ""))
            severity = _single_line(getattr(vulnerability, "severity", ""))
            vuln_id = _single_line(getattr(vulnerability, "id", ""))
            parts = [part for part in (vuln_id, title, severity) if part]
            if parts:
                lines.append(f"{prefix}.vulnerability: {' | '.join(parts)}")

    body = "\n".join(lines)
    if not body or len(body) > 1200:
        return ""
    return body


def _render_candidate_attributes(attributes: Any) -> str:
    """Render at most three candidate attributes as key=value pairs."""
    if not attributes:
        return ""

    rendered = []
    for attribute in list(attributes)[:3]:
        key = _single_line(getattr(attribute, "key", ""))
        value = _single_line(getattr(attribute, "value", ""), limit=120)
        if key:
            rendered.append(f"{key}={value}")
    return "; ".join(rendered)


def _render_candidate_evidence(evidence_refs: Any) -> str:
    """Render at most two evidence excerpts for candidate observations."""
    if not evidence_refs:
        return ""

    excerpts = []
    for evidence_ref in list(evidence_refs)[:2]:
        excerpt = _single_line(getattr(evidence_ref, "excerpt", ""), limit=160)
        if excerpt:
            excerpts.append(excerpt)
    return " | ".join(excerpts)


def _bool_label(value: Any) -> str:
    """Return lowercase boolean text for prompt-readable decision lines."""
    return "true" if bool(value) else "false"


def _compact_block(value: Any, *, limit: int = 900) -> str:
    """Return compact multi-line text suitable for a section body."""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _single_line(value: Any, *, limit: int = 300) -> str:
    """Return a compact one-line label value."""
    if value is None:
        return ""
    text = " ".join(str(value).strip().split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


# =============================================================================
# Tool Intent Formatter
# =============================================================================


def format_tool_intent_for_hint(tool_intent: Optional["ToolIntent"]) -> Optional[str]:
    """Format structured tool_intent as a hint string for parameter generator.
    
    This converts the structured LLM output into a natural language hint
    that can be passed to downstream planning/parameter generation.
    
    Args:
        tool_intent: Structured tool intent from LLM, or None.
        
    Returns:
        Formatted hint string, or None if no intent provided.
    """
    if not tool_intent:
        return None
    
    parts = [tool_intent.description]
    
    if tool_intent.target:
        parts.append(f"Target: {tool_intent.target}")
    
    if tool_intent.focus:
        parts.append(f"Focus: {tool_intent.focus}")
    
    hint = " | ".join(parts)
    logger.debug(f"[POST_TOOL_REASONING] Formatted tool intent hint: '{hint}'")
    return hint


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "record_decision",
    "record_observation",
    "format_tool_intent_for_hint",
]



