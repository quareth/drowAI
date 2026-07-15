"""Unified prompt builder for the simple-tool and deep reasoning finalizers.

This module owns the single capability-aware prompt assembly used by the
unified finalizer node. Prompt text is sourced from the versioned
``core/prompts/versions/finalizer/v1/`` family via ``TemplateLoader`` and
combined with capability-conditional sections so both simple-tool and deep
reasoning runs produce the same operator-voice 4-part output skeleton:
What we just did → What the evidence shows → What it means → Next move.

Section helpers are intentionally split one-per-concern; the orchestration
function only assembles a section list and joins. New sections must follow
the existing helper contract: return either a non-empty rendered string or
``""``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.graph.memory.findings import format_findings_for_finalizer
from agent.graph.utils import iteration_memory as _iteration_memory
from core.prompts.builders._text import strip_referenced_prior_turns_label
from core.prompts.loader import TemplateLoader

_TEMPLATE_LOADER = TemplateLoader(Path(__file__).resolve().parents[1] / "versions")

SYSTEM_BASE = _TEMPLATE_LOADER.load_latest_version("finalizer", "system_base.txt").rstrip(
    "\n"
)
ADDENDUM_RETRY = _TEMPLATE_LOADER.load_latest_version(
    "finalizer", "addendum_retry.txt"
).rstrip("\n")
ADDENDUM_DR = _TEMPLATE_LOADER.load_latest_version(
    "finalizer", "addendum_dr.txt"
).rstrip("\n")
ADDENDUM_ANALYST = _TEMPLATE_LOADER.load_latest_version(
    "finalizer", "addendum_analyst.txt"
).rstrip("\n")
USER_INSTRUCTIONS = _TEMPLATE_LOADER.load_latest_version(
    "finalizer", "instructions.txt"
).rstrip("\n")


# ---------------------------------------------------------------------------
# Shared text utilities
# ---------------------------------------------------------------------------


def _as_string_list(values: Any) -> List[str]:
    """Normalize arbitrary list-like inputs into stripped string lists."""
    if isinstance(values, list):
        return [str(value).strip() for value in values if str(value).strip()]
    return []


def _dedupe_strings(values: Sequence[str]) -> List[str]:
    """Return case-insensitive de-duplicated strings preserving first order."""
    seen: set[str] = set()
    deduped: List[str] = []
    for value in values:
        key = str(value).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(str(value).strip())
    return deduped


# ---------------------------------------------------------------------------
# Findings resolution (shared by simple-tool path)
# ---------------------------------------------------------------------------


def _is_candidate_finding(row: Mapping[str, Any]) -> bool:
    """Return True when one finding row is analyst-level candidate data."""
    return str(row.get("assertion_level") or "").strip().lower() == "candidate"


def _extract_candidate_findings(
    relevant_findings: Optional[Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Filter relevant findings down to candidate observation rows."""
    candidates: List[Dict[str, Any]] = []
    for row in relevant_findings or []:
        if not isinstance(row, Mapping):
            continue
        if _is_candidate_finding(row):
            candidates.append(dict(row))
    return candidates


def _extract_candidate_vulnerabilities(
    findings: Sequence[Mapping[str, Any]],
) -> List[str]:
    """Extract vulnerability hypothesis lines from candidate findings."""
    vulnerabilities: List[str] = []
    for row in findings:
        details = row.get("details")
        if not isinstance(details, Mapping):
            continue
        vulnerability = details.get("vulnerability")
        if vulnerability in (None, ""):
            continue
        confidence = details.get("vulnerability_confidence")
        if confidence in (None, ""):
            vulnerabilities.append(str(vulnerability))
        else:
            vulnerabilities.append(f"{vulnerability} (confidence={confidence})")
    return _dedupe_strings(vulnerabilities)


def _extract_candidate_actions(
    findings: Sequence[Mapping[str, Any]],
    *,
    current_goal: str,
) -> List[str]:
    """Derive action hints from effective goal plus candidate rationales."""
    actions: List[str] = []
    goal = str(current_goal or "").strip()
    if goal:
        actions.append(goal)
    for row in findings:
        details = row.get("details")
        if not isinstance(details, Mapping):
            continue
        rationale = str(details.get("rationale") or "").strip()
        if rationale:
            actions.append(rationale)
    return _dedupe_strings(actions)


