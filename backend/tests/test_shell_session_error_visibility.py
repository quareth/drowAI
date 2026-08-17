"""Stable public diagnostics for dedicated shell execution failures."""

from __future__ import annotations

import pytest

from backend.services.terminal.shell_session_service import ShellSessionService
from backend.tests.test_shell_session_service import (
    _CommandTerminalManager,
    _NoopProjector,
    _config,
    _identity,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellSessionErrorCode,
    ShellSessionIdentity,
)


@pytest.mark.asyncio
async def test_runtime_context_failure_hides_private_provider_detail() -> None:
    manager = _CommandTerminalManager()

    async def _unavailable(_identity: ShellSessionIdentity) -> object:
        raise RuntimeError("private provider detail")

    service = ShellSessionService(
        terminal_manager=manager,
        lifecycle_projector=_NoopProjector(),
        config=_config(),
        runtime_context_resolver=_unavailable,
    )
    update = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="printf quick", yield_time_ms=0),
        capability=ShellCapability.UTILITY,
    )

    assert update.error_code is ShellSessionErrorCode.SHELL_RUNTIME_UNAVAILABLE
    assert update.stderr == "Shell runtime is unavailable for this task."
    assert "private provider detail" not in update.stderr
    assert manager.create_calls == []
