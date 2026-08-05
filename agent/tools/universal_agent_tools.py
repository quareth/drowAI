"""Universal agent tool identifiers shared by catalog and profile composition.

This module is intentionally data-only. Registration, catalog visibility, and
subagent profile policy remain owned by their dedicated modules.
"""

from __future__ import annotations

UNIVERSAL_AGENT_TOOL_IDS: tuple[str, ...] = (
    "shell.exec",
    "shell.write_stdin",
)

__all__ = ["UNIVERSAL_AGENT_TOOL_IDS"]
