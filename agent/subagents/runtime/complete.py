"""Definition-configured subagent graph completion node.

Purpose
-------
Project a definition-configured child graph terminal state into the safe
``AgentResult`` contract used by agent-run lifecycle and parent handoff code.

Responsibility boundary
-----------------------
This module derives terminal result metadata only. It does not read raw tool
output, access live runtime handles, depend on control-plane services, or
dispatch follow-up work.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.subagents.contracts import AgentResult, AgentResultProjection
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.model import (
    SUBAGENT_FORCED_FINAL_METADATA_KEY,
    SUBAGENT_RESULT_METADATA_KEY,
)
from agent.subagents.runtime.state import (
    SubagentRuntimeState,
    subagent_state_from_graph_state,
)


SUBAGENT_COMPLETION_METADATA_KEY = "subagent_completion"
SUBAGENT_RESULT_PROJECTION_METADATA_KEY = "subagent_result_projection"


class SubagentCompletionError(ValueError):
    """Raised when a subagent cannot produce a valid terminal result."""


def complete_subagent_result(
    definition: SubagentDefinition,
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate or derive a subagent terminal ``AgentResult`` and finalize state."""

    _ = (context, config)
    interactive = InteractiveState.from_mapping(state)
    subagent = subagent_state_from_graph_state(interactive, definition=definition)
    result = _resolve_result(interactive, subagent)
    _validate_result_identity(result, subagent)
    projection = AgentResultProjection.from_result(
        result,
        agent_display_name=definition.display_name,
    )

    metadata = interactive.facts.ensure_metadata()
    metadata[SUBAGENT_RESULT_METADATA_KEY] = result.model_dump(mode="json")
    metadata[SUBAGENT_RESULT_PROJECTION_METADATA_KEY] = projection.model_dump(
        mode="json"
    )
    metadata[SUBAGENT_COMPLETION_METADATA_KEY] = {
        "agent_run_id": result.agent_run_id,
        "agent_id": result.agent_id,
        "agent_kind": result.agent_kind,
        "outcome": result.outcome,
    }
    _clear_pending_tool_plan(metadata)
    interactive.facts.selected_tool = None
    interactive.facts.tool_parameters = {}
    interactive.trace.final_text = result.summary
    interactive.trace.history.append(
        {
            "type": "subagent_result",
            "agent_run_id": result.agent_run_id,
            "agent_id": result.agent_id,
            "agent_kind": result.agent_kind,
            "outcome": result.outcome,
            "summary": result.summary,
        }
    )
    return interactive.as_graph_update()


def _resolve_result(
    interactive: InteractiveState,
    subagent: SubagentRuntimeState,
) -> AgentResult:
    metadata = interactive.facts.ensure_metadata()
    explicit = metadata.get(SUBAGENT_RESULT_METADATA_KEY)
    if isinstance(explicit, Mapping):
        result = AgentResult.model_validate(dict(explicit))
        final_text = _clean_string(interactive.trace.final_text)
        return result.model_copy(update={"summary": final_text}) if final_text else result

    return AgentResult(
        agent_run_id=subagent.agent_run_id,
        agent_id=subagent.agent_id,
        agent_kind=subagent.agent_kind,
        outcome=_derived_outcome(metadata),
        summary=_derived_summary(interactive),
        key_findings=tuple(_derived_key_findings(interactive)),
        evidence_refs=tuple(_derived_evidence_refs(interactive)),
        tools_used=tuple(_derived_tools_used(interactive)),
        limitations=tuple(_derived_limitations(metadata)),
        recommended_next_steps=tuple(_derived_next_steps(metadata)),
        final_checkpoint_id=_checkpoint_id_from_metadata(metadata),
    )


def _validate_result_identity(
    result: AgentResult,
    subagent: SubagentRuntimeState,
) -> None:
    if result.agent_run_id != subagent.agent_run_id:
        raise SubagentCompletionError(
            "Subagent result agent_run_id does not match assignment metadata"
        )
    if result.agent_id != subagent.agent_id:
        raise SubagentCompletionError(
            "Subagent result agent_id does not match assignment metadata"
        )
    if result.agent_kind != subagent.agent_kind:
        raise SubagentCompletionError(
            "Subagent result agent_kind does not match assignment metadata"
        )


def _derived_outcome(metadata: Mapping[str, Any]) -> str:
    if metadata.get(SUBAGENT_FORCED_FINAL_METADATA_KEY) is True:
        return "partial"
    if _compact_reports_failure(metadata):
        return "partial"
    if metadata.get("user_goal_achieved") is False:
        return "partial"
    return "completed"


