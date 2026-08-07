"""Universal agent tool identifiers shared by catalog and profile composition.

This module is intentionally data-only. Registration, catalog visibility, and
subagent profile policy remain owned by their dedicated modules.
"""

from __future__ import annotations

from runtime_shared.shell_capabilities import (
    SHELL_ASSESSMENT_TOOL_ID,
    SHELL_UTILITY_TOOL_ID,
    SHELL_WRITE_STDIN_TOOL_ID,
)

UNIVERSAL_AGENT_TOOL_IDS: tuple[str, ...] = (
    SHELL_UTILITY_TOOL_ID,
    SHELL_ASSESSMENT_TOOL_ID,
    SHELL_WRITE_STDIN_TOOL_ID,
)

__all__ = ["UNIVERSAL_AGENT_TOOL_IDS"]
