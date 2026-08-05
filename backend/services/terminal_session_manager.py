"""Compatibility facade for terminal session manager imports."""

from runtime_shared.shell_session_port import set_shell_session_service_resolver
from runtime_shared.terminal_manager_port import set_terminal_session_manager_resolver

from .terminal.manager import TerminalSessionManager, terminal_session_manager
from .terminal.models import TerminalSession
from .terminal.shell_session_service import ShellSessionService


def _resolve_terminal_session_manager():
    """Return the active terminal session manager for runtime-shared consumers."""
    return terminal_session_manager


shell_session_service = ShellSessionService(terminal_manager=terminal_session_manager)


def _resolve_shell_session_service():
    """Return the active shell-session service for runtime-shared consumers."""
    return shell_session_service


set_terminal_session_manager_resolver(_resolve_terminal_session_manager)
set_shell_session_service_resolver(_resolve_shell_session_service)


__all__ = [
    "shell_session_service",
    "TerminalSession",
    "TerminalSessionManager",
    "terminal_session_manager",
]
