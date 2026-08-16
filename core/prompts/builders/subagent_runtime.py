"""Build versioned prompts for definition-configured subagent runtime turns.

This module owns prompt assembly for the generic subagent model/tool loop. It
combines definition metadata, bounded runtime context, scoped runbooks, and the
canonical native tool-call guidance without executing tools or importing backend
services.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder
from core.prompts.builders.shell_capability_profiles import (
    build_shell_capability_profiles,
)
from core.prompts.registry import PromptRegistry
from core.runbooks.models import RunbookStage
from core.runbooks.service import RunbookService


SUBAGENT_RUNTIME_PROMPT_FAMILY = "subagent_runtime"
SUBAGENT_RUNTIME_SYSTEM_PROMPT_ID = "subagent_runtime_system"
SUBAGENT_RUNTIME_USER_PROMPT_ID = "subagent_runtime_user"
_MAX_PROMPT_STRING_CHARACTERS = 1_200
_MAX_PROMPT_SEQUENCE_ITEMS = 12
_MAX_PROMPT_MAPPING_ITEMS = 40
_TRUNCATION_MARKER = "...[truncated]"


class SubagentRuntimePromptBuilder:
    """Build prompts for one definition-configured subagent runtime session."""

    def __init__(
        self,
        *,
        prompt_registry: PromptRegistry | None = None,
        tool_planning_builder: ToolPlanningPromptBuilder | None = None,
        runbook_service: RunbookService | None = None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._tool_planning_builder = tool_planning_builder or ToolPlanningPromptBuilder()
        self._runbook_service = runbook_service or RunbookService()

    def build_system_prompt(
        self,
        *,
        definition_id: str,
        display_name: str,
        role_prompt: str,
        definition_instructions: str,
        ownership_boundary: str,
        boundary_rules: Sequence[str],
        max_committed_tools_per_batch: int,
        callable_tool_ids: Sequence[str] = (),
    ) -> str:
        """Return the versioned system prompt for the subagent runtime."""

        prompt_version = self._prompt_registry.get_latest_version(
            SUBAGENT_RUNTIME_PROMPT_FAMILY
        )
        template = self._prompt_registry.get_template(
            SUBAGENT_RUNTIME_SYSTEM_PROMPT_ID,
            version=prompt_version,
        )
        shared_guidance = self._tool_planning_builder.build_native_tool_call_shared_guidance(
            max_committed_tools_per_batch=max_committed_tools_per_batch,
        )
        rendered = template.format(
            role_prompt=_normalize_prompt_text(role_prompt),
            definition_id=_normalize_prompt_text(definition_id),
            display_name=_normalize_prompt_text(display_name),
            definition_instructions=_normalize_prompt_text(definition_instructions),
            ownership_boundary=_normalize_prompt_text(ownership_boundary),
            boundary_rules=_to_prompt_bullets(boundary_rules),
            native_tool_guidance=shared_guidance,
        )
        profile_section = build_shell_capability_profiles(
            callable_tool_ids,
            prompt_registry=self._prompt_registry,
        )
        if profile_section:
            rendered = f"{rendered.rstrip()}\n\n{profile_section}\n"
        return _ensure_trailing_newline(rendered)

    def build_user_prompt(
        self,
        *,
        display_name: str,
        assignment: Mapping[str, Any],
        tool_ids: Sequence[str],
        working_memory: Mapping[str, Any] | None = None,
        previous_tool_summary: Mapping[str, Any] | None = None,
        prior_tool_outcomes: Sequence[Mapping[str, Any]] = (),
        remaining_limits: Mapping[str, Any] | None = None,
    ) -> str:
        """Return existing bounded context plus compact cross-phase outcomes."""

        objective = str(assignment.get("objective") or "").strip()
        targets = list(assignment.get("targets") or [])
        scope_summary = assignment.get("scope_summary")
        prompt_version = self._prompt_registry.get_latest_version(
            SUBAGENT_RUNTIME_PROMPT_FAMILY
        )
        template = self._prompt_registry.get_template(
            SUBAGENT_RUNTIME_USER_PROMPT_ID,
            version=prompt_version,
        )
        rendered = template.format(
            display_name=_normalize_prompt_text(display_name),
            objective=objective,
            targets_json=_to_prompt_json(targets),
            explicit_constraints_json=_to_prompt_json(
                [scope_summary] if scope_summary else []
            ),
            tool_ids_json=_to_prompt_json(list(tool_ids)),
            tool_runbooks_section=_build_tool_runbooks_section(
                self._runbook_service,
                tool_ids,
            ),
            remaining_limits_json=_to_prompt_json(remaining_limits or {}),
            previous_tool_summary_json=_to_prompt_json(previous_tool_summary or {}),
            working_memory_json=_to_prompt_json(working_memory or {}),
            assignment_json=_to_prompt_json(assignment),
            prior_tool_outcomes_json=_to_prompt_json(list(prior_tool_outcomes)),
        )
        return _ensure_trailing_newline(rendered)


def _build_tool_runbooks_section(
    runbook_service: RunbookService,
    tool_ids: Sequence[str],
) -> str:
    """Return scoped tool runbook text for the subagent's candidate tools."""

    tool_runbooks = runbook_service.render_for_tools(
        selected_tools=list(tool_ids),
        stage=RunbookStage.TOOL_PARAMETERS,
    )
    return f"\nTool Runbooks:\n{tool_runbooks}\n" if tool_runbooks else ""


def _bounded_jsonable(value: Any, *, preserve_sequence: bool = False) -> Any:
    """Return prompt-safe JSON data with bounded prior-observation size."""

    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_PROMPT_MAPPING_ITEMS]
        preserve_session_results = value.get("execution_session_aggregate") is True
        return {
            str(item_key): _bounded_jsonable(
                item_value,
                preserve_sequence=(
                    preserve_session_results and str(item_key) == "results"
                ),
            )
            for item_key, item_value in items
        }
    if isinstance(value, tuple | list):
        items = list(value)
        if not preserve_sequence:
            items = items[:_MAX_PROMPT_SEQUENCE_ITEMS]
        return [
            _bounded_jsonable(item)
            for item in items
        ]
    if isinstance(value, frozenset | set):
        return [
            _bounded_jsonable(item)
            for item in sorted(value, key=lambda item: str(item))[
                :_MAX_PROMPT_SEQUENCE_ITEMS
            ]
        ]
    if isinstance(value, str) and len(value) > _MAX_PROMPT_STRING_CHARACTERS:
        return f"{value[:_MAX_PROMPT_STRING_CHARACTERS]}{_TRUNCATION_MARKER}"
    return value


def _to_prompt_json(value: Any) -> str:
    """Serialize prompt context deterministically without provider objects."""

    return json.dumps(
        _bounded_jsonable(value),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _to_prompt_bullets(values: Sequence[str]) -> str:
    """Render definition-owned prompt bullets without changing their text."""

    return "\n".join(f"- {value}" for value in values)


def _normalize_prompt_text(value: Any) -> str:
    """Normalize prompt slots to non-empty strings where possible."""

    return str(value or "").strip()


def _ensure_trailing_newline(text: str) -> str:
    """Keep prompt golden files stable with one trailing newline."""

    return text if text.endswith("\n") else f"{text}\n"


__all__ = [
    "SUBAGENT_RUNTIME_PROMPT_FAMILY",
    "SUBAGENT_RUNTIME_SYSTEM_PROMPT_ID",
    "SUBAGENT_RUNTIME_USER_PROMPT_ID",
    "SubagentRuntimePromptBuilder",
]