def _resolve_finding_lists(
    *,
    synthesized: Mapping[str, Any],
    aggregated_findings: Optional[Mapping[str, Any]],
    relevant_findings: Optional[Sequence[Mapping[str, Any]]],
    current_goal: str,
) -> Dict[str, Any]:
    """Resolve analyst-preferred findings with synthesizer fallback lists."""
    fallback_key_findings = (
        _as_string_list(aggregated_findings.get("all_findings"))
        if isinstance(aggregated_findings, Mapping)
        else _as_string_list(synthesized.get("key_findings"))
    )
    fallback_vulnerabilities = (
        _as_string_list(aggregated_findings.get("all_vulnerabilities"))
        if isinstance(aggregated_findings, Mapping)
        else _as_string_list(synthesized.get("vulnerabilities"))
    )
    fallback_actions = (
        _as_string_list(aggregated_findings.get("all_actions"))
        if isinstance(aggregated_findings, Mapping)
        else _as_string_list(synthesized.get("next_actions"))
    )
    candidate_findings = _extract_candidate_findings(relevant_findings)
    if not candidate_findings:
        return {
            "source": "synthesizer",
            "candidate_findings": [],
            "key_findings": fallback_key_findings,
            "raw_tool_findings": [],
            "vulnerabilities": fallback_vulnerabilities,
            "next_actions": fallback_actions,
        }
    return {
        "source": "analyst",
        "candidate_findings": candidate_findings,
        "key_findings": [],
        "raw_tool_findings": fallback_key_findings,
        "vulnerabilities": _extract_candidate_vulnerabilities(candidate_findings)
        or fallback_vulnerabilities,
        "next_actions": _extract_candidate_actions(
            candidate_findings,
            current_goal=current_goal,
        )
        or fallback_actions,
    }


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _build_user_request_section(user_message: str) -> str:
    """Render the user-request section."""
    text = str(user_message or "").strip()
    if not text:
        return ""
    return f"## User Request\n{text}"


def _build_referenced_prior_turns_section(referenced_prior_turns: Optional[str]) -> str:
    """Render canonical referenced-turn context when available."""
    text = strip_referenced_prior_turns_label(referenced_prior_turns)
    if not text:
        return ""
    return (
        "## Referenced Prior Turns\n"
        f"{text}\n\n"
        "Use these as canonical prior conversation context. Do not claim an exact "
        "quote unless this text supports it."
    )


def _build_output_format_section(requested_output_format: Optional[str]) -> str:
    """Render output-format instructions for final answer formatting."""
    if requested_output_format == "json":
        return (
            "## Output Format Request\n"
            "The user requested the final answer in **JSON** format.\n"
            "You MUST wrap your JSON output in a markdown code fence like this:\n"
            "```json\n{...your JSON here...}\n```\n"
            "This ensures proper syntax highlighting in the UI."
        )
    if requested_output_format in {"csv", "markdown"}:
        return (
            "## Output Format Request\n"
            f"The user requested the final answer in **{requested_output_format.upper()}** format.\n"
            "Follow that request in your final response."
        )
    return ""


def _build_retry_history_section(
    retry_attempts: Optional[Sequence[Mapping[str, Any]]],
    aggregated_findings: Optional[Mapping[str, Any]],
) -> str:
    """Render retry narrative when multi-attempt aggregation is available."""
    if not retry_attempts or len(retry_attempts) <= 1 or not aggregated_findings:
        return ""
    retry_narrative = str(aggregated_findings.get("retry_narrative") or "").strip()
    if not retry_narrative:
        return ""
    return f"## Retry History\n{retry_narrative}"


def _build_active_decision_section(metadata: Optional[Mapping[str, Any]]) -> str:
    """Render advisory active-decision continuity section."""
    # Lazy import to avoid post_tool package init cycle when this module is
    # loaded by core.prompts.constants.
    from core.prompts.builders.post_tool.sections import format_active_decision_hint

    hint = format_active_decision_hint(metadata or {})
    if not hint:
        return ""
    return f"## Active Decision (advisory)\n{hint}"


