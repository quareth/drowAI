"""Tests for websocket channel handler orchestration contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.services.tenant.authorization import ACTION_TASK_CONTROL
from backend.services.websocket.channel_handlers import (
    serve_metrics_task_websocket,
    serve_terminal_task_websocket,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict[str, object]] = []

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))


@pytest.mark.asyncio
async def test_metrics_handler_requires_connection_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    register_calls: list[int] = []
    unregister_calls: list[int] = []
    serve_calls: list[int] = []

    async def _ownership_enforcer(_websocket, **_kwargs):  # noqa: ANN001
        return True

    class _MetricsStreamerStub:
        async def serve_metrics_websocket(self, _websocket, task_id: int) -> None:  # noqa: ANN001
            serve_calls.append(task_id)

    class _ManagerStub:
        def __init__(self) -> None:
            self.metrics_streamer = _MetricsStreamerStub()

        async def register_connection(self, _websocket, task_id: int) -> bool:  # noqa: ANN001
            register_calls.append(task_id)
            return False

        async def unregister_connection(self, _websocket, task_id: int) -> None:  # noqa: ANN001
            unregister_calls.append(task_id)

    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        _ManagerStub(),
    )

    await serve_metrics_task_websocket(
        websocket,
        task_id=51,
        user_id=7,
        ownership_enforcer=_ownership_enforcer,
    )

    assert register_calls == [51]
    assert serve_calls == []
    assert unregister_calls == []


@pytest.mark.asyncio
async def test_metrics_handler_unregisters_connection_after_stream_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    register_calls: list[int] = []
    unregister_calls: list[int] = []
    serve_calls: list[int] = []

    async def _ownership_enforcer(_websocket, **_kwargs):  # noqa: ANN001
        return True

    class _MetricsStreamerStub:
        async def serve_metrics_websocket(self, _websocket, task_id: int) -> None:  # noqa: ANN001
            serve_calls.append(task_id)

    class _ManagerStub:
        def __init__(self) -> None:
            self.metrics_streamer = _MetricsStreamerStub()

        async def register_connection(self, _websocket, task_id: int) -> bool:  # noqa: ANN001
            register_calls.append(task_id)
            return True

        async def unregister_connection(self, _websocket, task_id: int) -> None:  # noqa: ANN001
            unregister_calls.append(task_id)

    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        _ManagerStub(),
    )

    await serve_metrics_task_websocket(
        websocket,
        task_id=52,
        user_id=8,
        ownership_enforcer=_ownership_enforcer,
    )

    assert register_calls == [52]
    assert serve_calls == [52]
    assert unregister_calls == [52]


@pytest.mark.asyncio
async def test_terminal_handler_requires_task_control_action() -> None:
    websocket = _FakeWebSocket()
    observed: dict[str, object] = {}

    async def _ownership_enforcer(_websocket, **kwargs):  # noqa: ANN001
        observed.update(kwargs)
        return False

    await serve_terminal_task_websocket(
        websocket,
        task_id=71,
        user_id=11,
        ownership_enforcer=_ownership_enforcer,
    )

    assert observed["action"] == ACTION_TASK_CONTROL
    assert observed["task_id"] == 71
    assert observed["user_id"] == 11


@pytest.mark.asyncio
async def test_terminal_handler_passes_authorized_task_to_terminal_ws_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    authorized_task = SimpleNamespace(id=91, tenant_id=501)
    observed: dict[str, object] = {}

    async def _ownership_enforcer(_websocket, **_kwargs):  # noqa: ANN001
        return True

    monkeypatch.setattr(
        "backend.services.websocket.channel_handlers.get_ws_task_for_bound_tenant",
        lambda _ws, *, task_id, user_id: authorized_task if (task_id, user_id) == (91, 17) else None,
    )

    async def _handle_terminal_ws(_websocket, task_id: int, user_id: int, authorized_task=None):  # noqa: ANN001
        observed["task_id"] = task_id
        observed["user_id"] = user_id
        observed["authorized_task"] = authorized_task

    monkeypatch.setattr("backend.services.terminal.ws_handler.handle_terminal_ws", _handle_terminal_ws)

    await serve_terminal_task_websocket(
        websocket,
        task_id=91,
        user_id=17,
        include_connection_user=False,
        ownership_enforcer=_ownership_enforcer,
    )

    assert observed == {
        "task_id": 91,
        "user_id": 17,
        "authorized_task": authorized_task,
    }
