"""Scout graph completion node.

This module turns the Scout child graph's terminal state into the safe
``AgentResult`` contract used by the process-local agent-run registry. It
does not read raw tool output or live runtime handles.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.subagents.contracts import AgentResult, AgentResultProjection
from agent.subagents.scout.nodes.choose_action import SCOUT_RESULT_METADATA_KEY
from agent.subagents.scout.state import ScoutRuntimeState, scout_state_from_graph_state


SCOUT_COMPLETION_METADATA_KEY = "scout_completion"
SCOUT_RESULT_PROJECTION_METADATA_KEY = "scout_result_projection"


class ScoutCompletionError(ValueError):
    """Raised when Scout cannot produce a valid terminal result."""


def complete_scout_result(
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate or derive Scout's terminal ``AgentResult`` and finalize state."""

    _ = (context, config)
    interactive = InteractiveState.from_mapping(state)
    scout = scout_state_from_graph_state(interactive)
    result = _resolve_result(interactive, scout)
    _validate_result_identity(result, scout)
    projection = AgentResultProjection.from_result(result)

    metadata = interactive.facts.ensure_metadata()
    metadata[SCOUT_RESULT_METADATA_KEY] = result.model_dump(mode="json")
    metadata[SCOUT_RESULT_PROJECTION_METADATA_KEY] = projection.model_dump(mode="json")
    metadata[SCOUT_COMPLETION_METADATA_KEY] = {
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
            "type": "scout_result",
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
    scout: ScoutRuntimeState,
) -> AgentResult:
    metadata = interactive.facts.ensure_metadata()
    explicit = metadata.get(SCOUT_RESULT_METADATA_KEY)
    if isinstance(explicit, Mapping):
        result = AgentResult.model_validate(dict(explicit))
        final_text = _clean_string(interactive.trace.final_text)
        return result.model_copy(update={"summary": final_text}) if final_text else result

    return AgentResult(
        agent_run_id=scout.agent_run_id,
        agent_id=scout.agent_id,
        agent_kind=scout.agent_kind,
        outcome=_derived_outcome(metadata),
        summary=_derived_summary(interactive),
        key_findings=tuple(_derived_key_findings(interactive)),
        evidence_refs=tuple(_derived_evidence_refs(interactive)),
        tools_used=tuple(_derived_tools_used(interactive)),
        limitations=tuple(_derived_limitations(metadata)),
        recommended_next_steps=tuple(_derived_next_steps(metadata)),
        final_checkpoint_id=_checkpoint_id_from_metadata(metadata),
    )


def _validate_result_identity(result: AgentResult, scout: ScoutRuntimeState) -> None:
    if result.agent_run_id != scout.agent_run_id:
        raise ScoutCompletionError(
            "Scout result agent_run_id does not match assignment metadata"
        )
    if result.agent_id != scout.agent_id:
        raise ScoutCompletionError(
            "Scout result agent_id does not match assignment metadata"
        )
    if result.agent_kind != scout.agent_kind:
        raise ScoutCompletionError(
            "Scout result agent_kind does not match assignment metadata"
        )


def _derived_outcome(metadata: Mapping[str, Any]) -> str:
    router_outcome = metadata.get("router_outcome")
    if isinstance(router_outcome, Mapping):
        reason = str(router_outcome.get("reason") or "").lower()
        if "budget" in reason or "stuck" in reason:
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
    raise ScoutCompletionError("Scout completion requires a non-empty summary")


def _derived_key_findings(interactive: InteractiveState) -> list[str]:
    synthesized = _mapping(interactive.facts.safe_metadata.get("synthesized_output"))
    compact = _mapping(interactive.facts.last_tool_result_compact)
    return _string_list(synthesized.get("key_findings")) or _string_list(
        compact.get("key_findings")
    )


def _derived_evidence_refs(interactive: InteractiveState) -> list[dict[str, str]]:
    compact = _mapping(interactive.facts.last_tool_result_compact)
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
    limitations = _string_list(metadata.get("limitations"))
    limitations.extend(_string_list(metadata.get("tool_gaps")))
    return _dedupe(limitations)


def _derived_next_steps(metadata: Mapping[str, Any]) -> list[str]:
    synthesized = _mapping(metadata.get("synthesized_output"))
    compact = _mapping(metadata.get("last_tool_result_compact"))
    return _string_list(synthesized.get("next_actions")) or _string_list(
        compact.get("report_recommendations")
    )


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    "SCOUT_COMPLETION_METADATA_KEY",
    "SCOUT_RESULT_PROJECTION_METADATA_KEY",
    "ScoutCompletionError",
    "complete_scout_result",
]
