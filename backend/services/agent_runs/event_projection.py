"""Subagent-run stream event projection helpers.

This module owns the shared subagent event attribution shape. It builds
task-stream lifecycle packets and preserves only the additive agent identity
metadata that frontend replay/projection code needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.core.time_utils import format_iso, utc_now
from backend.services.agent_runs.contracts import (
    AgentRunLifecycleProjection,
    AgentResultProjection,
    agent_display_name,
)
from backend.services.agent_runs.registry import LocalAgentRun
from backend.services.chat.event_builders import attach_conversation_ids
from backend.services.langgraph_chat.streaming.event_types import ensure_mutable_metadata

AGENT_RUN_METADATA_KEYS: tuple[str, ...] = (
    "producer_type",
    "agent_run_id",
    "agent_id",
    "agent_kind",
    "agent_display_name",
    "parent_turn_id",
    "parent_run_id",
    "internal_only",
    "lifecycle_version",
)


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
    projection = AgentRunLifecycleProjection(
        agent_run_id=entry.agent_run_id,
        agent_id=entry.agent_id,
        agent_kind=entry.agent_kind,
        agent_display_name=display_name,
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

    source = _agent_metadata_source(raw_event)
    if not source:
        return

    metadata = ensure_mutable_metadata(processed)
    for key in AGENT_RUN_METADATA_KEYS:
        if key not in source:
            continue
        value = _safe_metadata_value(key, source[key])
        if value is not None:
            if key == "agent_display_name":
                metadata[key] = value
            else:
                metadata.setdefault(key, value)


def _agent_metadata_source(raw_event: Mapping[str, Any]) -> dict[str, Any]:
    source: dict[str, Any] = {}
    raw_metadata = raw_event.get("metadata")
    if isinstance(raw_metadata, Mapping):
        source.update(dict(raw_metadata))
    for key in AGENT_RUN_METADATA_KEYS:
        if key in raw_event:
            source[key] = raw_event[key]
    if source.get("agent_run_id") and source.get("producer_type") is None:
        source["producer_type"] = "subagent"
    display_name = registered_agent_display_name(source.get("agent_id"))
    if display_name is not None:
        source["agent_display_name"] = display_name
    elif source.get("agent_display_name") is None:
        source["agent_display_name"] = (
            _format_agent_kind(source.get("agent_kind"))
        )
    return source


def _safe_metadata_value(key: str, value: Any) -> str | int | bool | None:
    if key == "internal_only":
        return value if isinstance(value, bool) else None
    if key == "lifecycle_version":
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value > 0:
            return value
        return None
    if key == "producer_type":
        return "subagent" if value == "subagent" else None
    if key == "agent_kind":
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def registered_agent_display_name(value: Any) -> str | None:
    """Resolve display metadata from the shared definition-owned registry."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        return agent_display_name(normalized)
    except KeyError:
        return None


def _format_agent_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = [part for part in value.strip().replace("-", "_").split("_") if part]
    if not parts:
        return None
    return " ".join(f"{part[:1].upper()}{part[1:]}" for part in parts)


__all__ = [
    "AGENT_RUN_METADATA_KEYS",
    "apply_agent_run_metadata",
    "build_agent_run_lifecycle_event",
    "registered_agent_display_name",
]