def _derived_summary(interactive: InteractiveState) -> str:
    synthesized = _mapping(interactive.facts.safe_metadata.get("synthesized_output"))
    compact = _mapping(interactive.facts.last_tool_result_compact)
    for candidate in (
        interactive.trace.final_text,
        synthesized.get("summary"),
        compact.get("summary"),
        interactive.facts.message,
    ):
        text = _clean_string(candidate)
        if text:
            return text
    raise SubagentCompletionError("Subagent completion requires a non-empty summary")


def _derived_key_findings(interactive: InteractiveState) -> list[str]:
    metadata = interactive.facts.safe_metadata
    synthesized = _mapping(metadata.get("synthesized_output"))
    compact = _mapping(interactive.facts.last_tool_result_compact)
    findings = _working_memory_tool_findings(metadata)
    findings.extend(_working_memory_available_findings(metadata))
    findings.extend(_string_list(synthesized.get("key_findings")))
    findings.extend(_string_list(compact.get("key_findings")))
    return _dedupe(findings)


def _derived_evidence_refs(interactive: InteractiveState) -> list[dict[str, str]]:
    metadata = interactive.facts.safe_metadata
    compact = _mapping(interactive.facts.last_tool_result_compact) or _mapping(
        metadata.get("last_tool_result_compact")
    )
    batch = _mapping(metadata.get("last_tool_result_compact_batch"))
    refs = _artifact_refs_from_compact(compact)
    for row in _list_items(batch.get("results")):
        refs.extend(
            _artifact_refs_from_compact(_mapping(row.get("compact_tool_result")))
        )
    return _dedupe_evidence_refs(refs)


