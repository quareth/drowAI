"""
Shared PTY WebSocket handler
Centralizes interactive terminal session handling over WebSocket using
docker exec persistent PTY via terminal_session_manager.

Usage:
- Caller must accept the WebSocket and handle authentication.
- Then call `handle_terminal_ws(websocket, task_id, user_id)`.

Message contract (text frames JSON):
- Client -> Server:
  - {"type":"ping"}
  - {"type":"create_session"}
  - {"type":"input","data":"..."}  // raw keystrokes
  - {"type":"resize","cols":80,"rows":24}

- Server -> Client:
  - {"type":"pong"}
  - {"type":"session_created","session_id":"...","session":{...}}
  - Binary frames with PTY output (raw bytes)
  - {"type":"error","message":"..."}
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .manager import terminal_session_manager

if TYPE_CHECKING:
    from ...models import Task


logger = logging.getLogger("backend.services.terminal_ws_handler")


async def _ensure_websocket_accepted(websocket: WebSocket) -> None:
    """Accept real Starlette websockets if a caller skipped the gateway accept step."""
    if getattr(websocket, "application_state", None) is WebSocketState.CONNECTING:
        await websocket.accept()


async def handle_terminal_ws(
    websocket: WebSocket,
    task_id: int,
    user_id: Optional[int] = None,
    authorized_task: "Task | None" = None,
) -> None:
    """Run a PTY-backed terminal session over an accepted WebSocket.

    The caller must have already called `await websocket.accept()` and
    performed any authentication/authorization checks. This function will
    handle the message loop and lifecycle of the terminal session.
    """
    session = None
    session_id: Optional[str] = None
    await _ensure_websocket_accepted(websocket)

    if user_id is None:
        await websocket.send_text(json.dumps({"type": "error", "message": "identity_required"}))
        await websocket.close(code=1008, reason="identity_required")
        return

    try:
        while True:
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "Invalid JSON format"})
                )
                continue

            message_type = message.get("type")

            if message_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if message_type == "create_session":
                # Create a persistent PTY session via the session manager
                session = await terminal_session_manager.create_session(
                    task_id,
                    user_id,
                    authorized_task=authorized_task,
                )
                if not session:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Failed to create terminal session"}))
                    continue
                session_id = session.session_id
                # Attach this websocket and replay buffer
                await terminal_session_manager.attach_websocket(session_id, websocket)
                continue

            if message_type == "resume_session":
                # Resume an existing session by id (validate task)
                req_sid = message.get("session_id")
                sess = terminal_session_manager.get_session(req_sid) if hasattr(terminal_session_manager, 'get_session') else None
                if not sess or not sess.is_active:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Session not found or inactive"}))
                    continue
                if int(getattr(sess, 'task_id', -1)) != int(task_id):
                    await websocket.send_text(json.dumps({"type": "error", "message": "Session does not belong to this task"}))
                    continue
                session_id = sess.session_id
                await terminal_session_manager.attach_websocket(session_id, websocket)
                continue

            if message_type == "close_session":
                # Close the specified session (defaults to current if not provided)
                req_sid = message.get("session_id") or session_id
                if not req_sid:
                    await websocket.send_text(json.dumps({"type": "error", "message": "No session to close"}))
                    continue
                try:
                    await terminal_session_manager.close_session(req_sid)
                    await websocket.send_text(json.dumps({"type": "session_closed", "session_id": req_sid}))
                except Exception:
                    logger.error("Failed to close terminal session", exc_info=True)
                    await websocket.send_text(json.dumps({"type": "error", "message": "terminal_error"}))
                continue

            if message_type == "input":
                # Write keystrokes to PTY
                # Write keystrokes to PTY
                sid = session_id
                sess = terminal_session_manager.get_session(sid) if sid else None
                if not sess or not sess.is_active:
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": "No active session"})
                    )
                    continue
                input_data = message.get("data", "")
                if input_data:
                    try:
                        await terminal_session_manager.send_input(sid, input_data)
                    except Exception as e:
                        logger.error(f"PTY input error: {e}")
                continue

            if message_type == "resize":
                # Best-effort resize (handled by manager/unified service)
                if session_id:
                    cols = message.get("cols", 80)
                    rows = message.get("rows", 24)
                    success = await terminal_session_manager.resize_session(
                        session_id, cols, rows
                    )
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "resize_result",
                                "success": success,
                                "cols": cols,
                                "rows": rows,
                            }
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "No active session for resize",
                            }
                        )
                    )
                continue

            # Unknown message
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "message": f"Unknown message type: {message_type}",
                    }
                )
            )

    except WebSocketDisconnect:
        logger.info(f"Terminal WebSocket disconnected for task {task_id}")
    except Exception as e:
        msg = str(e)
        if 'Cannot call "send" once a close message has been sent' in msg:
            logger.info("Terminal WebSocket closed during send; ignoring")
        else:
            logger.error(f"Terminal WebSocket error: {e}")
        # Avoid sending anything on a possibly closed socket
    finally:
        # Detach the websocket and allow a short reconnect grace before closing PTY.
        try:
            if session_id:
                await terminal_session_manager.detach_websocket(session_id, websocket)
                await terminal_session_manager.schedule_disconnect_grace(session_id)
        except Exception:
            pass
