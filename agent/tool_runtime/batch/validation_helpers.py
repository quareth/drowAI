"""Shared validation helpers for tool-batch admission.

The builder commit path and runtime validator both need to reject unresolved
result placeholders in committed call parameters. Keeping the marker logic here
prevents the runtime admission authority from drifting from builder checks.
"""

from __future__ import annotations

from typing import Any, Mapping


_PLACEHOLDER_MARKERS = (
    "${",
    "{{",
    "<previous",
    "<prior",
    "<result",
    "<output",
)


def looks_like_placeholder(value: Any) -> bool:
    """Return True when a parameter value contains an unresolved placeholder."""
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
    if isinstance(value, Mapping):
        return any(looks_like_placeholder(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(looks_like_placeholder(item) for item in value)
    return False


__all__ = ["looks_like_placeholder"]