def _build_phase_memory_section(
    metadata: Optional[Mapping[str, Any]],
    *,
    turn_sequence: Optional[int],
) -> str:
    """Render prior current-turn phase-memory section."""
    if not isinstance(metadata, Mapping):
        return ""
    return _iteration_memory.render_phase_memory_section(
        dict(metadata),
        turn_sequence=turn_sequence,
    )


def _build_effective_goal_section(current_goal: Optional[str]) -> str:
    """Render effective-goal section from current facts state."""
    goal = str(current_goal or "").strip()
    if not goal:
        return ""
    return f"## Effective Goal\n{goal}"


def _build_ptr_observation_section(synthesized: Mapping[str, Any]) -> str:
    """Render PTR observation prose carried in synthesized output."""
    observation = str(synthesized.get("observation_text") or "").strip()
    if not observation:
        return ""
    return f"## PTR Analyst Observation\n{observation}"


def _build_tool_summary_section(synthesized: Mapping[str, Any]) -> str:
    """Render raw tool summary block from synthesized output."""
    if not synthesized:
        return ""
    tool_name = synthesized.get("tool", "unknown tool")
    return (
        f"## Tool Summary ({tool_name})\n"
        f"{synthesized.get('summary') or 'No summary provided.'}"
    )


def _build_findings_sections(
    *,
    synthesized: Mapping[str, Any],
    aggregated_findings: Optional[Mapping[str, Any]],
    relevant_findings: Optional[Sequence[Mapping[str, Any]]],
    current_goal: str,
) -> List[str]:
    """Render findings, vulnerabilities, and actions blocks with precedence."""
    resolved = _resolve_finding_lists(
        synthesized=synthesized,
        aggregated_findings=aggregated_findings,
        relevant_findings=relevant_findings,
        current_goal=current_goal,
    )
    sections: List[str] = []
    if resolved["source"] == "analyst" and resolved["candidate_findings"]:
        sections.append("### Key Findings (analyst-derived)")
        sections.append(format_findings_for_finalizer(resolved["candidate_findings"]))
        if resolved["raw_tool_findings"]:
            sections.append("Raw-tool key_findings (compressed by the synthesizer):")
            sections.extend(f"- {finding}" for finding in resolved["raw_tool_findings"])
    elif resolved["key_findings"]:
        sections.append("### Key Findings")
        sections.extend(f"- {finding}" for finding in resolved["key_findings"])

    if resolved["vulnerabilities"]:
        sections.append("### Vulnerabilities")
        sections.extend(f"- {vuln}" for vuln in resolved["vulnerabilities"])

    if resolved["next_actions"]:
        sections.append("### Recommended Actions")
        sections.extend(f"- {action}" for action in resolved["next_actions"])
    return sections


# ---- DR-only section helpers -----------------------------------------------


def _build_recent_transcript_section(transcript_text: str) -> str:
    """Render the bundle-derived conversation transcript (DR only)."""
    text = str(transcript_text or "").strip()
    if not text:
        return ""
    from core.prompts.constants import CONVERSATION_SECTION_LABEL

    return f"## {CONVERSATION_SECTION_LABEL}\n{text}"


def _build_runtime_state_section(runtime_state_text: str) -> str:
    """Render the bundle-derived runtime state (DR only)."""
    text = str(runtime_state_text or "").strip()
    if not text:
        return ""
    return f"## Runtime State\n{text}"


def _build_targets_section(targets: Optional[Sequence[str]]) -> str:
    """Render scope targets discovered for the engagement (DR only)."""
    if not targets:
        return ""
    cleaned = [str(target).strip() for target in targets if str(target).strip()]
    if not cleaned:
        return ""
    return "## Targets / Scope\n" + ", ".join(cleaned)


def _build_plan_section(plan: Optional[Sequence[str]]) -> str:
    """Render the deep reasoning plan section (DR only)."""
    if not plan:
        return ""
    # Lazy import: node_utils pulls in heavy graph types and is not needed
    # for simple-tool builds.
    from agent.graph.nodes.node_utils import format_plan

    rendered = format_plan(list(plan))
    if not rendered.strip():
        return ""
    return f"## Plan\n{rendered}"


