"""Compose and expose the backend's production terminal session services.

This compatibility facade wires the terminal manager, shell lifecycle
projector, runtime context resolution, and runtime-shared resolver
registrations. Terminal mechanics and lifecycle projection behavior remain in
their dedicated service modules.
"""

import time

from backend.database import SessionLocal
from backend.services.chat.shell_session_lifecycle_projector import (
    ShellSessionLifecycleProjector,
)
from backend.services.runtime_provider import RuntimeActorType
from backend.services.runtime_provider.context import RuntimeProviderContextResolver
from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
from runtime_shared.shell_session_port import set_shell_session_service_resolver
from runtime_shared.shell_session_contracts import ShellSessionIdentity
from runtime_shared.terminal_manager_port import set_terminal_session_manager_resolver

from .terminal.manager import TerminalSessionManager, terminal_session_manager
from .terminal.models import TerminalSession
from .terminal.shell_session_service import ShellSessionService


def _resolve_terminal_session_manager():
    """Return the active terminal session manager for runtime-shared consumers."""
    return terminal_session_manager


def _resolve_shell_runtime_context(identity: ShellSessionIdentity):
    """Resolve runtime context for production shell-session service composition."""
    db = SessionLocal()
    try:
        resolver = RuntimeProviderContextResolver(db)
        return resolver.resolve_internal_task_context(
            task_id=identity.task_id,
            actor_type=RuntimeActorType.AGENT,
            actor_id=f"shell_session:{identity.execution_owner_id}",
        )
    finally:
        db.close()


shell_session_service = ShellSessionService(
    terminal_manager=terminal_session_manager,
    lifecycle_projector=ShellSessionLifecycleProjector(
        session_factory=SessionLocal,
        stream_hub_provider=get_in_memory_stream_hub,
        wall_clock=time.time,
    ),
    runtime_context_resolver=_resolve_shell_runtime_context,
)


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
