"""Docker websocket stream session service.

Responsibilities:
- Own docker-channel websocket stream lifecycle for log + metrics background tasks.
- Register/unregister websocket transport connections through the connection manager.
- Handle docker websocket keepalive and lightweight control messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List

from fastapi import WebSocket, WebSocketDisconnect

from .log_streamer import stream_logs_to_client

logger = logging.getLogger("backend.services.ws_docker_stream_session")


class WSDockerStreamSessionService:
    """Serve docker websocket sessions using one shared lifecycle path."""

    def __init__(self) -> None:
        self._session_tasks: Dict[WebSocket, List[asyncio.Task]] = {}

    async def _cancel_session_tasks(self, websocket: WebSocket) -> None:
        tasks = self._session_tasks.pop(websocket, [])
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.warning(
                    "Docker websocket background task failed during session cleanup",
                    exc_info=True,
                )

    async def serve_docker_websocket(
        self,
        websocket: WebSocket,
        task_id: int,
        *,
        user_sub: str | None = None,
    ) -> None:
        """Run docker websocket lifecycle for one websocket connection."""
        from .connection_manager import websocket_manager

        registered = await websocket_manager.register_connection(websocket, task_id)
        if not registered:
            return

        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "connection_established",
                        "connection": "docker",
                        "task_id": task_id,
                        "user": user_sub,
                    }
                )
            )

            log_task = asyncio.create_task(stream_logs_to_client(websocket, task_id))
            metrics_task = asyncio.create_task(
                websocket_manager.metrics_streamer.stream_metrics_to_client(websocket, task_id)
            )
            self._session_tasks[websocket] = [log_task, metrics_task]
            # Yield once so newly created stream tasks can start before the
            # websocket receive loop potentially exits immediately.
            await asyncio.sleep(0)

            while True:
                incoming = await websocket.receive_text()
                if incoming == "ping":
                    await websocket.send_text("pong")
                    continue

                try:
                    message = json.loads(incoming)
                except json.JSONDecodeError:
                    continue

                if message.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif message.get("type") == "request_logs":
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "log",
                                "task_id": task_id,
                                "message": "Docker logs streaming initiated",
                            }
                        )
                    )
        except WebSocketDisconnect:
            logger.info("Docker websocket disconnected for task %s", task_id)
        except Exception:
            logger.error("Docker websocket stream error for task %s", task_id, exc_info=True)
        finally:
            await self._cancel_session_tasks(websocket)
            await websocket_manager.unregister_connection(websocket, task_id)


ws_docker_stream_session_service = WSDockerStreamSessionService()
