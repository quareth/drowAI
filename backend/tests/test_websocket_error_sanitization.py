"""Regression tests for sanitized websocket error payloads."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend import main as main_module
from backend.services.terminal import ws_handler as terminal_ws_handler


class _FakeWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
        incoming_messages: list[str] | None = None,
    ) -> None:
        self.headers: dict[str, str] = headers or {}
        self.query_params = query_params or {}
        self.sent_payloads: list[dict] = []
        self.close_events: list[tuple[int | None, str | None]] = []
        self._incoming_messages = list(incoming_messages or [])

    async def accept(self, subprotocol: str | None = None) -> None:  # noqa: ARG002
        return None

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))

    async def receive_text(self) -> str:
        if self._incoming_messages:
            return self._incoming_messages.pop(0)
        raise WebSocketDisconnect(code=1000)


@pytest.mark.asyncio
async def test_websocket_endpoint_sanitizes_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _auth_ok(websocket, **_kwargs):  # noqa: ANN001
        await websocket.accept(subprotocol="Bearer.valid-token")
        return SimpleNamespace(user_data={"user_id": 1, "sub": "owner"}, user_id=1)

    async def _handler_boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("sensitive internal error")

    monkeypatch.setattr(main_module, "authorize_ws_connection", _auth_ok)
    monkeypatch.setattr(main_module, "handle_agent_multi_websocket", _handler_boom)

    ws = _FakeWebSocket(
        headers={"sec-websocket-protocol": "Bearer.valid-token"},
        query_params={"type": "agent-multi"},
    )
    await main_module.websocket_endpoint(ws)

    assert any(p.get("message") == "Internal server error" and p.get("code") == "internal_error" for p in ws.sent_payloads)
    assert not any("sensitive internal error" in json.dumps(p) for p in ws.sent_payloads)


@pytest.mark.asyncio
async def test_handle_terminal_websocket_sanitizes_handler_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _allow(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return True

    async def _terminal_boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("terminal secret")

    monkeypatch.setattr(main_module, "enforce_ws_task_ownership", _allow)
    monkeypatch.setattr("backend.services.terminal.ws_handler.handle_terminal_ws", _terminal_boom)

    ws = _FakeWebSocket()
    await main_module.handle_terminal_websocket(ws, 1, {"sub": "owner"}, 1)

    assert any(p.get("message") == "terminal_error" for p in ws.sent_payloads)
    assert not any("terminal secret" in json.dumps(p) for p in ws.sent_payloads)


@pytest.mark.asyncio
async def test_terminal_ws_handler_sanitizes_close_session_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _close_boom(_session_id: str):  # noqa: ANN001
        raise RuntimeError("close session secret")

    monkeypatch.setattr(terminal_ws_handler.terminal_session_manager, "close_session", _close_boom)

    ws = _FakeWebSocket(
        incoming_messages=[json.dumps({"type": "close_session", "session_id": "sid-1"})]
    )
    await terminal_ws_handler.handle_terminal_ws(ws, task_id=1, user_id=1)

    assert any(p.get("message") == "terminal_error" for p in ws.sent_payloads)
    assert not any("close session secret" in json.dumps(p) for p in ws.sent_payloads)
