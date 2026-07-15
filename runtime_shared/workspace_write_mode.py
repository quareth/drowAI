"""Shared normalization policy for runtime workspace write modes.

This module keeps backend and runner workspace-write handling aligned without
duplicating string parsing or append-scope rules across transport adapters.
"""

from __future__ import annotations

from typing import Any

WORKSPACE_WRITE_MODE_APPEND = "append"
WORKSPACE_WRITE_MODE_WRITE = "write"

_VALID_WRITE_MODES = frozenset(
    {WORKSPACE_WRITE_MODE_APPEND, WORKSPACE_WRITE_MODE_WRITE}
)


def normalize_workspace_write_mode(value: Any) -> str | None:
    """Return the canonical workspace write mode, or ``None`` when invalid."""
    if value is None:
        return WORKSPACE_WRITE_MODE_WRITE
    normalized = str(value).strip().lower().replace("_", "-")
    if not normalized:
        return WORKSPACE_WRITE_MODE_WRITE
    if normalized in _VALID_WRITE_MODES:
        return normalized
    return None


def workspace_path_allows_append(path: Any) -> bool:
    """Return whether append mode is allowed for a workspace-relative path."""
    normalized = str(path or "").strip().replace("\\", "/")
    return (
        bool(normalized)
        and normalized.startswith("index/")
        and ".." not in normalized.split("/")
    )


__all__ = [
    "WORKSPACE_WRITE_MODE_APPEND",
    "WORKSPACE_WRITE_MODE_WRITE",
    "normalize_workspace_write_mode",
    "workspace_path_allows_append",
]
