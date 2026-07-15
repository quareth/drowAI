"""Pure JSON/value helpers for the control channel.

Stateless functions only; no I/O and no protocol knowledge beyond shaping
dict/values. The single sibling dependency is ``constants`` for the terminal
stream capability marker.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.control_channel.constants import _TERMINAL_STREAM_CAPABILITY


def _merge_json_dicts(
    base: Mapping[str, object] | None,
    patch: Mapping[str, object] | None,
) -> dict[str, object]:
    merged: dict[str, object] = {}
    if isinstance(base, Mapping):
        for key, value in base.items():
            merged[str(key)] = value
    if not isinstance(patch, Mapping):
        return merged
    for key, value in patch.items():
        normalized_key = str(key)
        existing = merged.get(normalized_key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[normalized_key] = _merge_json_dicts(existing, value)
        else:
            merged[normalized_key] = value
    return merged


def _is_terminal_tool_command_response(response: Mapping[str, object]) -> bool:
    status = str(response.get("status") or "").strip().lower()
    metadata = response.get("metadata")
    terminal = bool(metadata.get("terminal")) if isinstance(metadata, Mapping) else False
    return terminal or status in {"completed", "succeeded", "failed", "timed_out", "cancelled", "canceled"}


def _coerce_positive_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _stream_capabilities(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    """Return runner capabilities with terminal streaming advertised once."""
    normalized = [str(item).strip() for item in capabilities if str(item).strip()]
    if _TERMINAL_STREAM_CAPABILITY not in normalized:
        normalized.append(_TERMINAL_STREAM_CAPABILITY)
    return tuple(normalized)
