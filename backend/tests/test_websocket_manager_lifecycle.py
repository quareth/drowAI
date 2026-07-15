"""Lifecycle regression tests for websocket manager background cleanup."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.services.websocket.connection_manager import WebSocketManager


def test_websocket_manager_init_does_not_require_running_loop() -> None:
    """Instantiation must be safe during module import and test collection."""
    manager = WebSocketManager()
    assert manager.cleanup_task is None


@pytest.mark.asyncio
async def test_websocket_manager_cleanup_task_start_and_stop() -> None:
    """Cleanup task should be startable/stoppable under a running loop."""
    manager = WebSocketManager()

    manager.start_cleanup_task()
    assert manager.cleanup_task is not None
    assert not manager.cleanup_task.done()

    await manager.stop_cleanup_task()
    assert manager.cleanup_task is None


class _DummyWebSocket:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.client = SimpleNamespace(host=host)
        self.close_events: list[tuple[int | None, str | None]] = []

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


class _BroadcastWebSocket(_DummyWebSocket):
    def __init__(self, host: str = "127.0.0.1", *, fail_send: bool = False) -> None:
        super().__init__(host=host)
        self.fail_send = fail_send
        self.sent_payloads: list[dict[str, object]] = []

    async def send_text(self, payload: str) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent_payloads.append(json.loads(payload))


@pytest.mark.asyncio
async def test_unregister_connection_cleans_metadata_and_rate_limit() -> None:
    manager = WebSocketManager()
    websocket = _DummyWebSocket()

    manager.active_connections[42] = {websocket}
    manager.track_connection_metadata(websocket, task_id=42, client_ip="127.0.0.1")
    manager.rate_limiter.connection_limits["127.0.0.1"] = 2

    await manager.unregister_connection(websocket, 42)

    assert websocket not in manager.connection_metadata
    assert manager.rate_limiter.connection_limits["127.0.0.1"] == 1
    assert 42 not in manager.active_connections


@pytest.mark.asyncio
async def test_unregister_connection_decrements_rate_limit_counter_and_never_negative() -> None:
    manager = WebSocketManager()
    websocket = _DummyWebSocket("10.10.10.10")

    manager.active_connections[7] = {websocket}
    manager.track_connection_metadata(websocket, task_id=7, client_ip="10.10.10.10")
    manager.rate_limiter.connection_limits["10.10.10.10"] = 1

    await manager.unregister_connection(websocket, 7)
    assert manager.rate_limiter.connection_limits["10.10.10.10"] == 0

    # A second unregister should not drive the counter below zero.
    await manager.unregister_connection(websocket, 7)
    assert manager.rate_limiter.connection_limits["10.10.10.10"] == 0


@pytest.mark.asyncio
async def test_register_connection_rolls_back_rate_limit_on_rejected_gates() -> None:
    manager = WebSocketManager()
    websocket = _DummyWebSocket("10.20.30.40")

    manager.validate_connection_limits = lambda _task_id: False  # noqa: ARG005
    await manager.register_connection(websocket, task_id=11)

    assert manager.rate_limiter.connection_limits["10.20.30.40"] == 0
    assert websocket.close_events[-1] == (1013, "Connection limit reached")

    manager.validate_connection_limits = lambda _task_id: True  # noqa: ARG005
    manager._should_allow_connection = lambda _task_id: False  # noqa: ARG005
    await manager.register_connection(websocket, task_id=11)

    assert manager.rate_limiter.connection_limits["10.20.30.40"] == 0
    assert websocket.close_events[-1] == (1013, "Service temporarily unavailable")


@pytest.mark.asyncio
async def test_broadcast_to_task_reaches_metrics_subscribers() -> None:
    manager = WebSocketManager()
    ws_active = _BroadcastWebSocket()
    ws_metrics = _BroadcastWebSocket()

    manager.active_connections[99] = {ws_active}
    manager.metrics_streamer.metrics_connections[99] = {ws_metrics}

    await manager.broadcast_to_task(99, {"type": "status_update", "status": "paused"})

    assert ws_active.sent_payloads == [{"type": "status_update", "status": "paused"}]
    assert ws_metrics.sent_payloads == [{"type": "status_update", "status": "paused"}]


@pytest.mark.asyncio
async def test_broadcast_to_task_disconnects_failed_metrics_subscribers() -> None:
    manager = WebSocketManager()
    ws_metrics_ok = _BroadcastWebSocket()
    ws_metrics_bad = _BroadcastWebSocket(fail_send=True)

    manager.metrics_streamer.metrics_connections[77] = {ws_metrics_ok, ws_metrics_bad}
    manager.metrics_streamer.metrics_tasks[ws_metrics_ok] = asyncio.create_task(asyncio.sleep(3600))
    manager.metrics_streamer.metrics_tasks[ws_metrics_bad] = asyncio.create_task(asyncio.sleep(3600))

    await manager.broadcast_to_task(77, {"type": "status_update", "status": "running"})

    assert ws_metrics_ok.sent_payloads == [{"type": "status_update", "status": "running"}]
    assert ws_metrics_bad not in manager.metrics_streamer.metrics_connections.get(77, set())
    assert ws_metrics_bad not in manager.metrics_streamer.metrics_tasks

    await manager.metrics_streamer.disconnect_metrics(ws_metrics_ok, 77)
