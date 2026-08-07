"""Tests for the backend-free shell session service resolver port."""

from __future__ import annotations

import inspect

import pytest

from runtime_shared import shell_session_port
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_capabilities import ShellCapability


def _identity() -> ShellSessionIdentity:
    return ShellSessionIdentity(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:turn-123",
        runtime_placement_mode="runner",
        workspace_id="workspace-abc",
        workspace_path="/workspace",
        runner_id="runner-1",
        execution_site_id="site-1",
    )


class _BoundShellSessionService:
    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        capability: ShellCapability = ShellCapability.ASSESSMENT,
    ) -> ShellSessionUpdate:
        assert identity.execution_owner_id == "main:turn-123"
        assert request.command == "whoami"
        return ShellSessionUpdate(
            success=True,
            status="success",
            process_status=None,
            session_id=None,
            stdout="ok\n",
            stderr="",
            exit_code=0,
            stdin_available=False,
            truncated=False,
            duration_ms=1,
        )

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        return ShellCapability.ASSESSMENT

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate:
        assert identity.task_id == 11
        assert request.session_id == "shs_abc123"
        return ShellSessionUpdate(
            success=True,
            status="success",
            process_status=None,
            session_id="shs_abc123",
            stdout="",
            stderr="",
            exit_code=None,
            stdin_available=True,
            truncated=False,
            duration_ms=1,
        )

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None:
        assert (tenant_id, task_id, execution_owner_id) == (
            7,
            11,
            "main:turn-123",
        )

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None:
        assert (tenant_id, task_id) == (7, 11)


def test_shell_session_port_module_is_backend_free() -> None:
    source = inspect.getsource(shell_session_port)

    assert shell_session_port.__doc__
    assert "backend-free" in shell_session_port.__doc__.lower()
    assert "from backend" not in source
    assert "import backend" not in source


@pytest.mark.asyncio
async def test_unbound_service_fails_closed_with_structured_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_session_port, "_shell_session_service_resolver", None)

    service = shell_session_port.get_shell_session_service()

    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="whoami"),
    )
    assert update == ShellSessionUpdate(
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

    poll = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id="shs_abc123"),
    )
    assert poll.error_code is ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE
    assert (
        await service.get_session_capability(
            identity=_identity(),
            public_session_id="shs_abc123",
        )
        is None
    )

    await service.close_owner_sessions(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:turn-123",
    )
    await service.close_task_sessions(tenant_id=7, task_id=11)


@pytest.mark.asyncio
async def test_resolver_exposes_bound_service_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _BoundShellSessionService()
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )

    resolved = shell_session_port.get_shell_session_service()

    assert resolved is service
    update = await resolved.execute(
        identity=_identity(),
        request=ShellExecRequest(command="whoami"),
    )
    assert update.success is True
    assert update.stdout == "ok\n"

    poll = await resolved.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(session_id="shs_abc123"),
    )
    assert poll.session_id == "shs_abc123"

    await resolved.close_owner_sessions(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:turn-123",
    )
    await resolved.close_task_sessions(tenant_id=7, task_id=11)
