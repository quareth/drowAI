"""Compatibility facade for terminal session manager imports."""

from runtime_shared.terminal_manager_port import set_terminal_session_manager_resolver

from .terminal.manager import TerminalSessionManager, terminal_session_manager
from .terminal.models import TerminalSession


def _resolve_terminal_session_manager():
    """Return the active terminal session manager for runtime-shared consumers."""
    return terminal_session_manager


set_terminal_session_manager_resolver(_resolve_terminal_session_manager)


__all__ = [
    "TerminalSession",
    "TerminalSessionManager",
    "terminal_session_manager",
]
