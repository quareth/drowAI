"""Tests for backend terminal service contracts.

This module owns structural checks for backend-only terminal application
boundaries. It does not exercise PTY transport behavior or runtime-shared DTOs.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import get_type_hints

import pytest

from backend.services.terminal import contracts
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellSessionIdentity,
    ShellSessionOrigin,
)


def _identity() -> ShellSessionIdentity:
    return ShellSessionIdentity(
        tenant_id=10,
        task_id=20,
        execution_owner_id="main:turn-1",
        runtime_placement_mode="local",
        workspace_id="workspace-1",
        workspace_path="/workspace",
        runner_id=None,
        execution_site_id="site-1",
    )


def test_shell_session_terminal_event_is_immutable_backend_projection_fact() -> None:
    event = contracts.ShellSessionTerminalEvent(
        identity=_identity(),
        public_session_id="shell-public-1",
        originating_capability=ShellCapability.ASSESSMENT,
        origin=ShellSessionOrigin(
            tool_call_id="call-1",
            tool_batch_id="batch-1",
            tool_name="shell.assessment",
        ),
        close_reason="owner_cleanup",
    )

    params = contracts.ShellSessionTerminalEvent.__dataclass_params__
    assert params.frozen is True
    assert getattr(contracts.ShellSessionTerminalEvent, "__slots__", ()) == (
        "identity",
        "public_session_id",
        "originating_capability",
        "origin",
        "close_reason",
    )
    assert tuple(field.name for field in dataclasses.fields(event)) == (
        "identity",
        "public_session_id",
        "originating_capability",
        "origin",
        "close_reason",
    )
    assert event.identity is not None
    assert event.origin is not None
    assert event.originating_capability is ShellCapability.ASSESSMENT

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.close_reason = "task_cleanup"  # type: ignore[misc]


def test_shell_session_terminal_event_reuses_existing_contract_types() -> None:
    hints = get_type_hints(contracts.ShellSessionTerminalEvent)

    assert hints == {
        "identity": ShellSessionIdentity,
        "public_session_id": str,
        "originating_capability": ShellCapability,
        "origin": ShellSessionOrigin | None,
        "close_reason": str,
    }


def test_shell_session_lifecycle_projector_port_exposes_one_async_operation() -> None:
    protocol = contracts.ShellSessionLifecycleProjectorPort
    public_members = {
        name
        for name, value in protocol.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    }

    assert public_members == {"project_terminal_event"}
    assert inspect.iscoroutinefunction(protocol.project_terminal_event)
    assert str(inspect.signature(protocol.project_terminal_event)) == (
        "(self, event: 'ShellSessionTerminalEvent') -> 'None'"
    )
    hints = get_type_hints(protocol.project_terminal_event)
    assert hints == {
        "event": contracts.ShellSessionTerminalEvent,
        "return": type(None),
    }
    assert "register" not in protocol.__dict__
    assert "create" not in protocol.__dict__
    assert "factory" not in protocol.__dict__


class _RecordingProjector:
    def __init__(self) -> None:
        self.events: list[contracts.ShellSessionTerminalEvent] = []

    async def project_terminal_event(
        self, event: contracts.ShellSessionTerminalEvent
    ) -> None:
        self.events.append(event)


async def test_shell_session_lifecycle_projector_port_accepts_async_projector() -> None:
    event = contracts.ShellSessionTerminalEvent(
        identity=_identity(),
        public_session_id="shell-public-1",
        originating_capability=ShellCapability.UTILITY,
        origin=None,
        close_reason="cancelled",
    )
    projector: contracts.ShellSessionLifecycleProjectorPort = _RecordingProjector()

    result = await projector.project_terminal_event(event)

    assert result is None
    assert projector.events == [event]  # type: ignore[attr-defined]
