"""Compose Scout prompts from canonical native tool-builder guidance.

Scout already receives its complete bounded recon tool profile, so this builder
does not perform candidate selection. It reuses the selector-independent
sections of the canonical tool-parameter builder and adds only Scout's bounded
assignment context plus the batch-strategy metadata contract.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder
from core.runbooks.models import RunbookStage
from core.runbooks.service import RunbookService


_RUNBOOK_SERVICE = RunbookService()


class ScoutToolBuilderPromptBuilder:
    """Build one native-call request over all Scout-visible recon tools."""

    def build_system_prompt(
        self,
        *,
        max_committed_tools_per_batch: int,
    ) -> str:
        """Return canonical shared guidance plus Scout scheduling metadata."""

        shared_guidance = (
            ToolPlanningPromptBuilder().build_native_tool_call_shared_guidance(
                max_committed_tools_per_batch=max_committed_tools_per_batch,
            )
        )
        return f"""You are Pathfinder, a bounded recon subagent.
Emit native tool calls only.

{shared_guidance}

Pathfinder batch strategy metadata (`_execution_strategy`):
- Every native tool call must include `_execution_strategy` as either "parallel" or "sequential".
- `_execution_strategy` is scheduling metadata, not a tool parameter.
- Use the same `_execution_strategy` value on every call in the batch.
- Use "sequential" when committing one call.
- For multiple calls, choose the strategy using the exact execution-strategy guidance above.

Pathfinder boundaries:
- Use only the targets, objective, scope, and constraints in the assignment context.
- Do not exploit, authenticate, mutate files, run shells, manage agents, or request credentials.
"""

    def build_user_prompt(
        self,
        *,
        assignment: Mapping[str, Any],
        tool_ids: Sequence[str],
        working_memory: Mapping[str, Any] | None = None,
        previous_tool_summary: Mapping[str, Any] | None = None,
    ) -> str:
        """Return bounded assignment context for the canonical builder rules."""

        objective = str(assignment.get("objective") or "").strip()
        targets = list(assignment.get("targets") or [])
        scope_summary = assignment.get("scope_summary")
        tool_runbooks = _RUNBOOK_SERVICE.render_for_tools(
            selected_tools=list(tool_ids),
            stage=RunbookStage.TOOL_PARAMETERS,
        )
        runbooks_section = (
            f"\nTool Runbooks:\n{tool_runbooks}\n" if tool_runbooks else ""
        )
        return f"""Current Turn Input:

Turn Execution Brief:
- Overall goal: {objective}
- Next operational goal: {objective}
- Success condition: {objective}
- Targets: {_to_prompt_json(targets)}
- Explicit constraints: {_to_prompt_json([scope_summary] if scope_summary else [])}

Candidate Tools (complete Pathfinder profile; no selection step):
{_to_prompt_json(list(tool_ids))}
{runbooks_section}

Assignment:
{_to_prompt_json(assignment)}

Previous Tool Executed:
{_to_prompt_json(previous_tool_summary or {})}

Working Memory Snapshot:
{_to_prompt_json(working_memory or {})}
"""


def _to_prompt_json(value: Any) -> str:
    """Serialize prompt context deterministically without provider objects."""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


__all__ = ["ScoutToolBuilderPromptBuilder"]
