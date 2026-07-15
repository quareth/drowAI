"""Regression tests for metrics websocket alias deprecation visibility."""

from __future__ import annotations

import logging
import importlib
from types import SimpleNamespace

import pytest
from backend.services.websocket.alias_policy import ALIAS_WS_DEPRECATION_HEADERS


class _FakeWebSocket:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.client = SimpleNamespace(host="203.0.113.10")
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] | None = None
        self.close_events: list[tuple[int | None, str | None]] = []

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        self.accepted_subprotocol = subprotocol
        self.accepted_headers = headers

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


class _MetricsStreamerStub:
    def __init__(self) -> None:
        self.serve_calls: list[int] = []

    async def serve_metrics_websocket(self, websocket, task_id: int) -> None:  # noqa: ANN001
        self.serve_calls.append(task_id)


class _WebSocketManagerStub:
    def __init__(self) -> None:
        self.metrics_streamer = _MetricsStreamerStub()
        self.register_calls: list[int] = []
        self.unregister_calls: list[int] = []

    async def register_connection(self, _websocket, task_id: int) -> bool:  # noqa: ANN001
        self.register_calls.append(task_id)
        return True

    async def unregister_connection(self, _websocket, task_id: int) -> None:  # noqa: ANN001
        self.unregister_calls.append(task_id)


def _get_metrics_router_module():
    # Import backend.main first to preserve the app's established import order.
    importlib.import_module("backend.main")
    return importlib.import_module("backend.routers.tasks.metrics")


@pytest.mark.asyncio
async def test_metrics_alias_emits_deprecation_headers_and_log_on_authorized_path(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics_router = _get_metrics_router_module()

    async def _authorize_alias_websocket(
        websocket, *, task_id: int, endpoint: str, canonical: str  # noqa: ANN001
    ):
        assert task_id == 42
        assert endpoint == "/api/tasks/ws/tasks/{task_id}/metrics"
        assert canonical == "/ws?type=metrics&taskId=<id>"
        await websocket.accept(
            subprotocol="Bearer.jwt-token",
            headers=list(ALIAS_WS_DEPRECATION_HEADERS),
        )
        return SimpleNamespace(user_id=17, user_data={"user_id": 17, "sub": "owner"})

    async def _enforce_ws_task_ownership(_websocket, **_kwargs):  # noqa: ANN001
        return True

    manager_stub = _WebSocketManagerStub()
    monkeypatch.setattr(
        "backend.services.websocket.connection_manager.websocket_manager",
        manager_stub,
    )
    monkeypatch.setattr(metrics_router, "authorize_alias_websocket", _authorize_alias_websocket)
    monkeypatch.setattr(metrics_router, "enforce_ws_task_ownership", _enforce_ws_task_ownership)

    caplog.set_level(logging.WARNING, logger="backend.services.ws_alias_gateway")
    websocket = _FakeWebSocket()
    await metrics_router.websocket_task_metrics(websocket, task_id=42)

    assert websocket.accepted_subprotocol == "Bearer.jwt-token"
    assert websocket.accepted_headers == ALIAS_WS_DEPRECATION_HEADERS
    assert manager_stub.register_calls == [42]
    assert manager_stub.metrics_streamer.serve_calls == [42]
    assert manager_stub.unregister_calls == [42]
    assert not any(
        "Metrics websocket alias rejected due to invalid origin" in record.getMessage()
        for record in caplog.records
    )
