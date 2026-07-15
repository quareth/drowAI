"""Shared terminal contracts.

Responsibilities:
- Re-export canonical PTY prompt markers used by backend and runtime code.
- Re-export canonical session-id builders from runtime-safe shared contracts.
"""

from __future__ import annotations

from runtime_shared.terminal_contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
    build_agent_session_id,
    build_named_agent_session_id,
)

__all__ = [
    "AGENT_PROMPT_ENV",
    "AGENT_PROMPT_MARKER",
    "build_agent_session_id",
    "build_named_agent_session_id",
]
