"""Deprecated websocket alias routes for docker and terminal channels.

Responsibilities:
- Keep compatibility websocket aliases available under `/api/docker/ws/*`.
- Delegate shared alias prelude (origin/auth/deprecation) to alias gateway helper.
- Delegate channel websocket behavior to shared channel handlers.

Boundary:
- No REST endpoint handling.
- No channel-specific policy duplication beyond handler selection.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket

from backend.services.websocket.alias_gateway import authorize_alias_websocket
from backend.services.websocket.channel_handlers import (
    serve_docker_task_websocket,
    serve_terminal_task_websocket,
)
from backend.services.websocket.gateway import enforce_ws_task_ownership

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/logs/{task_id}")
async def websocket_logs_endpoint(
    websocket: WebSocket,
    task_id: int,
) -> None:
    """
    WebSocket endpoint for real-time Docker logs streaming with token authentication.
    """
    try:
        auth_ctx = await authorize_alias_websocket(
            websocket,
            task_id=task_id,
            endpoint="/api/docker/ws/logs/{task_id}",
            canonical="/ws?type=docker&taskId=<id>",
        )
        if auth_ctx is None:
            return

        logger.info("[WebSocket] Docker logs connection established for task %s", task_id)
        await serve_docker_task_websocket(
            websocket,
            task_id,
            user_id=auth_ctx.user_id,
            user_sub=auth_ctx.user_data.get("sub"),
            ownership_enforcer=enforce_ws_task_ownership,
        )
    except Exception as e:
        logger.error(f"[WebSocket] Error in docker logs endpoint: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass


@router.websocket("/ws/terminal/{task_id}")
async def websocket_terminal_endpoint(
    websocket: WebSocket,
    task_id: int,
) -> None:
    """
    WebSocket endpoint for real-time terminal interaction using shared PTY handler.
    """
    try:
        auth_ctx = await authorize_alias_websocket(
            websocket,
            task_id=task_id,
            endpoint="/api/docker/ws/terminal/{task_id}",
            canonical="/ws?type=terminal&taskId=<id>",
        )
        if auth_ctx is None:
            return

        await serve_terminal_task_websocket(
            websocket,
            task_id,
            user_id=auth_ctx.user_id,
            user_sub=auth_ctx.user_data.get("sub"),
            include_connection_user=False,
            ownership_enforcer=enforce_ws_task_ownership,
        )
    except Exception as e:
        logger.error(f"[WebSocket] Error in terminal endpoint: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass
