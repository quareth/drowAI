"""Shared websocket alias gateway prelude for deprecated endpoint routes.

Responsibilities:
- Enforce alias origin validation and close policy.
- Reuse shared websocket auth/identity pipeline for alias routes.
- Emit deprecation telemetry consistently before channel handler delegation.

Boundary:
- No channel-specific websocket loops or message contracts.
- No task ownership enforcement (handled by channel handlers via gateway).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import WebSocket

from .alias_policy import (
    ALIAS_WS_DEPRECATION_HEADERS,
    log_alias_ws_deprecation,
    validate_alias_origin,
)
from .gateway import WSAuthContext, authorize_ws_connection

logger = logging.getLogger("backend.services.ws_alias_gateway")


async def authorize_alias_websocket(
    websocket: WebSocket,
    *,
    task_id: int,
    endpoint: str,
    canonical: str,
    validate_origin_func: Callable[[WebSocket], Awaitable[bool]] = validate_alias_origin,
    authorize_func: Callable[..., Awaitable[WSAuthContext | None]] = authorize_ws_connection,
) -> WSAuthContext | None:
    """Run shared alias websocket prelude and return authenticated context."""
    if not await validate_origin_func(websocket):
        logger.warning("websocket alias rejected due to invalid origin endpoint=%s task=%s", endpoint, task_id)
        await websocket.close(code=1008, reason="Invalid origin")
        return None

    auth_ctx = await authorize_func(
        websocket,
        accept_headers=ALIAS_WS_DEPRECATION_HEADERS,
    )
    if auth_ctx is None:
        return None

    log_alias_ws_deprecation(
        endpoint=endpoint,
        canonical=canonical,
        task_id=task_id,
        user_id=auth_ctx.user_id,
        websocket=websocket,
    )
    return auth_ctx
