"""Safe agent-run attribution helpers for graph stream events.

This module keeps subagent child-run identity stamping separate from event-family
payload construction. It only forwards the additive, non-secret metadata fields
that task-stream processors are allowed to preserve.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

AGENT_RUN_ATTRIBUTION_KEYS: tuple[str, ...] = (
    "producer_type",
    "agent_run_id",
    "agent_id",
    "agent_kind",
    "agent_display_name",
    "agent_icon_key",
    "parent_turn_id",
    "parent_run_id",
    "internal_only",
    "lifecycle_version",
)


def resolve_agent_run_attribution(
    *,
    state: Any = None,
    metadata: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return safe subagent attribution fields derived from graph state/config."""

    source: dict[str, Any] = {}
    state_metadata = _state_metadata(state)
    if state_metadata:
        source.update(state_metadata)
    if metadata:
        source.update(dict(metadata))
    configurable = _configurable(config)
    if configurable:
        source.update(configurable)

    if source.get("agent_run_id") and source.get("producer_type") is None:
        source["producer_type"] = "subagent"
    if source.get("agent_display_name") is None:
        source["agent_display_name"] = _format_agent_kind(source.get("agent_kind"))

    attribution: dict[str, Any] = {}
    for key in AGENT_RUN_ATTRIBUTION_KEYS:
        value = _safe_attribution_value(key, source.get(key))
        if value is not None:
            attribution[key] = value
    return attribution


def _state_metadata(state: Any) -> Mapping[str, Any]:
    facts = getattr(state, "facts", None)
    if facts is None:
        return {}
    safe_metadata = getattr(facts, "safe_metadata", None)
    if isinstance(safe_metadata, Mapping):
        return safe_metadata
    raw_metadata = getattr(facts, "metadata", None)
    return raw_metadata if isinstance(raw_metadata, Mapping) else {}


def _configurable(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    candidate = config.get("configurable")
    return candidate if isinstance(candidate, Mapping) else {}


def _safe_attribution_value(key: str, value: Any) -> str | int | bool | None:
    if key == "internal_only":
        return value if isinstance(value, bool) else None
    if key == "lifecycle_version":
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) and value > 0 else None
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


def _format_agent_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = [part for part in value.strip().replace("-", "_").split("_") if part]
    if not parts:
        return None
    return " ".join(f"{part[:1].upper()}{part[1:]}" for part in parts)


__all__ = [
    "AGENT_RUN_ATTRIBUTION_KEYS",
    "resolve_agent_run_attribution",
]
