"""Subagent-run stream event projection helpers.

This module owns the shared subagent event attribution shape. It builds
task-stream lifecycle packets and preserves only the additive agent identity
metadata that frontend replay/projection code needs.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from agent.graph.contracts.streaming_constants import (
    REASONING_PHASE_INDEX,
    STEP_REASONING_DELTA,
    STEP_REASONING_SECTION_END,
    STEP_REASONING_START,
)
from agent.graph.context.contracts import ActiveAgentRun, CompletedAgentResult
from agent.graph.emission.agent_run_attribution import resolve_agent_run_attribution
from agent.subagents.registry import SubagentDisplayMetadata
from backend.core.time_utils import format_iso, utc_now
from backend.services.agent_runs.contracts import (
    AgentRunLifecycleProjection,
    AgentResultProjection,
)
from backend.services.agent_runs.registry_contracts import LocalAgentRun
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.streaming.event_types import ensure_mutable_metadata


_PARENT_PROGRESS_SECTION = "parent_progress"


def build_agent_run_lifecycle_event(
    entry: LocalAgentRun,
    *,
    display_metadata: SubagentDisplayMetadata,
    parent_run_id: str | None,
) -> dict[str, Any]:
    """Return a task-stream status packet for one subagent lifecycle update."""

    result_projection = (
        None
        if entry.result is None
        else AgentResultProjection.from_result(
            entry.result,
            agent_display_name=display_metadata.display_name,
        )
    )
    display_name = display_metadata.display_name
    icon_key = display_metadata.icon
    projection = AgentRunLifecycleProjection(
        agent_run_id=entry.agent_run_id,
        agent_id=entry.agent_id,
        agent_kind=entry.agent_kind,
        agent_display_name=display_name,
        agent_icon_key=icon_key,
        status=entry.status,
        lifecycle_version=entry.lifecycle_version,
        task_id=entry.task_id,
        conversation_id=entry.conversation_id,
        parent_turn_id=entry.parent_turn_id,
        parent_run_id=parent_run_id,
        assignment=entry.assignment if entry.lifecycle_version == 1 else None,
        result=result_projection,
        safe_error=entry.safe_error,
    )
    metadata = attach_conversation_ids(
        {
            "subtype": "agent_run_lifecycle",
            "producer_type": "subagent",
            "agent_run_id": entry.agent_run_id,
            "agent_id": entry.agent_id,
            "agent_kind": entry.agent_kind,
            "agent_display_name": display_name,
            "agent_icon_key": icon_key,
            "parent_turn_id": entry.parent_turn_id,
            "parent_run_id": parent_run_id,
            "internal_only": False,
            "lifecycle_version": entry.lifecycle_version,
            "status": entry.status,
            "streaming": False,
        },
        entry.conversation_id,
    )
    metadata["turn_id"] = entry.parent_turn_id
    metadata["id"] = entry.parent_turn_id
    return {
        "type": "status",
        "content": "agent_run_lifecycle",
        "metadata": metadata,
        "agent_run": projection.model_dump(mode="json"),
        "timestamp": format_iso(utc_now()),
    }


def build_parent_handoff_progress_events(
    *,
    completed_results: tuple[CompletedAgentResult, ...],
    active_runs: tuple[ActiveAgentRun, ...],
    conversation_id: str,
    parent_turn_id: str,
    claim_id: str,
    action: str = "evaluating",
    turn_sequence: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return one parent-owned reasoning block for a processed handoff batch."""

    section_id = _parent_progress_section_id(
        parent_turn_id=parent_turn_id,
        completed_agent_run_ids=tuple(
            _string_value(result.get("agent_run_id")) for result in completed_results
        ),
        action=action,
    )
    timestamp = time.time()
    content = _parent_progress_content(
        completed_results=completed_results,
        active_runs=active_runs,
        action=action,
    )
    progress_payload = {
        "action": action,
        "claim_id": claim_id,
        "completed_assignment_count": len(completed_results),
        "completed_agent_run_ids": [
            _string_value(result.get("agent_run_id")) for result in completed_results
        ],
        "completed_agent_names": _display_names(completed_results),
        "active_assignment_count": len(active_runs),
        "active_agent_run_ids": [
            _string_value(run.get("agent_run_id")) for run in active_runs
        ],
        "active_agent_names": _display_names(active_runs),
    }

    base_metadata = attach_conversation_ids(
        {
            "producer_type": "main_agent",
            "progress_kind": "parent_handoff",
            "parent_turn_id": parent_turn_id,
            "id": parent_turn_id,
            "turn_id": parent_turn_id,
            "section_name": _PARENT_PROGRESS_SECTION,
            "reasoning_section_id": section_id,
            "ind": REASONING_PHASE_INDEX,
            "internal_only": False,
            "parent_progress": progress_payload,
            "timestamp": timestamp,
        },
        conversation_id,
    )
    if turn_sequence is not None:
        base_metadata["turn_sequence"] = turn_sequence

    start_metadata = {
        **base_metadata,
        "subtype": "reasoning_start",
        "step": _PARENT_PROGRESS_SECTION,
        "step_type": STEP_REASONING_START,
        "streaming": True,
    }
    delta_metadata = {
        **base_metadata,
        "subtype": "reasoning_delta",
        "step_type": STEP_REASONING_DELTA,
        "streaming": False,
    }
    end_metadata = {
        **base_metadata,
        "subtype": "reasoning_section_end",
        "step_type": STEP_REASONING_SECTION_END,
        "streaming": False,
    }

    return (
        {
            "type": "reasoning_start",
            "content": "",
            "metadata": start_metadata,
            "timestamp": format_iso(utc_now()),
        },
        {
            "type": "reasoning_delta",
            "content": content,
            "metadata": delta_metadata,
            "timestamp": format_iso(utc_now()),
        },
        {
            "type": "reasoning_section_end",
            "content": f"[Reasoning complete: {_PARENT_PROGRESS_SECTION}]",
            "metadata": end_metadata,
            "timestamp": format_iso(utc_now()),
        },
    )


