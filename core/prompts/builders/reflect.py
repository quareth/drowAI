"""Canonical-projection prompt builder for the ``reflect`` graph node.

This module owns :func:`build_reflection_prompt`, which composes the user
prompt for the ``reflect`` strategic-reflection node from the same
canonical runtime state projections that ``think_more`` and
``synthesis`` consume, plus reflect-only ``## Stuck Pattern`` and
``## Recent Decisions`` sections, and a JSON ``## Your Task`` tail that
preserves the structured-output contract.

Responsibilities:
    * Delegate the 19 shared reasoning sections to
      :func:`compose_shared_reasoning_sections` so reflect sees the same
      canonical context (User Input/User Goal, Current Execution Context,
      Phase Memory, Current Focus, Active Decision, Relevant Findings,
      Environment, Last-Tool cluster, Request Contract, Plan/Todo, Scope
      Hints).
    * Append reflect-only ``## Stuck Pattern`` (rendered only when
      ``problem`` is non-empty) and ``## Recent Decisions`` (rendered only
      when ``recent_decisions`` is non-empty / not ``None``). The builder
      does not re-slice ``recent_decisions`` — the node passes at most
      five entries via ``facts.safe_decision_history[-5:]``.
    * Frame reflection as internal, local blocker steering rather than a
      user-facing rewrite of the whole plan.
    * Preserve the JSON task tail's body (``**Required Response Format**:``
      subheading and the JSON code block) byte-for-byte because it is the
      structured-output contract for ``REFLECT_STRUCTURED_OUTPUT``. The
      top-level heading migrates from the legacy ``**Your Task**:``
      bold-line form to ``## Your Task`` markdown form, but the inner
      subheading stays as bold-line because it is output-contract text,
      not a top-level section style.

The wired ``reflect_node`` (``agent/graph/nodes/reflect.py``) computes
the keyword-only context kwargs (turn/phase sequences, relevant findings,
environment context, recent decisions) and passes them in; this builder
never computes turn or phase identifiers, never selects findings, and
never logs.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from core.prompts.builders._reasoning_context import compose_shared_reasoning_sections


_REFLECT_INTRO = (
    "Internal agent steering: the current run is stuck on the active "
    "todo/current blocker and needs a local direction correction."
    "\n\n"
    "NOTE: Tool failures are handled automatically by immediate retry logic. "
    "This reflection is about why the current path is not producing progress "
    "for the active blocker, not a user-facing report and not a full plan rewrite."
)


_REFLECT_TASK_TAIL = """## Your Task
1. Analyze WHY the current path is not making progress on the active todo/current blocker.
2. Identify the smallest direction change that could produce new evidence or decide the blocker is not recoverable.
3. Propose local next-direction changes only; do not rewrite the overall plan.
4. Do not recommend exact tools unless those tools or broad capabilities are present in the provided context.
5. Write for downstream agent steering, not for the end user.

**Required Response Format**:
```json
{
  "root_cause": "Local analysis of why the active blocker is not progressing",
  "alternative_approaches": ["Small next-direction change 1", "Small next-direction change 2", ...]
}
```

Focus on the active blocker and immediate recovery direction, not broad replanning or individual tool retry mechanics.
Provide your reflection as valid JSON."""


def build_reflection_prompt(
    state: Mapping[str, object],
    *,
    problem: str,
    recent_decisions: Optional[Sequence[str]] = None,
    turn_sequence: Optional[int] = None,
    current_phase_sequence: Optional[int] = None,
    latest_recorded_phase_sequence: Optional[int] = None,
    relevant_findings: Optional[Sequence[Mapping[str, Any]]] = None,
    capability_surface: str = "",
    environment_context: str = "",
) -> str:
    """Build the reflect user prompt from canonical state projections.

    Composes the verbatim intro framing, the 19 shared reasoning sections
    via :func:`compose_shared_reasoning_sections`, reflect-only
    ``## Stuck Pattern`` and ``## Recent Decisions`` sections (each
    omitted when empty), and the JSON ``## Your Task`` tail.

    Every section except ``## Your Task`` is conditional and omitted when
    its body is empty. The builder never re-slices ``recent_decisions``;
    the node passes the already-bounded slice.

    Args:
        state: Graph state mapping (or compatible view).
        problem: Stuck-pattern description produced by ``_identify_problem``.
            When empty / falsy, the ``## Stuck Pattern`` section is omitted.
        recent_decisions: At-most-five recent decision history entries
            supplied by the node via ``facts.safe_decision_history[-5:]``.
            When ``None`` or empty, the ``## Recent Decisions`` section
            is omitted.
        turn_sequence: Canonical runtime-stamped turn ordinal supplied by
            the node; the builder never computes it.
        current_phase_sequence: Phase sequence that the reflect step is
            about to create, supplied by the node.
        latest_recorded_phase_sequence: Most recent phase already stored
            in the ledger for the active turn, supplied by the node.
        relevant_findings: Pre-selected target-scoped findings supplied by
            the node via ``build_relevant_findings_for_prompt``.
        capability_surface: Compact capability-family summary derived from
            the caller-visible tool set.
        environment_context: Preformatted environment section text supplied
            by the node via ``get_environment_full``.

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

    if isinstance(problem, str) and problem.strip():
        sections.append(f"## Stuck Pattern\n{problem}")

    if recent_decisions:
        decisions_body = "\n".join(f"- {entry}" for entry in recent_decisions)
        sections.append(f"## Recent Decisions\n{decisions_body}")

    sections.append(_REFLECT_TASK_TAIL)

    return "\n\n".join([_REFLECT_INTRO, *sections])


def build_reflection_fallback_guidance(problem: str) -> str:
    """Build deterministic guidance used when the reflect LLM call fails.

    The reflect node records this text into current-turn phase memory so
    downstream PTR sees the same strategic fallback guidance through the
    existing memory-rendering path.
    """
    normalized_problem = problem.strip() if isinstance(problem, str) else ""
    problem_section = (
        f"\n\nCurrent stuck pattern:\n{normalized_problem}"
        if normalized_problem
        else ""
    )
    return (
        "Reflection fallback triggered.\n\n"
        "The run has not made progress on the current todo/task for multiple "
        "phases, so reflection was requested. However, the reflection LLM call "
        "failed, and this is deterministic fallback guidance rather than a full "
        "strategic analysis."
        f"{problem_section}\n\n"
        "Assume the current direction is not working. Do not repeat the same "
        "action path or same assumption unless the next step is materially "
        "different and directly addresses the blocker.\n\n"
        "Reassess whether the current task can be solved with the information "
        "currently available:\n"
        "- If required information is missing and cannot be collected from the "
        "current environment, finalize/synthesize clearly with what is blocked "
        "and what input is needed.\n"
        "- If recovery is still possible, change direction explicitly and choose "
        "a different approach that can produce new evidence.\n"
        "- If no materially different approach is justified, finalize/synthesize "
        "with a clean explanation of what got stuck and why.\n\n"
        "Keep the next response concise and useful to the user: state what is "
        "stuck, why progress stopped, and what is required to proceed."
    )


__all__ = ["build_reflection_fallback_guidance", "build_reflection_prompt"]