def _artifact_refs_from_compact(compact: Mapping[str, Any]) -> list[dict[str, str]]:
    refs = compact.get("artifact_refs")
    if not isinstance(refs, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in refs:
        if not isinstance(item, Mapping):
            continue
        ref: dict[str, str] = {}
        for key in ("artifact_id", "path", "label", "provenance_id"):
            value = _clean_string(item.get(key))
            if value:
                ref[key] = value
        if ref:
            normalized.append(ref)
    return normalized


def _dedupe_evidence_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for ref in refs:
        key = tuple(sorted(ref.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _derived_tools_used(interactive: InteractiveState) -> list[str]:
    tools: list[str] = []
    for record in interactive.trace.executed_tools or []:
        tool_id = _clean_string(getattr(record, "tool_id", None))
        if tool_id and tool_id not in tools:
            tools.append(tool_id)

    compact = _mapping(interactive.facts.last_tool_result_compact)
    compact_tool = _clean_string(compact.get("tool"))
    if compact_tool and compact_tool not in tools:
        tools.append(compact_tool)

    selected_tool = _clean_string(interactive.facts.selected_tool)
    if selected_tool and selected_tool not in tools:
        tools.append(selected_tool)
    return tools


def _derived_limitations(metadata: Mapping[str, Any]) -> list[str]:
    synthesized = _mapping(metadata.get("synthesized_output"))
    compact = _mapping(metadata.get("last_tool_result_compact"))
    batch = _mapping(metadata.get("last_tool_result_compact_batch"))
    limitations = _string_list(metadata.get("limitations"))
    limitations.extend(_string_list(metadata.get("model_limitations")))
    limitations.extend(_string_list(metadata.get("tool_gaps")))
    limitations.extend(_string_list(synthesized.get("limitations")))
    limitations.extend(_string_list(synthesized.get("tool_gaps")))
    limitations.extend(_string_list(compact.get("limitations")))
    limitations.extend(_string_list(compact.get("tool_gaps")))
    limitations.extend(_compact_error_limitations(compact))
    limitations.extend(_batch_error_limitations(batch))
    if metadata.get(SUBAGENT_FORCED_FINAL_METADATA_KEY) is True:
        limitations.append("Subagent tool or iteration budget was exhausted.")
    return _dedupe(limitations)


def _compact_error_limitations(compact: Mapping[str, Any]) -> list[str]:
    limitations = _string_list(compact.get("errors"))
    status = _clean_string(compact.get("status")).lower()
    if compact.get("success") is False or status in {"failed", "error", "partial"}:
        if status:
            limitations.append(f"Latest compact tool result status was {status}.")
        elif not limitations:
            limitations.append("Latest compact tool result was unsuccessful.")
    return limitations


def _compact_reports_failure(metadata: Mapping[str, Any]) -> bool:
    compact = _mapping(metadata.get("last_tool_result_compact"))
    status = _clean_string(compact.get("status")).lower()
    if compact.get("success") is False or status in {"failed", "error"}:
        return True
    batch = _mapping(metadata.get("last_tool_result_compact_batch"))
    batch_status = _clean_string(batch.get("status")).lower()
    if batch.get("success") is False or batch_status in {
        "completed_with_errors",
        "failed",
        "denied",
        "cancelled",
        "error",
    }:
        return True
    return any(
        row.get("success") is False
        or _clean_string(row.get("status")).lower()
        in {"failed", "denied", "cancelled", "error"}
        for row in _list_items(batch.get("results"))
    )


def _batch_error_limitations(batch: Mapping[str, Any]) -> list[str]:
    limitations: list[str] = []
    status = _clean_string(batch.get("status")).lower()
    if batch.get("success") is False or status in {
        "completed_with_errors",
        "failed",
        "denied",
        "cancelled",
        "error",
    }:
        if status:
            limitations.append(f"Latest compact tool batch status was {status}.")
        else:
            limitations.append("Latest compact tool batch was unsuccessful.")

    for row in _list_items(batch.get("results")):
        row_status = _clean_string(row.get("status")).lower()
        if row.get("success") is not False and row_status not in {
            "failed",
            "denied",
            "cancelled",
            "error",
        }:
            continue
        limitations.append(_batch_row_limitation(row, row_status=row_status))
    return limitations


def _batch_row_limitation(
    row: Mapping[str, Any],
    *,
    row_status: str,
) -> str:
    tool_id = _clean_string(row.get("tool_id")) or "unknown tool"
    call_id = _clean_string(row.get("tool_call_id"))
    detail = _clean_string(row.get("error_message"))
    if not detail:
        detail = _clean_string(row.get("failure_category"))
    if not detail:
        compact = _mapping(row.get("compact_tool_result"))
        detail = "; ".join(_string_list(compact.get("errors")))
    status = row_status or "unsuccessful"
    label = f"{tool_id} ({call_id})" if call_id else tool_id
    if detail:
        return f"Tool call {label} reported {status}: {detail}"
    return f"Tool call {label} reported {status}."


def _derived_next_steps(metadata: Mapping[str, Any]) -> list[str]:
    synthesized = _mapping(metadata.get("synthesized_output"))
    compact = _mapping(metadata.get("last_tool_result_compact"))
    next_steps = _string_list(synthesized.get("recommended_next_steps"))
    next_steps.extend(_string_list(synthesized.get("next_actions")))
    next_steps.extend(_string_list(compact.get("recommended_next_steps")))
    next_steps.extend(_string_list(compact.get("next_actions")))
    next_steps.extend(_string_list(compact.get("report_recommendations")))
    return _dedupe(next_steps)


def _checkpoint_id_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    for key in ("checkpoint_id", "final_checkpoint_id"):
        value = _clean_string(metadata.get(key))
        if value:
            return value
    return None


def _clear_pending_tool_plan(metadata: dict[str, Any]) -> None:
    for key in (
        "planner_plan",
        "tool_plan_prepared",
        "planned_execution_strategy",
    ):
        metadata.pop(key, None)


def _working_memory_tool_findings(metadata: Mapping[str, Any]) -> list[str]:
    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return []
    findings: list[str] = []
    for item in _list_items(working_memory.get("tool_runs")):
        findings.extend(_string_list(item.get("key_findings")))
    return _dedupe(findings)


def _working_memory_available_findings(metadata: Mapping[str, Any]) -> list[str]:
    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return []
    findings: list[str] = []
    for item in _list_items(working_memory.get("available_findings")):
        if text := _available_finding_summary(item):
            findings.append(text)
    return _dedupe(findings)


def _available_finding_summary(item: Mapping[str, Any]) -> str:
    details = _mapping(item.get("details"))
    for key in ("summary", "description", "observation"):
        value = _clean_string(details.get(key))
        if value:
            return value

    kind = _clean_string(item.get("kind"))
    subject = _clean_string(item.get("subject")) or _clean_string(item.get("target"))
    if kind and subject:
        return f"{kind}: {subject}"
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return _dedupe([text for item in value if (text := _clean_string(item))])


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


__all__ = [
    "SUBAGENT_COMPLETION_METADATA_KEY",
    "SUBAGENT_RESULT_PROJECTION_METADATA_KEY",
    "SubagentCompletionError",
    "complete_subagent_result",
]
