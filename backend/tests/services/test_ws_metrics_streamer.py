"""Tests for extracted websocket metrics streamer behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend.services.websocket.metrics_streamer import WSMetricsStreamer
from backend.services.websocket import metrics_streamer as metrics_streamer_module


class _FakeWebSocket:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent_payloads: list[dict] = []
        self.client_state = SimpleNamespace(name="CONNECTED")

    async def send_text(self, payload: str) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent_payloads.append(json.loads(payload))

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1000)


@pytest.mark.asyncio
async def test_metrics_subscription_lifecycle_registers_and_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = WSMetricsStreamer()
    websocket = _FakeWebSocket()

    async def _stream_never(_ws: _FakeWebSocket, _task_id: int) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(streamer, "_stream_metrics_to_client", _stream_never)

    await streamer.handle_metrics_subscription(websocket, task_id=11)

    assert 11 in streamer.metrics_connections
    assert websocket in streamer.metrics_connections[11]
    assert websocket in streamer.metrics_tasks

    await streamer.disconnect_metrics(websocket, task_id=11)
    await asyncio.sleep(0)

    assert 11 not in streamer.metrics_connections
    assert websocket not in streamer.metrics_tasks


@pytest.mark.asyncio
async def test_broadcast_metrics_update_disconnects_failed_connections() -> None:
    streamer = WSMetricsStreamer()
    ws_ok = _FakeWebSocket()
    ws_bad = _FakeWebSocket(fail_send=True)

    streamer.metrics_connections[22] = {ws_ok, ws_bad}
    streamer.metrics_tasks[ws_ok] = asyncio.create_task(asyncio.sleep(3600))
    streamer.metrics_tasks[ws_bad] = asyncio.create_task(asyncio.sleep(3600))

    await streamer.broadcast_metrics_update(22, {"cpu_percent": 1.2})

    assert any(payload.get("type") == "metrics_update" for payload in ws_ok.sent_payloads)
    assert ws_bad not in streamer.metrics_connections.get(22, set())
    assert ws_bad not in streamer.metrics_tasks

    await streamer.disconnect_metrics(ws_ok, 22)


@pytest.mark.asyncio
async def test_serve_metrics_websocket_runs_single_lifecycle_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = WSMetricsStreamer()

    class _LifecycleWebSocket(_FakeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self._messages = [json.dumps({"type": "ping"})]

        async def receive_text(self) -> str:
            if self._messages:
                return self._messages.pop(0)
            raise WebSocketDisconnect(code=1000)

    websocket = _LifecycleWebSocket()
    subscriptions: list[int] = []
    disconnects: list[int] = []

    async def _fake_subscribe(_websocket: _FakeWebSocket, task_id: int) -> None:
        subscriptions.append(task_id)

    async def _fake_disconnect(_websocket: _FakeWebSocket, task_id: int) -> None:
        disconnects.append(task_id)

    monkeypatch.setattr(streamer, "handle_metrics_subscription", _fake_subscribe)
    monkeypatch.setattr(streamer, "disconnect_metrics", _fake_disconnect)

    await streamer.serve_metrics_websocket(websocket, task_id=31)

    assert subscriptions == [31]
    assert disconnects == [31]
    assert websocket.sent_payloads[-1] == {"type": "pong"}


@pytest.mark.asyncio
async def test_stream_metrics_normalizes_runner_nested_metrics_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics stream should send client-facing runner metrics payloads."""

    streamer = WSMetricsStreamer()
    websocket = _FakeWebSocket()

    calls = {"count": 0}

    async def _fake_run_metrics_operation(*, task_id, operation, call, metadata=None):
        assert task_id == 66
        calls["count"] += 1
        if operation == "get_runtime_metrics":
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-66",
                        "metrics": {
                            "memory_usage": 64 * 1024 * 1024,
                            "memory_limit": 512 * 1024 * 1024,
                            "cpu_percent": 11.0,
                            "status": "running",
                            "container_running": True,
                        },
                    }
                },
            )
        return SimpleNamespace(ok=True, metadata={"delegate_result": "running"})

    async def _cancel_after_first_cycle(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(metrics_streamer_module, "_run_metrics_operation", _fake_run_metrics_operation)
    monkeypatch.setattr(metrics_streamer_module.asyncio, "sleep", _cancel_after_first_cycle)

    await streamer.stream_metrics_to_client(websocket, task_id=66)

    metrics_payloads = [payload for payload in websocket.sent_payloads if payload.get("type") == "metrics"]
    assert metrics_payloads
    data = metrics_payloads[0]["data"]
    assert data["cpu_percent"] == 11.0
    assert data["memory_usage_mb"] == 64.0
    assert data["memory_limit_mb"] == 512.0
    assert data["memory_percent"] == 12.5
    assert data["storage"]["used_mb"] == 0.0
    assert data["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert data["status"] == "running"
    assert data["container_running"] is True
    assert calls["count"] >= 1


@pytest.mark.asyncio
async def test_stream_metrics_stops_after_runner_not_found_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner not_found metrics snapshots should trigger `metrics_stopped` behavior."""

    streamer = WSMetricsStreamer()
    websocket = _FakeWebSocket()

    async def _fake_run_metrics_operation(*, task_id, operation, call, metadata=None):
        assert task_id == 67
        if operation == "get_runtime_metrics":
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-67",
                        "metrics": {"status": "not_found", "container_running": False},
                    }
                },
            )
        return SimpleNamespace(ok=True, metadata={"delegate_result": "not_found"})

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(metrics_streamer_module, "_run_metrics_operation", _fake_run_metrics_operation)
    monkeypatch.setattr(metrics_streamer_module.asyncio, "sleep", _no_wait)

    await streamer.stream_metrics_to_client(websocket, task_id=67)

    assert any(payload.get("type") == "metrics_stopped" for payload in websocket.sent_payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "error_message"),
    [
        ("RUNNER_OPERATION_RESULT_TIMEOUT", "operation timed out"),
        ("RUNNER_OPERATION_RESULT_MISMATCH", "result mismatch"),
        ("RUNNER_ASSIGNMENT_REQUIRED", "runner offline"),
    ],
)
async def test_stream_metrics_fails_closed_on_provider_metrics_failure(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    error_message: str,
) -> None:
    streamer = WSMetricsStreamer()
    websocket = _FakeWebSocket()

    async def _fake_run_metrics_operation(*, task_id, operation, call, metadata=None):
        assert task_id == 68
        assert operation == "get_runtime_metrics"
        return SimpleNamespace(
            ok=False,
            provider="cloud_runner",
            status=SimpleNamespace(value="failed"),
            error_code=error_code,
            error_message=error_message,
            metadata={},
        )

    monkeypatch.setattr(metrics_streamer_module, "_run_metrics_operation", _fake_run_metrics_operation)

    await streamer.stream_metrics_to_client(websocket, task_id=68)

    assert websocket.sent_payloads
    failure = websocket.sent_payloads[-1]
    assert failure["type"] == "metrics_stopped"
    assert failure["operation"] == "get_runtime_metrics"
    assert failure["error_code"] == error_code
    assert failure["error_message"] == error_message


@pytest.mark.asyncio
async def test_stream_metrics_fails_closed_on_provider_status_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamer = WSMetricsStreamer()
    websocket = _FakeWebSocket()

    async def _fake_run_metrics_operation(*, task_id, operation, call, metadata=None):
        assert task_id == 69
        if operation == "get_runtime_metrics":
            return SimpleNamespace(ok=True, metadata={"delegate_result": {}}, provider="cloud_runner")
        return SimpleNamespace(
            ok=False,
            provider="cloud_runner",
            status=SimpleNamespace(value="failed"),
            error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
            error_message="operation timed out",
            metadata={},
        )

    monkeypatch.setattr(metrics_streamer_module, "_run_metrics_operation", _fake_run_metrics_operation)

    await streamer.stream_metrics_to_client(websocket, task_id=69)

    assert websocket.sent_payloads
    failure = websocket.sent_payloads[-1]
    assert failure["type"] == "metrics_stopped"
    assert failure["operation"] == "get_runtime_status"
    assert failure["error_code"] == "RUNNER_OPERATION_RESULT_TIMEOUT"
    assert failure["error_message"] == "operation timed out"
