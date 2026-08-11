"""Shared terminal contracts.

Responsibilities:
- Re-export canonical PTY prompt markers used by backend and runtime code.
- Re-export canonical session-id builders from runtime-safe shared contracts.
- Own backend-only immutable terminal lifecycle projection boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellSessionIdentity,
    ShellSessionOrigin,
)
from runtime_shared.terminal_contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
    build_agent_session_id,
    build_named_agent_session_id,
)


@dataclass(frozen=True, slots=True)
class ShellSessionTerminalEvent:
    """Immutable fact for projecting an already-closed shell session."""

    identity: ShellSessionIdentity
    public_session_id: str
    originating_capability: ShellCapability
    origin: ShellSessionOrigin | None
    close_reason: str


class ShellSessionLifecycleProjectorPort(Protocol):
    """Application boundary for shell terminal lifecycle projection."""

    async def project_terminal_event(self, event: ShellSessionTerminalEvent) -> None: ...


__all__ = [
    "AGENT_PROMPT_ENV",
    "AGENT_PROMPT_MARKER",
    "ShellSessionLifecycleProjectorPort",
    "ShellSessionTerminalEvent",
    "build_agent_session_id",
    "build_named_agent_session_id",
]
