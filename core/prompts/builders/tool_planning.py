"""Prompt builder for tool planning templates.

This module loads versioned templates from
`core/prompts/versions/tool_planning/` via the shared `TemplateLoader`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent.graph.memory.findings import format_relevant_findings
from agent.tools.catalog_visibility import filter_visible_tool_ids
from core.prompts.builders._text import strip_referenced_prior_turns_label
from core.prompts.constants import render_intent_brief_block
from core.prompts.loader import TemplateLoader
from core.runbooks.models import RunbookStage
from core.runbooks.service import RunbookService


_VERSIONS_ROOT = Path(__file__).resolve().parents[1] / "versions"
_LOADER = TemplateLoader(_VERSIONS_ROOT)
_RUNBOOK_SERVICE = RunbookService()
_ARTIFACT_SEARCH_TOOL_ID = "artifact.search"
_ARTIFACT_READ_TOOL_ID = "artifact.read"
_CVE_LOOKUP_TOOL_ID = "knowledge.cve_lookup"


def _render_latest(template_name: str, **context: Any) -> str:
    template = _LOADER.load_latest_version("tool_planning", template_name)
    safe_context = {key: str(value) if value is not None else "" for key, value in context.items()}
    return template.format_map(safe_context)


def _format_history(history: Sequence[Dict[str, Any]], max_turns: int = 5) -> str:
    """Format conversation history for context.

    Includes recent observations and tool outputs to help LLM avoid:
    - Selecting tools that already failed
    - Repeating the same tool selection
    - Missing context about what was already tried
    """

    if not history:
        return "(No previous conversation)"

    recent_history = history[-max_turns:]
    lines = []
    for turn in recent_history:
        role = turn.get("role", "unknown")
        content = str(turn.get("content", ""))[:300]  # Slightly longer for better context
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_relevant_findings_section(findings: Sequence[Dict[str, Any]] | None) -> str:
    """Render relevant prior findings as an explicit planner section."""
    findings_text = format_relevant_findings(findings)
    if not findings_text:
        return ""
    return f"Relevant Prior Findings:\n{findings_text}"


def _build_optional_context_block(title: str, text: Optional[str]) -> str:
    """Render an optional prompt block only when bounded content is present."""
    if not text:
        return ""
    normalized = str(text).strip()[:1200]
    if not normalized:
        return ""
    return f"\n{title}:\n{normalized}"


def _build_latest_phase_memory_block(text: Optional[str]) -> str:
    """Render newest phase memory with selector-specific precedence rules."""
    if not text:
        return ""
    normalized = str(text).strip()
    if not normalized:
        return ""
    return (
        "\nLatest Current-Turn Phase (fresh runtime steering for the CURRENT action):\n"
        f"{normalized}\n\n"
        "Precedence:\n"
        "- Latest Current-Turn Phase is the freshest runtime steering signal for the immediate next action.\n"
        "- Turn Execution Brief remains authoritative for the original user goal, explicit constraints, and success condition.\n"
        "- If they conflict on the immediate next action, select tools for the latest phase while preserving all non-conflicting user constraints.\n"
    )


def _normalize_execution_strategy(raw_strategy: Any) -> str:
    """Return a builder-safe execution strategy label."""
    strategy = str(raw_strategy or "sequential").strip().lower()
    if strategy not in {"parallel", "sequential"}:
        return "sequential"
    return strategy


def _build_selector_decision_block(
    *,
    execution_strategy: Any,
    target: str,
    targets: Optional[Sequence[str]] = None,
) -> str:
    """Render upstream selector scheduling hints for the native tool-call builder."""
    strategy = _normalize_execution_strategy(execution_strategy)
    normalized_targets: List[str] = []
    for candidate in [target, *(targets or [])]:
        text = str(candidate or "").strip()
        if text and text not in normalized_targets:
            normalized_targets.append(text)

    lines = [
        "Selector Decision (advisory scheduling hint; Candidate Tools are not a mandatory execution list):",
        f'- Requested execution strategy: "{strategy}"',
    ]
    if not normalized_targets:
        lines.append("- Targets: (none)")
    elif len(normalized_targets) == 1:
        lines.append(f"- Target: {normalized_targets[0]}")
    else:
        lines.append(f"- Primary target: {normalized_targets[0]}")
        lines.append(f"- All targets for this turn: {json.dumps(normalized_targets)}")
    return "\n".join(lines)


def _build_cve_lookup_policy_text(visible_tool_ids: Sequence[str]) -> str:
    """Return scoped CVE lookup selection guidance when the tool is visible."""

    if _CVE_LOOKUP_TOOL_ID not in set(visible_tool_ids):
        return ""

    return (
        "\nCVE Lookup Policy:\n"
        "- If the user explicitly asks whether detected software may be vulnerable, you may include "
        "`knowledge.cve_lookup` after the discovery or versioning tool.\n"
        "- Use it only when you have both a concrete product and version.\n"
        "- If version evidence is weak or approximate, plan to state that the lookup result is lower-confidence.\n"
        "- Do not select `knowledge.cve_lookup` if the user did not ask for vulnerability checking, "
        "or if product+version evidence is missing.\n"
        "- Do not replace the requested primary tool with `knowledge.cve_lookup`; add it only when it helps answer the user's "
        "vulnerability question.\n"
    )


def _format_catalog(catalog: List[Dict[str, Any]]) -> str:
    """Format tool catalog for display."""

    if not catalog:
        return "(No tools available)"

    lines = []
    for entry in catalog:
        tool_id = entry.get("id", "")
        name = tool_id or entry.get("name", "")
        description = entry.get("description", "")
        lines.append(f"- {name}: {description}")
    return "\n".join(lines)


def _extract_tool_ids(raw_tools: Sequence[Any]) -> List[str]:
    """Extract tool ids from prompt-facing tool collections."""
    resolved: List[str] = []
    for item in raw_tools:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("id") or item.get("tool_id") or "").strip()
        else:
            value = str(item).strip()
        if value and value not in resolved:
            resolved.append(value)
    return resolved


def _format_available_tools(
    resolved_tools: Sequence[Any],
    catalog: Sequence[Mapping[str, Any]],
) -> str:
    """Render the visible tool list using catalog descriptions when available."""
    catalog_entries = [dict(entry) for entry in catalog if isinstance(entry, Mapping)]
    if not catalog_entries:
        catalog_entries = [
            dict(entry) for entry in resolved_tools if isinstance(entry, Mapping)
        ]
    catalog_text = _format_catalog(catalog_entries)
    if catalog_text != "(No tools available)":
        return catalog_text
    tool_ids = _extract_tool_ids(resolved_tools)
    if not tool_ids:
        return "(No tools available)"
    return "\n".join(f"- {tool_id}" for tool_id in tool_ids)


def _build_artifact_policy_text(tool_ids: Sequence[str]) -> str:
    """Return artifact-tool policy text.

    Artifact DB tools are hidden from LLM-facing prompts, so this remains a
    defensive no-op for legacy callers that still pass artifact IDs.
    """
    _ = tool_ids
    return ""


def _build_artifact_parameter_policy(selected_tools: Sequence[str]) -> str:
    """Return artifact parameter guardrails.

    Artifact DB tools are hidden from LLM-facing prompts, so this remains a
    defensive no-op for legacy callers that still pass artifact IDs.
    """
    _ = selected_tools
    return ""


def _build_tool_runbooks_section(selected_tools: Sequence[str]) -> str:
    """Return scoped tool runbooks for the native parameter builder."""

    runbooks = _RUNBOOK_SERVICE.render_for_tools(
        selected_tools=selected_tools,
        stage=RunbookStage.TOOL_PARAMETERS,
    )
    if not runbooks:
        return ""
    return f"\n{runbooks}"


def _build_tool_selection_runbooks_section(selected_categories: Sequence[str]) -> str:
    """Return scoped tool runbooks for candidate tool selection."""

    runbooks = _RUNBOOK_SERVICE.render_for_categories(
        selected_categories=selected_categories,
        stage=RunbookStage.TOOL_SELECTION,
    )
    if not runbooks:
        return ""
    return f"\n\n{runbooks}"


def _format_artifact_file_metadata(
    artifact_file_metadata: Sequence[Mapping[str, Any]] | None,
) -> str:
    """Render bounded artifact file metadata for filesystem parameter planning."""

    entries = [
        dict(entry)
        for entry in list(artifact_file_metadata or [])[:8]
        if isinstance(entry, Mapping)
    ]
    if not entries:
        return ""

    lines = ["\nArtifact File Metadata:"]
    for entry in entries:
        path = str(entry.get("path") or "").strip() or "(unknown)"
        status = str(entry.get("status") or "unavailable").strip() or "unavailable"
        parts = [f"path={path}", f"status={status}"]
        label = str(entry.get("label") or "").strip()
        if label:
            parts.append(f"label={label}")
        if status == "ready":
            if entry.get("size_bytes") is not None:
                parts.append(f"size_bytes={entry.get('size_bytes')}")
            if entry.get("line_count") is not None:
                parts.append(f"line_count={entry.get('line_count')}")
        else:
            reason = str(entry.get("reason") or "unavailable").strip()
            parts.append(f"reason={reason}")
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def _normalize_progress_status(value: Any) -> str:
    """Normalize todo status values to prompt-facing progression states."""

    raw = value
    if raw is not None and hasattr(raw, "value"):
        raw = getattr(raw, "value")
    status = str(raw or "").strip().lower()
    if status == "in_progress":
        return "in_progress"
    if status in {"complete_positive", "complete_negative", "completed"}:
        return "completed"
    if status in {"skipped", "exhausted"}:
        return "skipped"
    return "pending"


def _extract_todo_status(todo_item: Any) -> str:
    """Extract normalized progress status from supported todo item shapes."""

    if isinstance(todo_item, dict):
        return _normalize_progress_status(todo_item.get("status"))
    return _normalize_progress_status(getattr(todo_item, "status", None))


def _extract_todo_text(todo_item: Any) -> str:
    """Extract todo text/description from supported todo item shapes."""

    if isinstance(todo_item, str):
        return todo_item.strip()
    if isinstance(todo_item, dict):
        text = todo_item.get("description") or todo_item.get("text")
        return str(text).strip() if text else ""
    description = getattr(todo_item, "description", None)
    if description:
        return str(description).strip()
    text = getattr(todo_item, "text", None)
    return str(text).strip() if text else ""


def _to_progress_marker(status: str) -> str:
    """Convert normalized status to explicit marker used in prompts."""

    if status == "in_progress":
        return "[in_progress]"
    if status == "completed":
        return "[completed]"
    if status == "skipped":
        return "[skipped]"
    return "[pending]"


def _format_plan_context(
    plan_text: Optional[List[str]],
    current_goal: Optional[str],
    has_tool_hint: bool = False,
    todo_list: Optional[Sequence[Any]] = None,
) -> str:
    """Format plan text for display in parameter prompts."""

    if not plan_text and not current_goal and not todo_list:
        return "(No plan available)"

    lines: List[str] = []

    if current_goal:
        lines.append(f"**Current Goal**: {current_goal}")

    if plan_text:
        if has_tool_hint:
            lines.append(
                "**Background Plan with Progress Indicators** "
                "(for context only - follow the DIRECTIVE above instead):"
            )
        else:
            lines.append("**Plan Steps with Progress Indicators** (extract targets/parameters from these):")
        for i, step in enumerate(plan_text[:5], 1):  # Limit to first 5 steps
            status = "pending"
            if todo_list and i - 1 < len(todo_list):
                status = _extract_todo_status(todo_list[i - 1])
            lines.append(f"  {i}. {step} {_to_progress_marker(status)}")
    elif todo_list:
        lines.append("**Todo Progress**:")
        for i, todo_item in enumerate(todo_list[:5], 1):
            text = _extract_todo_text(todo_item) or f"Step {i}"
            status = _extract_todo_status(todo_item)
            lines.append(f"  {i}. {text} {_to_progress_marker(status)}")

    return "\n".join(lines) if lines else "(No plan available)"


class ToolPlanningPromptBuilder:
    """Builds consistent prompts for all stages of tool planning."""

    def build_system_prompt(
        self,
        user_message: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Build the tool-planning system prompt.

        The select/parameter prompts no longer consume transcript text, but this
        method keeps the historical arguments for compatibility with existing
        planner call sites.
        """
        return _render_latest("system.txt")

    def build_select_tools_system_prompt(self) -> str:
        """Return the dedicated selection-call system prompt.

        Candidate selection still uses the generic planner system prompt; the
        native call-builder has a dedicated parameter system prompt.
        """
        return self.build_system_prompt()

    def build_tool_parameters_system_prompt(
        self,
        *,
        max_committed_tools_per_batch: int = 1,
    ) -> str:
        """Build the native tool-call builder system prompt."""
        try:
            cap_value = int(max_committed_tools_per_batch)
        except (TypeError, ValueError):
            cap_value = 1
        if cap_value < 1:
            cap_value = 1
        return _render_latest(
            "tool_parameters_system.txt",
            max_committed_tools_per_batch=cap_value,
        )

    def build_resolve_tools_prompt(
        self,
        user_message: str,
        conversation_history: List[Dict[str, Any]],
        target: str,
        phase: str,
        constraints: Dict[str, Any],
        relevant_findings: Sequence[Dict[str, Any]] | None = None,
    ) -> str:
        """Build prompt for the resolve_tools stage (LLM call #1)."""

        history_text = _format_history(conversation_history)
        findings_text = format_relevant_findings(relevant_findings)

        return _render_latest(
            "resolve_tools.txt",
            user_message=user_message,
            conversation_history=history_text,
            target=target,
            phase=phase,
            constraints=json.dumps(constraints) if constraints else "{}",
            relevant_findings_count=len(list(relevant_findings or [])),
            relevant_findings=findings_text or "none",
        )

    def build_select_tools_prompt(
        self,
        *,
        user_message: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        resolved_tools: Optional[List[Any]] = None,
        catalog: Optional[List[Dict[str, Any]]] = None,
        target: str,
        phase: str,
        constraints: Dict[str, Any],
        intent_brief: Optional[Mapping[str, Any]] = None,
        next_tool_hint: Optional[str] = None,
        latest_phase_memory: Optional[str] = None,
        capability_surface: Optional[str] = None,
        working_memory_summary: Optional[str] = None,
        referenced_prior_turns: Optional[str] = None,
        relevant_findings: Sequence[Dict[str, Any]] | None = None,
        selected_categories: Optional[Sequence[str]] = None,
        max_tools_per_action: int = 3,
        max_committed_tools_per_batch: int = 1,
    ) -> str:
        """Build prompt for the tool selection stage (LLM call #2).

        ``max_tools_per_action`` is rendered into the template so the
        candidate cap is config-resident (sourced by the caller from
        ``AgentConfig.max_tools_per_action``); the template never bakes a
        literal cap.

        ``max_committed_tools_per_batch`` tells the selector the downstream
        builder cap so it can choose a requested strategy without treating
        candidates as a mandatory execution list.
        """

        resolved_tools = list(resolved_tools or [])
        catalog = list(catalog or [])
        visible_tool_ids = filter_visible_tool_ids(_extract_tool_ids(resolved_tools))
        if not visible_tool_ids:
            visible_tool_ids = filter_visible_tool_ids(_extract_tool_ids(catalog))
        visible_tool_id_set = set(visible_tool_ids)
        visible_catalog = [
            entry
            for entry in catalog
            if str(entry.get("id") or entry.get("tool_id") or "").strip() in visible_tool_id_set
        ]
        visible_resolved_tools = [
            tool
            for tool in resolved_tools
            if str(
                tool
                if isinstance(tool, str)
                else getattr(tool, "tool_id", "")
                or (tool.get("id") if isinstance(tool, Mapping) else "")
                or (tool.get("tool_id") if isinstance(tool, Mapping) else "")
            ).strip()
            in visible_tool_id_set
        ]
        if not visible_resolved_tools:
            visible_resolved_tools = list(visible_tool_ids)
        artifact_policy = _build_artifact_policy_text(visible_tool_ids)
        cve_lookup_policy = _build_cve_lookup_policy_text(visible_tool_ids)
        tool_runbooks = _build_tool_selection_runbooks_section(
            list(selected_categories or [])
        )
        relevant_findings_section = _build_relevant_findings_section(relevant_findings)
        working_memory_snapshot_block = _build_optional_context_block(
            "Working Memory Snapshot",
            working_memory_summary,
        )
        latest_phase_memory_block = _build_latest_phase_memory_block(
            latest_phase_memory,
        )
        capability_surface_block = _build_optional_context_block(
            "Available Agent Capability Surface",
            capability_surface,
        )
        referenced_prior_turns_block = _build_optional_context_block(
            "Referenced Prior Turns",
            strip_referenced_prior_turns_label(referenced_prior_turns),
        )
        available_tools_text = _format_available_tools(visible_resolved_tools, visible_catalog)
        intent_brief_block = render_intent_brief_block(intent_brief)

        tool_hint_text = ""
        if next_tool_hint:
            tool_hint_text = (
                "\n**CRITICAL - CURRENT INTENT (HIGHEST PRIORITY):**\n"
                f"The agent's current decision is: \"{next_tool_hint}\"\n"
                "Propose candidate tools that can accomplish THIS INTENT, not the brief's resolved intent.\n"
                "You are a candidate generator: the builder will commit the final batch.\n\n"
            )

        try:
            cap_value = int(max_tools_per_action)
        except (TypeError, ValueError):
            cap_value = 3
        if cap_value < 1:
            cap_value = 1
        try:
            committed_cap_value = int(max_committed_tools_per_batch)
        except (TypeError, ValueError):
            committed_cap_value = 1
        if committed_cap_value < 1:
            committed_cap_value = 1

        return _render_latest(
            "select_tools.txt",
            resolved_tools=available_tools_text,
            target=target,
            phase=phase,
            constraints=json.dumps(constraints) if constraints else "{}",
            intent_brief_block=intent_brief_block,
            next_tool_hint=tool_hint_text,
            latest_phase_memory_block=latest_phase_memory_block,
            capability_surface_block=capability_surface_block,
            working_memory_snapshot_block=working_memory_snapshot_block,
            referenced_prior_turns_block=referenced_prior_turns_block,
            artifact_policy=artifact_policy,
            cve_lookup_policy=cve_lookup_policy,
            tool_runbooks=tool_runbooks,
            relevant_findings_section=relevant_findings_section,
            max_tools_per_action=cap_value,
            max_committed_tools_per_batch=committed_cap_value,
        )

    def build_tool_parameters_prompt(
        self,
        *,
        user_message: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        selected_tools: Optional[List[str]] = None,
        target: str,
        phase: str,
        constraints: Dict[str, Any],
        intent_brief: Optional[Mapping[str, Any]] = None,
        plan_text: Optional[List[str]] = None,
        current_goal: Optional[str] = None,
        todo_list: Optional[Sequence[Any]] = None,
        next_tool_hint: Optional[str] = None,
        previous_tool: Optional[str] = None,
        previous_tool_output_summary: Optional[str] = None,
        working_memory_summary: Optional[str] = None,
        referenced_prior_turns: Optional[str] = None,
        relevant_findings: Sequence[Dict[str, Any]] | None = None,
        max_committed_tools_per_batch: int = 1,
        execution_strategy: str = "sequential",
        targets: Optional[Sequence[str]] = None,
        artifact_file_metadata: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> str:
        """Build prompt for the tool parameters stage (LLM call #3).

        ``max_committed_tools_per_batch`` is rendered into the template so the
        cap is config-resident (sourced by the caller from
        ``AgentConfig.max_committed_tools_per_batch``); the template never
        bakes a literal cap.

        ``execution_strategy`` and ``targets`` carry upstream selector output
        so the builder knows whether parallel multi-call batches are requested
        and which targets the brief may require action on.
        """
        selected_tools = [
            str(tool_id)
            for tool_id in list(selected_tools or [])
            if str(tool_id) not in {_ARTIFACT_SEARCH_TOOL_ID, _ARTIFACT_READ_TOOL_ID}
        ]

        tool_hint_text = ""
        has_hint = bool(next_tool_hint)
        if next_tool_hint:
            tool_hint_text = (
                "\n**CRITICAL - POST-TOOL REASONING DIRECTIVE (HIGHEST PRIORITY):**\n"
                f"The agent just decided: \"{next_tool_hint}\"\n"
                "This directive defines the pending work for this iteration and narrows the original Turn Execution Brief.\n"
                "Commit only calls required by this directive/current goal; do not recommit successful current-turn work from Working Memory unless the directive explicitly asks for a rerun.\n"
                "You MUST configure the tool to match this directive, NOT repeat previous scan types.\n"
                "IGNORE the plan steps below - they are outdated. Follow this directive EXACTLY.\n"
            )

        previous_tool_text = ""
        if previous_tool:
            previous_tool_text = f"\n**PREVIOUS TOOL EXECUTED**: {previous_tool}"
            if previous_tool_output_summary:
                previous_tool_text += f"\n**Output Summary**: {previous_tool_output_summary}"
            previous_tool_text += "\n"

        plan_context_text = _format_plan_context(
            plan_text,
            current_goal,
            has_tool_hint=has_hint,
            todo_list=todo_list,
        )
        artifact_parameter_policy = _build_artifact_parameter_policy(selected_tools)
        tool_runbooks = _build_tool_runbooks_section(selected_tools)
        artifact_file_metadata_text = _format_artifact_file_metadata(artifact_file_metadata)
        relevant_findings_section = _build_relevant_findings_section(relevant_findings)
        working_memory_snapshot_block = _build_optional_context_block(
            "Working Memory Snapshot",
            working_memory_summary,
        )
        referenced_prior_turns_block = _build_optional_context_block(
            "Referenced Prior Turns",
            strip_referenced_prior_turns_label(referenced_prior_turns),
        )
        intent_brief_block = render_intent_brief_block(intent_brief)

        try:
            cap_value = int(max_committed_tools_per_batch)
        except (TypeError, ValueError):
            cap_value = 1
        if cap_value < 1:
            cap_value = 1

        selector_decision_block = _build_selector_decision_block(
            execution_strategy=execution_strategy,
            target=target,
            targets=targets,
        )

        return _render_latest(
            "tool_parameters.txt",
            selected_tools=json.dumps(selected_tools),
            phase=phase,
            constraints=json.dumps(constraints) if constraints else "{}",
            intent_brief_block=intent_brief_block,
            selector_decision_block=selector_decision_block,
            plan_context=plan_context_text,
            next_tool_hint=tool_hint_text,
            previous_tool_context=previous_tool_text,
            working_memory_snapshot_block=working_memory_snapshot_block,
            referenced_prior_turns_block=referenced_prior_turns_block,
            artifact_parameter_policy=artifact_parameter_policy,
            tool_runbooks=tool_runbooks,
            artifact_file_metadata=artifact_file_metadata_text,
            relevant_findings_section=relevant_findings_section,
            max_committed_tools_per_batch=cap_value,
        )


__all__ = ["ToolPlanningPromptBuilder"]
