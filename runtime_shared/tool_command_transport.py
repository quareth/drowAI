"""Shared runner tool-command transport normalization helpers.

This module owns backend-free transport alias normalization for runner
tool-command dispatch so graph, provider, and runner code do not duplicate
string parsing rules.
"""

from __future__ import annotations

from typing import Any

TRANSPORT_FILE_COMM = "file-comm"
TRANSPORT_PTY = "pty"

_PTY_ALIASES = frozenset({"pty", "terminal"})
_FILE_COMM_ALIASES = frozenset({"file", "file-comm", "file_comm", "jsonl", "container"})


def normalize_tool_command_transport(value: Any) -> str | None:
    """Return the canonical runner tool-command transport for a user value."""
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in _PTY_ALIASES:
        return TRANSPORT_PTY
    if normalized in _FILE_COMM_ALIASES:
        return TRANSPORT_FILE_COMM
    return None


__all__ = [
    "TRANSPORT_FILE_COMM",
    "TRANSPORT_PTY",
    "normalize_tool_command_transport",
]