def _build_todo_section(
    todo_list: Optional[Iterable[Any]],
    *,
    limit: int = 8,
) -> str:
    """Render the deep reasoning todo status section (DR only)."""
    if not todo_list:
        return ""
    entries: List[str] = []
    # Lazy import to keep simple-tool builds free of DR state dependencies.
    from agent.graph.state import TodoItem

    for item in todo_list:
        if isinstance(item, TodoItem):
            entries.append(f"- [{item.status.value}] {item.description}")
        elif isinstance(item, str):
            entries.append(f"- {item}")
        elif isinstance(item, Mapping):
            description = str(item.get("description") or item)
            status = str(item.get("status") or "pending")
            entries.append(f"- [{status}] {description}")
        else:
            entries.append(f"- {str(item)}")
    if not entries:
        return ""
    return "## Todo Status\n" + "\n".join(entries[:limit])


def _build_iterations_section(
    records: Optional[Mapping[str, Any]],
    *,
    limit: int = 5,
) -> str:
    """Render the deep reasoning iteration overview (DR only)."""
    if not records:
        return ""
    summaries: List[str] = []
    try:
        ordered_keys = sorted(records.keys(), key=lambda value: int(value))
    except (TypeError, ValueError):
        ordered_keys = list(records.keys())
    for index_key in ordered_keys:
        if len(summaries) >= limit:
            break
        entry = records.get(index_key) or {}
        lines: List[str] = [f"### Iteration {index_key}"]

        reasoning_snippets: Iterable[str] = entry.get("reasoning") or []
        if reasoning_snippets:
            lines.append("Reasoning:")
            for snippet in reasoning_snippets:
                lines.append(f"- {snippet}")

        tool_record: Mapping[str, Any] = entry.get("tool") or {}
        if tool_record:
            tool_name = tool_record.get("tool", "unknown tool")
            status = tool_record.get("status", "unknown")
            summary = tool_record.get("summary")
            lines.append(f"Tool: {tool_name} (status: {status})")
            if summary:
                lines.append(f"  Summary: {summary}")
            command = tool_record.get("command")
            if command:
                lines.append(f"  Command: {command}")

        observation = entry.get("observation")
        if observation:
            lines.append(f"Observation: {observation}")

        summaries.append("\n".join(lines))

    if not summaries:
        return ""
    return "## Iterations Overview\n" + "\n\n".join(summaries)


def _build_observations_section(
    observations: Optional[Sequence[str]],
    *,
    limit: int = 8,
) -> str:
    """Render the deep reasoning key observations section (DR only)."""
    if not observations:
        return ""
    from agent.graph.nodes.node_utils import format_observations

    rendered = format_observations(list(observations), limit=limit)
    if not rendered.strip():
        return ""
    return f"## Key Observations\n{rendered}"


def _build_tool_activity_section(
    executed_tools: Optional[Sequence[Mapping[str, Any]]],
    *,
    limit: int = 5,
) -> str:
    """Render the deep reasoning tool activity section (DR only)."""
    if not executed_tools:
        return ""
    from agent.graph.nodes.node_utils import format_tool_attempts

    rendered = format_tool_attempts(list(executed_tools), limit=limit)
    if not rendered.strip():
        return ""
    return f"## Tool Activity\n{rendered}"


def _build_user_instructions_section() -> str:
    """Return the closing user-prompt instructions block (always present)."""
    return USER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------


def _is_deep_reasoning(capability: Optional[str]) -> bool:
    return str(capability or "").strip().lower() == "deep_reasoning"


