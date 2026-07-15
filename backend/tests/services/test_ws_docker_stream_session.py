"""Tests for shared docker websocket stream session lifecycle."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from backend.services.websocket.docker_stream_session import WSDockerStreamSessionService


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []
        self._incoming = [
            json.dumps({"type": "ping"}),
            json.dumps({"type": "request_logs"}),
        ]

    async def send_text(self, payload: str) -> None:
        try:
            self.sent_payloads.append(json.loads(payload))
        except json.JSONDecodeError:
            self.sent_payloads.append({"raw": payload})

    async def receive_text(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        raise WebSocketDisconnect(code=1000)


@pytest.mark.asyncio
async def test_serve_docker_websocket_uses_single_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WSDockerStreamSessionService()
    websocket = _FakeWebSocket()
    register_calls: list[int] = []
    unregister_calls: list[int] = []
    log_stream_calls: list[int] = []
    metrics_stream_calls: list[int] = []

    class _MetricsStreamerStub:
        async def stream_metrics_to_client(self, _ws: _FakeWebSocket, task_id: int) -> None:
            metrics_stream_calls.append(task_id)
            await asyncio.sleep(3600)

    class _ManagerStub:
        def __init__(self) -> None:
            self.metrics_streamer = _MetricsStreamerStub()

        async def register_connection(self, _ws: _FakeWebSocket, task_id: int) -> bool:
            register_calls.append(task_id)
            return True

        async def unregister_connection(self, _ws: _FakeWebSocket, task_id: int) -> None:
            unregister_calls.append(task_id)

    async def _fake_log_stream(_ws: _FakeWebSocket, task_id: int) -> None:
        log_stream_calls.append(task_id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        _ManagerStub(),
    )
    monkeypatch.setattr(
        "backend.services.websocket.docker_stream_session.stream_logs_to_client",
        _fake_log_stream,
    )

    await service.serve_docker_websocket(websocket, task_id=55, user_sub="owner")

    assert register_calls == [55]
    assert unregister_calls == [55]
    assert log_stream_calls == [55]
    assert metrics_stream_calls == [55]
    assert websocket.sent_payloads[0]["type"] == "connection_established"
    assert any(payload.get("type") == "pong" for payload in websocket.sent_payloads)
    assert any(payload.get("type") == "log" for payload in websocket.sent_payloads)


@pytest.mark.asyncio
async def test_serve_docker_websocket_sends_handshake_before_stream_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WSDockerStreamSessionService()
    websocket = _FakeWebSocket()

    class _MetricsStreamerStub:
        async def stream_metrics_to_client(self, ws: _FakeWebSocket, task_id: int) -> None:
            await ws.send_text(json.dumps({"type": "metrics", "task_id": task_id}))
            await asyncio.sleep(3600)

    class _ManagerStub:
        def __init__(self) -> None:
            self.metrics_streamer = _MetricsStreamerStub()

        async def register_connection(self, _ws: _FakeWebSocket, _task_id: int) -> bool:
            return True

        async def unregister_connection(self, _ws: _FakeWebSocket, _task_id: int) -> None:
            return None

    async def _eager_log_stream(ws: _FakeWebSocket, task_id: int) -> None:
        await ws.send_text(json.dumps({"type": "heartbeat", "task_id": task_id}))
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        _ManagerStub(),
    )
    monkeypatch.setattr(
        "backend.services.websocket.docker_stream_session.stream_logs_to_client",
        _eager_log_stream,
    )

    await service.serve_docker_websocket(websocket, task_id=77, user_sub="owner")

    assert websocket.sent_payloads[0]["type"] == "connection_established"


@pytest.mark.asyncio
async def test_serve_docker_websocket_unregisters_when_stream_task_failed_before_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WSDockerStreamSessionService()
    websocket = _FakeWebSocket()
    websocket._incoming = []
    unregister_calls: list[int] = []

    class _MetricsStreamerStub:
        async def stream_metrics_to_client(self, _ws: _FakeWebSocket, _task_id: int) -> None:
            await asyncio.sleep(3600)

    class _ManagerStub:
        def __init__(self) -> None:
            self.metrics_streamer = _MetricsStreamerStub()

        async def register_connection(self, _ws: _FakeWebSocket, _task_id: int) -> bool:
            return True

        async def unregister_connection(self, _ws: _FakeWebSocket, task_id: int) -> None:
            unregister_calls.append(task_id)

    async def _failing_log_stream(_ws: _FakeWebSocket, _task_id: int) -> None:
        raise RuntimeError("log stream crashed")

    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        _ManagerStub(),
    )
    monkeypatch.setattr(
        "backend.services.websocket.docker_stream_session.stream_logs_to_client",
        _failing_log_stream,
    )

    await service.serve_docker_websocket(websocket, task_id=88, user_sub="owner")

    assert unregister_calls == [88]
