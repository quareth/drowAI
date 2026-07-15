"""Shared reasoning-context composer for reasoning-loop prompts.

This module owns :func:`compose_shared_reasoning_sections`, the single
authority for the canonical reasoning projection shared by
``build_think_more_prompt``
(``core/prompts/builders/deep_reasoning.py``),
``build_synthesis_prompt`` (``core/prompts/builders/synthesis.py``),
and ``build_reflection_prompt`` (``core/prompts/builders/reflect.py``).

Responsibilities:
    * Compose the already-rendered section strings for the shared
      reasoning projection (User Input, User Goal, Current Execution
      Context, Prior Current-Turn Phase Memory, Current Focus, Prior
      Active Decision (Advisory), Relevant Prior Findings, Agent Operational
      Capability Surface, Container Environment, Tool Executed, Request
      Contract, Tool Output Summary, Key Findings, Tool Errors,
      Structured Signals, Decision Evidence, Artifact References,
      Current Plan, Todo List, Scope Hints), in that exact order.
    * Be a pure formatter: never mutate state, never log, never call the
      LLM, never resolve turn/phase identity, never select findings.
      Caller-side nodes supply all canonical inputs (turn/phase
      sequences, pre-selected relevant findings, preformatted environment
      context); this composer only formats them.
    * Keep every section data-driven and conditional — omit a section
      entirely when its body is empty. The task tail (and any consumer-
      specific sections such as ``## Loop Details`` for synthesis or
      ``## Stuck Pattern`` / ``## Recent Decisions`` for reflect) are the
      caller's responsibility to append after the composer's output.

Returning already-rendered strings (instead of a registry/dict) keeps the
contract narrow and lets callers simply ``[*shared, *tail]`` join the
result with their existing intro and task tail.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence

from agent.graph.memory.findings import format_relevant_findings
from agent.graph.utils import iteration_memory as _iteration_memory
from core.prompts.builders._text import derive_user_input_and_goal
from core.prompts.builders.post_tool._formatting import (
    as_mapping,
    as_sequence,
    format_plan,
    format_todos,
    get_field,
)
from core.prompts.builders.post_tool.last_tool import extract_last_tool_sections
from core.prompts.builders.post_tool.sections import (
    extract_scope_hint,
    format_active_decision_hint,
    format_current_execution_context,
    format_environment_context,
    format_request_contract,
)


def compose_shared_reasoning_sections(
    state: Mapping[str, object],
    *,
    turn_sequence: Optional[int] = None,
    current_phase_sequence: Optional[int] = None,
    latest_recorded_phase_sequence: Optional[int] = None,
    relevant_findings: Optional[Sequence[Mapping[str, Any]]] = None,
    capability_surface: str = "",
    environment_context: str = "",
) -> List[str]:
    """Compose shared reasoning sections from canonical state.

    The helper returns the rendered section strings in this exact order
    (each entry is the full ``f"## Heading\\n{body}"`` string, except
    ``## Prior Current-Turn Phase Memory`` which is self-headered by
    :func:`render_phase_memory_section`):

        1. ``## User Input``
        2. ``## User Goal``
        3. ``## Current Execution Context``
        4. ``## Prior Current-Turn Phase Memory``
        5. ``## Current Focus``
        6. ``## Prior Active Decision (Advisory)``
        7. ``## Relevant Prior Findings``
        8. ``## Agent Operational Capability Surface``
        9. ``## Container Environment``
        10. ``## Tool Executed``
        11. ``## Request Contract``
        12. ``## Tool Output Summary``
        13. ``## Key Findings``
        14. ``## Tool Errors``
        15. ``## Structured Signals``
        16. ``## Decision Evidence``
        17. ``## Artifact References``
        18. ``## Current Plan``
        19. ``## Todo List``
        20. ``## Scope Hints``

    Each section is conditional on its underlying body being non-empty;
    sections whose data is missing are silently omitted.

    Args:
        state: Graph state mapping (or compatible view). Only
            ``state["facts"]`` (and ``facts["metadata"]`` within it) is
            read.
        turn_sequence: Canonical runtime-stamped turn ordinal supplied by
            the caller; the composer never computes it.
        current_phase_sequence: Phase sequence the current step is about
            to create, supplied by the caller.
        latest_recorded_phase_sequence: Most recent phase already stored
            in the ledger for the active turn, supplied by the caller.
        relevant_findings: Pre-selected target-scoped findings supplied
            by the caller (e.g. via
            ``build_relevant_findings_for_prompt``); the composer only
            formats them and never runs selection.
        capability_surface: Compact list of broad capability families
            derived from the caller-visible tool set.
        environment_context: Preformatted environment section text
            supplied by the caller (e.g. via ``get_environment_full``).

    Returns:
        List of already-rendered section strings, ready for callers to
        join with their intro and task tail using ``"\\n\\n".join(...)``.
    """
    facts = as_mapping(state.get("facts") or {})
    metadata = as_mapping(get_field(facts, "metadata", {}) or {})

    user_input, derived_user_goal = derive_user_input_and_goal(facts)
    current_goal = str(get_field(facts, "current_goal", "") or "").strip()

    plan_body = format_plan(as_sequence(get_field(facts, "plan", [])))
    todo_body = format_todos(as_sequence(get_field(facts, "todo_list", [])))
    scope_hint = extract_scope_hint(metadata)
    active_decision_hint = format_active_decision_hint(metadata)
    request_contract_section = format_request_contract(
        metadata.get("request_contract")
    )
    env_context = format_environment_context(environment_context)
    capability_surface_text = str(capability_surface or "").strip()
    relevant_findings_text = format_relevant_findings(relevant_findings)

    execution_context_section = format_current_execution_context(
        turn_sequence=turn_sequence,
        current_phase_sequence=current_phase_sequence,
        latest_recorded_phase_sequence=latest_recorded_phase_sequence,
    )
    phase_memory_section = _iteration_memory.render_phase_memory_section(
        dict(metadata),
        turn_sequence=turn_sequence,
    )
    last_tool_sections = extract_last_tool_sections(metadata, facts, None)

    sections: List[str] = []

    if user_input:
        sections.append(f"## User Input\n{user_input}")
    if derived_user_goal:
        sections.append(f"## User Goal\n{derived_user_goal}")
    if execution_context_section:
        sections.append(
            f"## Current Execution Context\n{execution_context_section}"
        )
    if phase_memory_section:
        sections.append(phase_memory_section)
    if current_goal:
        sections.append(f"## Current Focus\n{current_goal}")
    if active_decision_hint:
        sections.append(
            f"## Prior Active Decision (Advisory)\n{active_decision_hint}"
        )
    if relevant_findings_text:
        sections.append(f"## Relevant Prior Findings\n{relevant_findings_text}")
    if capability_surface_text:
        sections.append(
            f"## Agent Operational Capability Surface\n{capability_surface_text}"
        )
    if env_context:
        sections.append(f"## Container Environment\n{env_context}")
    if last_tool_sections.get("tool_executed"):
        sections.append(f"## Tool Executed\n{last_tool_sections['tool_executed']}")
    if request_contract_section:
        sections.append(f"## Request Contract\n{request_contract_section}")
    if last_tool_sections.get("tool_output_summary"):
        sections.append(
            f"## Tool Output Summary\n{last_tool_sections['tool_output_summary']}"
        )
    if last_tool_sections.get("key_findings"):
        sections.append(f"## Key Findings\n{last_tool_sections['key_findings']}")
    if last_tool_sections.get("tool_errors"):
        sections.append(f"## Tool Errors\n{last_tool_sections['tool_errors']}")
    if last_tool_sections.get("structured_signals"):
        sections.append(
            f"## Structured Signals\n{last_tool_sections['structured_signals']}"
        )
    if last_tool_sections.get("decision_evidence"):
        sections.append(
            f"## Decision Evidence\n{last_tool_sections['decision_evidence']}"
        )
    if last_tool_sections.get("artifact_refs"):
        sections.append(
            f"## Artifact References\n{last_tool_sections['artifact_refs']}"
        )
    if plan_body:
        sections.append(f"## Current Plan\n{plan_body}")
    if todo_body:
        sections.append(f"## Todo List\n{todo_body}")
    if scope_hint:
        sections.append(f"## Scope Hints\n{scope_hint}")

    return sections


__all__ = ["compose_shared_reasoning_sections"]
