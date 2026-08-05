"""Runtime-safe terminal manager resolver port.

This module exposes a backend-free resolver contract for retrieving the active
terminal session manager. Runtime-image modules depend on this port instead of
importing backend terminal modules directly.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from runtime_shared.terminal_contracts import TerminalReadResult


class TerminalSessionManagerPort(Protocol):
    """Protocol for PTY manager capabilities consumed by runtime modules."""

    async def prepare_agent_session(self, *args: Any, **kwargs: Any) -> Any: ...

    async def send_input(self, *args: Any, **kwargs: Any) -> bool: ...

    async def read_output(self, *args: Any, **kwargs: Any) -> bytes: ...

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> TerminalReadResult: ...

    async def close_session(self, *args: Any, **kwargs: Any) -> bool: ...


_terminal_manager_resolver: Callable[[], TerminalSessionManagerPort] | None = None


def set_terminal_session_manager_resolver(
    resolver: Callable[[], TerminalSessionManagerPort],
) -> None:
    """Register the process-local resolver for the terminal session manager."""
    global _terminal_manager_resolver
    _terminal_manager_resolver = resolver


def get_terminal_session_manager() -> TerminalSessionManagerPort:
    """Return the configured terminal session manager instance."""
    if _terminal_manager_resolver is None:
        raise RuntimeError(
            "Terminal session manager resolver is not configured. "
            "Management runtime must register it before PTY operations."
        )
    return _terminal_manager_resolver()