def _assemble_system_prompt(
    *,
    retry_attempts: Optional[Sequence[Mapping[str, Any]]],
    capability: Optional[str],
    candidate_findings: Sequence[Mapping[str, Any]],
) -> str:
    """Return the additive operator-voice system prompt for this turn."""
    parts: List[str] = [SYSTEM_BASE]
    if retry_attempts and len(retry_attempts) > 1:
        parts.append(ADDENDUM_RETRY)
    if _is_deep_reasoning(capability):
        parts.append(ADDENDUM_DR)
    if candidate_findings:
        parts.append(ADDENDUM_ANALYST)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_finalize_prompts(
    *,
    user_message: str,
    synthesized: Optional[Mapping[str, Any]] = None,
    last_result: Optional[Mapping[str, Any]] = None,
    retry_attempts: Optional[Sequence[Mapping[str, Any]]] = None,
    aggregated_findings: Optional[Mapping[str, Any]] = None,
    requested_output_format: Optional[str] = None,
    referenced_prior_turns: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    relevant_findings: Optional[Sequence[Mapping[str, Any]]] = None,
    current_goal: Optional[str] = None,
    turn_sequence: Optional[int] = None,
    capability: Optional[str] = None,
    plan: Optional[Sequence[str]] = None,
    todo_list: Optional[Iterable[Any]] = None,
    dr_iteration_records: Optional[Mapping[str, Any]] = None,
    observations: Optional[Sequence[str]] = None,
    executed_tools: Optional[Sequence[Mapping[str, Any]]] = None,
    transcript_text: str = "",
    runtime_state_text: str = "",
    targets: Optional[Sequence[str]] = None,
) -> Tuple[str, str]:
    """Build system + user prompts for the unified finalizer node.

    Capability gating:
      - ``capability == "deep_reasoning"`` activates the DR addendum and the
        DR-specific sections (transcript, runtime state, targets, plan, todos,
        iterations, observations, tool activity). The user-request section
        is suppressed because the bundle transcript already carries the
        in-flight ``latest=true`` turn.
      - Any other capability (default) renders the simple-tool section spine
        (user request, output format, retry history, effective goal, active
        decision, phase memory, PTR observation, tool summary, findings).
    """

    _ = last_result  # currently unused; preserved for builder symmetry
    is_dr = _is_deep_reasoning(capability)
    synthesized_map: Mapping[str, Any] = synthesized or {}
    current_goal_text = str(current_goal or "").strip()
    candidate_findings = _extract_candidate_findings(relevant_findings)

    system_prompt = _assemble_system_prompt(
        retry_attempts=retry_attempts,
        capability=capability,
        candidate_findings=candidate_findings,
    )

    sections: List[str] = []

    if is_dr:
        sections.append(_build_recent_transcript_section(transcript_text))
        sections.append(_build_referenced_prior_turns_section(referenced_prior_turns))
        sections.append(_build_runtime_state_section(runtime_state_text))
        sections.append(_build_targets_section(targets))
        sections.append(_build_plan_section(plan))
        sections.append(_build_todo_section(todo_list))
        sections.append(_build_iterations_section(dr_iteration_records))
        sections.append(_build_observations_section(observations))
        sections.append(_build_tool_activity_section(executed_tools))
    else:
        sections.append(_build_user_request_section(user_message))
        sections.append(_build_referenced_prior_turns_section(referenced_prior_turns))
        sections.append(_build_output_format_section(requested_output_format))
        sections.append(_build_retry_history_section(retry_attempts, aggregated_findings))
        sections.append(_build_effective_goal_section(current_goal_text))
        sections.append(_build_active_decision_section(metadata))
        sections.append(
            _build_phase_memory_section(metadata, turn_sequence=turn_sequence)
        )
        sections.append(_build_ptr_observation_section(synthesized_map))
        sections.append(_build_tool_summary_section(synthesized_map))
        sections.extend(
            _build_findings_sections(
                synthesized=synthesized_map,
                aggregated_findings=aggregated_findings,
                relevant_findings=relevant_findings,
                current_goal=current_goal_text,
            )
        )

    sections.append(_build_user_instructions_section())

    user_prompt = "\n\n".join(section for section in sections if section)
    return system_prompt, user_prompt


__all__ = [
    "ADDENDUM_ANALYST",
    "ADDENDUM_DR",
    "ADDENDUM_RETRY",
    "SYSTEM_BASE",
    "USER_INSTRUCTIONS",
    "build_finalize_prompts",
]
