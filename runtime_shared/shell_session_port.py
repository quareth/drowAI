"""Backend-free shell session service resolver port.

This module exposes the process-local shell-session service contract used by
agent and graph code. It owns no backend implementation and returns structured
runtime-unavailable results when no service has been bound.
"""

from __future__ import annotations

from typing import Callable, Protocol

from runtime_shared.shell_capabilities import ShellCapability

from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)


class ShellSessionServicePort(Protocol):
    """Protocol for provider-backed interactive shell session operations."""

    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        capability: ShellCapability = ShellCapability.ASSESSMENT,
    ) -> ShellSessionUpdate: ...

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None: ...

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate: ...

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None: ...

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None: ...


class _UnavailableShellSessionService:
    """Fail-closed service used before backend composition binds the real port."""

    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        capability: ShellCapability = ShellCapability.ASSESSMENT,
    ) -> ShellSessionUpdate:
        return _runtime_unavailable_update()

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        return None

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate:
        return _runtime_unavailable_update()

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None:
        return None

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None:
        return None


_UNAVAILABLE_SERVICE = _UnavailableShellSessionService()
_shell_session_service_resolver: Callable[[], ShellSessionServicePort] | None = None


def _runtime_unavailable_update() -> ShellSessionUpdate:
    return ShellSessionUpdate(
        success=False,
        status="error",
        process_status=None,
        session_id=None,
        stdout="",
        stderr="",
        exit_code=None,
        stdin_available=False,
        truncated=False,
        duration_ms=0,
        error_code=ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE,
    )


def set_shell_session_service_resolver(
    resolver: Callable[[], ShellSessionServicePort],
) -> None:
    """Register the process-local resolver for the shell-session service."""
    global _shell_session_service_resolver
    _shell_session_service_resolver = resolver


def clear_shell_session_service_resolver() -> None:
    """Clear the process-local shell-session service resolver."""
    global _shell_session_service_resolver
    _shell_session_service_resolver = None


def get_shell_session_service() -> ShellSessionServicePort:
    """Return the configured shell-session service or a fail-closed substitute."""
    if _shell_session_service_resolver is None:
        return _UNAVAILABLE_SERVICE
    return _shell_session_service_resolver()
