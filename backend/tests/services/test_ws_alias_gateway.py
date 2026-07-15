"""Tests for shared websocket alias prelude orchestration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.websocket.alias_gateway import authorize_alias_websocket
from backend.services.websocket.alias_policy import ALIAS_WS_DEPRECATION_HEADERS


class _FakeWebSocket:
    def __init__(self) -> None:
        self.client = SimpleNamespace(host="203.0.113.22")
        self.close_events: list[tuple[int | None, str | None]] = []

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))


@pytest.mark.asyncio
async def test_authorize_alias_websocket_rejects_invalid_origin() -> None:
    websocket = _FakeWebSocket()
    authorize_called = False

    async def _validate_origin(_ws):  # noqa: ANN001
        return False

    async def _authorize(_ws, **_kwargs):  # noqa: ANN001
        nonlocal authorize_called
        authorize_called = True
        return None

    context = await authorize_alias_websocket(
        websocket,
        task_id=10,
        endpoint="/api/docker/ws/logs/{task_id}",
        canonical="/ws?type=docker&taskId=<id>",
        validate_origin_func=_validate_origin,
        authorize_func=_authorize,
    )

    assert context is None
    assert authorize_called is False
    assert websocket.close_events == [(1008, "Invalid origin")]


@pytest.mark.asyncio
async def test_authorize_alias_websocket_stops_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    deprecation_logged = False

    async def _validate_origin(_ws):  # noqa: ANN001
        return True

    async def _authorize(_ws, **_kwargs):  # noqa: ANN001
        return None

    def _log_deprecation(**_kwargs):  # noqa: ANN001
        nonlocal deprecation_logged
        deprecation_logged = True

    monkeypatch.setattr("backend.services.websocket.alias_gateway.log_alias_ws_deprecation", _log_deprecation)

    context = await authorize_alias_websocket(
        websocket,
        task_id=11,
        endpoint="/api/docker/ws/terminal/{task_id}",
        canonical="/ws?type=terminal&taskId=<id>",
        validate_origin_func=_validate_origin,
        authorize_func=_authorize,
    )

    assert context is None
    assert deprecation_logged is False


@pytest.mark.asyncio
async def test_authorize_alias_websocket_logs_deprecation_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FakeWebSocket()
    deprecation_payloads: list[dict[str, object]] = []
    accept_headers_seen: list[object] = []

    async def _validate_origin(_ws):  # noqa: ANN001
        return True

    async def _authorize(_ws, **kwargs):  # noqa: ANN001
        accept_headers_seen.append(kwargs.get("accept_headers"))
        return SimpleNamespace(user_id=33, user_data={"sub": "owner"})

    def _log_deprecation(**kwargs):  # noqa: ANN001
        deprecation_payloads.append(kwargs)

    monkeypatch.setattr("backend.services.websocket.alias_gateway.log_alias_ws_deprecation", _log_deprecation)

    context = await authorize_alias_websocket(
        websocket,
        task_id=12,
        endpoint="/api/tasks/ws/tasks/{task_id}/metrics",
        canonical="/ws?type=metrics&taskId=<id>",
        validate_origin_func=_validate_origin,
        authorize_func=_authorize,
    )

    assert context is not None
    assert context.user_id == 33
    assert accept_headers_seen == [ALIAS_WS_DEPRECATION_HEADERS]
    assert deprecation_payloads == [
        {
            "endpoint": "/api/tasks/ws/tasks/{task_id}/metrics",
            "canonical": "/ws?type=metrics&taskId=<id>",
            "task_id": 12,
            "user_id": 33,
            "websocket": websocket,
        }
    ]
