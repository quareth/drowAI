"""Coverage for terminal websocket identity enforcement."""

from __future__ import annotations

import json

from fastapi import WebSocketDisconnect
import pytest
from starlette.websockets import WebSocketState

import backend.services.terminal.ws_handler as ws_handler
from backend.services.terminal.ws_handler import handle_terminal_ws


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []
        self.close_events: list[tuple[int | None, str | None]] = []

    async def send_text(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_events.append((code, reason))

    async def receive_text(self) -> str:
        raise AssertionError("receive_text should not be called when identity is missing")


class _UnacceptedFakeWebSocket(_FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.application_state = WebSocketState.CONNECTING
        self.accept_calls = 0

    async def accept(self) -> None:
        self.accept_calls += 1
        self.application_state = WebSocketState.CONNECTED


@pytest.mark.asyncio
async def test_handle_terminal_ws_rejects_missing_identity() -> None:
    websocket = _FakeWebSocket()

    await handle_terminal_ws(websocket, task_id=123, user_id=None)

    assert websocket.sent_payloads == [{"type": "error", "message": "identity_required"}]
    assert websocket.close_events == [(1008, "identity_required")]


@pytest.mark.asyncio
async def test_handle_terminal_ws_accepts_if_gateway_did_not_accept() -> None:
    websocket = _UnacceptedFakeWebSocket()

    await handle_terminal_ws(websocket, task_id=123, user_id=None)

    assert websocket.accept_calls == 1
    assert websocket.sent_payloads == [{"type": "error", "message": "identity_required"}]
    assert websocket.close_events == [(1008, "identity_required")]


@pytest.mark.asyncio
async def test_handle_terminal_ws_closes_session_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _Session:
        session_id = "session-1"

    class _DisconnectWebSocket:
        def __init__(self) -> None:
            self._messages = [json.dumps({"type": "create_session"})]

        async def send_text(self, payload: str) -> None:
            del payload

        async def close(self, code: int | None = None, reason: str | None = None) -> None:
            del code, reason

        async def receive_text(self) -> str:
            if self._messages:
                return self._messages.pop(0)
            raise WebSocketDisconnect(code=1000)

    async def _create_session(task_id: int, user_id: int, authorized_task=None):  # noqa: ANN001, ANN201
        del task_id, user_id, authorized_task
        return _Session()

    async def _attach_websocket(session_id: str, websocket: object) -> None:
        del websocket
        calls.append(("attach", session_id))

    async def _close_session(session_id: str) -> bool:
        calls.append(("close", session_id))
        return True

    async def _detach_websocket(session_id: str, websocket: object) -> None:
        del websocket
        calls.append(("detach", session_id))

    async def _schedule_disconnect_grace(session_id: str) -> None:
        calls.append(("grace", session_id))

    monkeypatch.setattr(ws_handler.terminal_session_manager, "create_session", _create_session)
    monkeypatch.setattr(ws_handler.terminal_session_manager, "attach_websocket", _attach_websocket)
    monkeypatch.setattr(ws_handler.terminal_session_manager, "close_session", _close_session)
    monkeypatch.setattr(ws_handler.terminal_session_manager, "detach_websocket", _detach_websocket)
    monkeypatch.setattr(ws_handler.terminal_session_manager, "schedule_disconnect_grace", _schedule_disconnect_grace)

    websocket = _DisconnectWebSocket()
    await handle_terminal_ws(websocket, task_id=42, user_id=7)

    assert ("attach", "session-1") in calls
    assert ("detach", "session-1") in calls
    assert ("grace", "session-1") in calls
    assert ("close", "session-1") not in calls
