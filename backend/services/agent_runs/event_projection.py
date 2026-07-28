"""Subagent-run stream event projection helpers.

This module owns the shared subagent event attribution shape. It builds
task-stream lifecycle packets and preserves only the additive agent identity
metadata that frontend replay/projection code needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.emission.agent_run_attribution import resolve_agent_run_attribution
from backend.core.time_utils import format_iso, utc_now
from backend.services.agent_runs.contracts import (
    AgentRunLifecycleProjection,
    AgentResultProjection,
    agent_display_name,
    agent_icon_key,
)
from backend.services.agent_runs.registry import LocalAgentRun
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.streaming.event_types import ensure_mutable_metadata


def build_agent_run_lifecycle_event(
    entry: LocalAgentRun,
    *,
    parent_run_id: str | None,
) -> dict[str, Any]:
    """Return a task-stream status packet for one subagent lifecycle update."""

    result_projection = (
        None
        if entry.result is None
        else AgentResultProjection.from_result(entry.result)
    )
    display_name = agent_display_name(entry.agent_id)
    icon_key = agent_icon_key(entry.agent_id)
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


__all__ = [
    "apply_agent_run_metadata",
    "build_agent_run_lifecycle_event",
]