def apply_agent_run_metadata(
    processed: dict[str, Any],
    raw_event: Mapping[str, Any],
) -> None:
    """Forward safe subagent attribution metadata from raw graph events."""

    raw_metadata = raw_event.get("metadata")
    attribution = resolve_agent_run_attribution(
        metadata=raw_metadata if isinstance(raw_metadata, Mapping) else None,
        config={"configurable": raw_event},
    )
    if not attribution:
        return

    metadata = ensure_mutable_metadata(processed)
    for key, value in attribution.items():
        if key == "agent_display_name":
            metadata[key] = value
        else:
            metadata.setdefault(key, value)


def _parent_progress_section_id(
    *,
    parent_turn_id: str,
    completed_agent_run_ids: tuple[str, ...],
    action: str,
) -> str:
    identity = "|".join(
        [
            parent_turn_id.strip(),
            action.strip().lower(),
            *sorted(run_id for run_id in completed_agent_run_ids if run_id),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{parent_turn_id}:parent-handoff:{digest}"


def _parent_progress_content(
    *,
    completed_results: tuple[CompletedAgentResult, ...],
    active_runs: tuple[ActiveAgentRun, ...],
    action: str,
) -> str:
    completed_count = len(completed_results)
    active_count = len(active_runs)
    completed_label = _assignment_label(completed_count)
    active_label = _assignment_label(active_count)
    completed_names = _names_phrase(_display_names(completed_results))
    active_names = _names_phrase(_display_names(active_runs))

    parts = [
        f"{completed_count} {completed_label} returned a handoff",
    ]
    if completed_names:
        parts[-1] += f": {completed_names}"
    parts[-1] += "."

    if active_count:
        active_sentence = f"{active_count} relevant {active_label} still active"
        if active_names:
            active_sentence += f": {active_names}"
        parts.append(f"{active_sentence}.")
    else:
        parts.append("No relevant assignments remain active.")

    action_text = action.strip().replace("_", " ") or "evaluating"
    parts.append(f"The parent is {action_text} the batch before the next step.")
    return " ".join(parts)


def _assignment_label(count: int) -> str:
    return "assignment" if count == 1 else "assignments"


def _display_names(
    items: tuple[CompletedAgentResult, ...] | tuple[ActiveAgentRun, ...],
) -> list[str]:
    names: list[str] = []
    for item in items:
        name = _string_value(item.get("agent_display_name")) or _string_value(
            item.get("agent_id")
        )
        if name and name not in names:
            names.append(name)
    return names


def _names_phrase(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])}, and {len(names) - 3} more"


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "apply_agent_run_metadata",
    "build_agent_run_lifecycle_event",
    "build_parent_handoff_progress_events",
]
