"""Shared shell capability identifiers and alias normalization.

This module owns only serializable shell capability data used across the agent
and backend boundaries. It does not import registries, prompts, persistence, or
runtime-provider implementations.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping


class ShellCapability(str, Enum):
    """Declared purpose of a model-started shell session."""

    UTILITY = "utility"
    ASSESSMENT = "assessment"


SHELL_UTILITY_TOOL_ID: Final = "shell.utility"
SHELL_ASSESSMENT_TOOL_ID: Final = "shell.assessment"
SHELL_EXEC_TOOL_ID: Final = "shell.exec"
SHELL_WRITE_STDIN_TOOL_ID: Final = "shell.write_stdin"

SHELL_START_CAPABILITY_BY_TOOL_ID: Mapping[str, ShellCapability] = MappingProxyType(
    {
        SHELL_UTILITY_TOOL_ID: ShellCapability.UTILITY,
        SHELL_ASSESSMENT_TOOL_ID: ShellCapability.ASSESSMENT,
    }
)
SHELL_SESSION_START_CAPABILITY_BY_TOOL_ID: Mapping[
    str, ShellCapability
] = MappingProxyType(
    {
        **SHELL_START_CAPABILITY_BY_TOOL_ID,
        SHELL_EXEC_TOOL_ID: ShellCapability.ASSESSMENT,
    }
)
MODEL_FACING_SHELL_START_TOOL_IDS: frozenset[str] = frozenset(
    SHELL_START_CAPABILITY_BY_TOOL_ID
)
SHELL_SESSION_START_TOOL_IDS: frozenset[str] = frozenset(
    {*MODEL_FACING_SHELL_START_TOOL_IDS, SHELL_EXEC_TOOL_ID}
)
SHELL_SESSION_TOOL_IDS: frozenset[str] = frozenset(
    {*SHELL_SESSION_START_TOOL_IDS, SHELL_WRITE_STDIN_TOOL_ID}
)


def resolve_shell_start_capability(tool_id: object) -> ShellCapability | None:
    """Return the declared capability for a model-facing start alias."""

    return SHELL_START_CAPABILITY_BY_TOOL_ID.get(str(tool_id or "").strip())


def resolve_shell_session_start_capability(
    tool_id: object,
) -> ShellCapability | None:
    """Return capability provenance for any supported shell-session start id."""

    return SHELL_SESSION_START_CAPABILITY_BY_TOOL_ID.get(
        str(tool_id or "").strip()
    )


def canonical_shell_implementation_tool_id(tool_id: object) -> str:
    """Map a model-facing start alias to the existing implementation id."""

    normalized = str(tool_id or "").strip()
    if normalized in MODEL_FACING_SHELL_START_TOOL_IDS:
        return SHELL_EXEC_TOOL_ID
    return normalized


def normalize_shell_capability(value: object) -> ShellCapability | None:
    """Return a recognized shared runtime capability without reclassification."""

    if isinstance(value, ShellCapability):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return ShellCapability(normalized)
    except ValueError:
        return None


__all__ = [
    "MODEL_FACING_SHELL_START_TOOL_IDS",
    "SHELL_ASSESSMENT_TOOL_ID",
    "SHELL_EXEC_TOOL_ID",
    "SHELL_SESSION_START_TOOL_IDS",
    "SHELL_SESSION_START_CAPABILITY_BY_TOOL_ID",
    "SHELL_SESSION_TOOL_IDS",
    "SHELL_START_CAPABILITY_BY_TOOL_ID",
    "SHELL_UTILITY_TOOL_ID",
    "SHELL_WRITE_STDIN_TOOL_ID",
    "ShellCapability",
    "canonical_shell_implementation_tool_id",
    "normalize_shell_capability",
    "resolve_shell_start_capability",
    "resolve_shell_session_start_capability",
]
