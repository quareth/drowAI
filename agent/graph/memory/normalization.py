"""Shared normalization helpers for working-memory payloads.

This module centralizes pure normalization logic used by working-memory
contracts so orchestration modules stay smaller and focused on flow.
"""

from __future__ import annotations

from typing import Any, Callable, Collection, Mapping, Sequence


def _clean_text(value: Any, *, lowercase: bool = False) -> str:
    """Return stripped text; optionally lowercased for token fields."""
    text = str(value if value is not None else "").strip()
    return text.lower() if lowercase else text


def _optional_clean_text(value: Any) -> str | None:
    """Return stripped text when provided; otherwise null."""
    if value is None:
        return None
    return str(value).strip()


def normalize_stored_item(
    item: Mapping[str, Any] | Any,
    *,
    authority_order: Sequence[str],
    default_provenance: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Normalize arbitrary item payloads with required status/provenance fields."""
    payload = dict(item) if isinstance(item, Mapping) else {"value": item}
    payload["status"] = str(payload.get("status", "unknown"))

    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping):
        authority = provenance.get("authority", "derived")
        payload["provenance"] = {
            "authority": authority if authority in authority_order else "derived",
            "source": provenance.get("source", "unset"),
        }
    else:
        payload["provenance"] = default_provenance()
    return payload


def _normalize_todo_delta_entry(item: Any) -> dict[str, Any] | None:
    """Normalize a single advisory todo-delta item."""
    if not isinstance(item, Mapping):
        return None
    try:
        index = int(item.get("index"))
    except Exception:
        return None
    if index < 0:
        return None

    status = _clean_text(item.get("status"), lowercase=True)
    if not status:
        return None

    entry: dict[str, Any] = {"index": index, "status": status}
    completion_type = item.get("completion_type")
    if completion_type is not None:
        entry["completion_type"] = str(completion_type)
    completion_reason = item.get("completion_reason")
    if completion_reason is not None:
        entry["completion_reason"] = str(completion_reason)
    return entry


def normalize_todo_delta(value: Any) -> list[dict[str, Any]]:
    """Normalize advisory todo delta entries for active decision memory."""
    if not isinstance(value, list):
        return []
    normalized = [
        entry
        for entry in (_normalize_todo_delta_entry(item) for item in value)
        if entry is not None
    ]
    return normalized[:10]


def _normalize_tool_intent(value: Any) -> dict[str, Any] | None:
    """Normalize tool-intent payload; requires non-empty description."""
    if not isinstance(value, Mapping):
        return None
    description = _clean_text(value.get("description"))
    if not description:
        return None
    return {
        "description": description,
        "target": _optional_clean_text(value.get("target")),
        "focus": _optional_clean_text(value.get("focus")),
    }


def normalize_active_decision(value: Any, *, statuses: Collection[str]) -> dict[str, Any] | None:
    """Normalize advisory active-decision payload for pointer-first memory."""
    if not isinstance(value, Mapping):
        return None

    status_raw = _clean_text(value.get("status", "active"), lowercase=True)
    status = status_raw if status_raw in statuses else "active"

    next_action = _clean_text(value.get("next_action"), lowercase=True)
    if not next_action:
        return None

    payload = {
        "source": str(value.get("source", "post_tool_reasoning") or "post_tool_reasoning"),
        "authority": str(value.get("authority", "llm_proposal") or "llm_proposal"),
        "status": status,
        "next_action": next_action,
        "tool_intent": _normalize_tool_intent(value.get("tool_intent")),
        "effective_next_goal": _optional_clean_text(value.get("effective_next_goal")),
        "action_reasoning": _clean_text(value.get("action_reasoning")),
        "todo_delta": normalize_todo_delta(value.get("todo_delta")),
    }
    if value.get("status_reason") is not None:
        payload["status_reason"] = str(value.get("status_reason"))
    if value.get("iteration") is not None:
        try:
            payload["iteration"] = int(value.get("iteration"))
        except Exception:
            pass
    return payload


def normalize_objective(
    value: Any,
    *,
    authority_order: Sequence[str],
    default_provenance: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Normalize objective payload with explicit provenance defaults."""
    normalized = dict(value) if isinstance(value, Mapping) else {"text": "unknown"}
    normalized["status"] = str(normalized.get("status", "unknown"))
    source = str(normalized.get("source", "unset"))
    normalized["source"] = source

    provenance = normalized.get("provenance")
    if isinstance(provenance, Mapping):
        authority = provenance.get("authority", "derived")
        normalized["provenance"] = {
            "authority": authority if authority in authority_order else "derived",
            "source": provenance.get("source", source),
        }
    else:
        normalized["provenance"] = default_provenance(authority="derived", source=source)
    return normalized
