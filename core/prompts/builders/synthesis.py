"""Canonical-projection prompt builder for the ``synthesis`` graph node.

This module owns :func:`build_synthesis_prompt`, which composes the user
prompt for the ``synthesis`` graceful-exit node from the same canonical
runtime state projections that ``think_more`` consumes, plus a synthesis-
specific ``## Loop Details`` section and a plain-text task tail.

Responsibilities:
    * Mirror :func:`DeepReasoningPromptBuilder.build_think_more_prompt`
      composition (User Input/User Goal, Current Execution Context, Phase
      Memory, Current Focus, Active Decision, Relevant Findings,
      Environment, Last-Tool cluster, Request Contract, Plan/Todo, Scope
      Hints) so synthesis sees the same shared context.
    * Keep all sections except ``## Your Task`` data-driven and
      conditional; render no placeholder text when source data is empty.
    * Render synthesis-only ``## Loop Details`` only when
      ``reflection_count > 0`` or ``iterations > 0``; omit otherwise.
    * Preserve the existing plain-text task-tail framing previously held
      in ``core/prompts/constants.build_synthesis_prompt`` so the
      ``synthesis`` output contract (graceful, conversational, never JSON)
      is unchanged.

The wired ``synthesis_node`` (``agent/graph/nodes/synthesis.py``) computes
the keyword-only context kwargs and passes them in; this builder never
computes turn or phase identifiers.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from core.prompts.builders._reasoning_context import compose_shared_reasoning_sections


_SYNTHESIS_INTRO = (
    "I have detected that I'm in a reasoning loop and need to provide a "
    "final response."
)


_SYNTHESIS_TASK_TAIL = """## Your Task
Generate a graceful final response that:
1. Acknowledges you got stuck in a loop (be honest and transparent)
2. Summarizes what you discovered and attempted
3. Explains any partial findings or observations (even if incomplete)
4. Identifies what prevented you from completing the task
5. Suggests concrete alternative approaches the user could try

**Format**:
Write a natural, conversational response (not JSON). Be professional, helpful, and self-aware.
Start with an acknowledgment like \"I apologize, but I've detected I'm stuck in a reasoning loop...\"

**Remember**: Users appreciate honesty and partial value over incomplete results."""


def _format_loop_details(reflection_count: int, iterations: int) -> str:
    """Render the synthesis-only ``## Loop Details`` body.

    Returns an empty string when neither counter is positive so the
    section is omitted entirely.
    """
    lines: List[str] = []
    if reflection_count > 0:
        lines.append(f"Reflection cycles: {reflection_count}")
    if iterations > 0:
        lines.append(f"Total iterations: {iterations}")
    return "\n".join(lines)


def build_synthesis_prompt(
    state: Mapping[str, object],
    *,
    turn_sequence: Optional[int] = None,
    current_phase_sequence: Optional[int] = None,
    latest_recorded_phase_sequence: Optional[int] = None,
    relevant_findings: Optional[List[Mapping[str, Any]]] = None,
    capability_surface: str = "",
    environment_context: str = "",
    reflection_count: int = 0,
    iterations: int = 0,
) -> str:
    """Build the synthesis user prompt from canonical state projections.

    Mirrors :func:`build_think_more_prompt` composition: every section
    except ``## Your Task`` is conditional and omitted when its body is
    empty. Adds a synthesis-only ``## Loop Details`` block (rendered only
    when ``reflection_count > 0`` or ``iterations > 0``) and uses the
    plain-text graceful-exit task tail rather than a JSON schema.

    Args:
        state: Graph state mapping (or compatible view).
        turn_sequence: Canonical runtime-stamped turn ordinal supplied by
            the node; the builder never computes it.
        current_phase_sequence: Phase sequence that the synthesis step is
            about to create, supplied by the node.
        latest_recorded_phase_sequence: Most recent phase already stored
            in the ledger for the active turn, supplied by the node.
        relevant_findings: Pre-selected target-scoped findings supplied by
            the node via ``build_relevant_findings_for_prompt``.
        capability_surface: Compact capability-family summary derived from
            the caller-visible tool set.
        environment_context: Preformatted environment section text supplied
            by the node via ``get_environment_full``.
        reflection_count: Synthesis-specific loop-diagnosis counter
            (reflect-prefixed entries in ``decision_history``).
        iterations: Synthesis-specific loop-diagnosis counter (current
            iteration count from ``facts.iterations``).

    Returns:
        Composed user prompt string.
    """
    sections = compose_shared_reasoning_sections(
        state,
        turn_sequence=turn_sequence,
        current_phase_sequence=current_phase_sequence,
        latest_recorded_phase_sequence=latest_recorded_phase_sequence,
        relevant_findings=relevant_findings,
        capability_surface=capability_surface,
        environment_context=environment_context,
    )

    loop_details_body = _format_loop_details(reflection_count, iterations)
    if loop_details_body:
        sections.append(f"## Loop Details\n{loop_details_body}")

    sections.append(_SYNTHESIS_TASK_TAIL)

    return "\n\n".join([_SYNTHESIS_INTRO, *sections])


__all__ = ["build_synthesis_prompt"]
