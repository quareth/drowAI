"""WebSocket channel orchestration handlers shared by canonical and alias routes.

Responsibilities:
- Own channel-specific websocket message loops and lifecycle orchestration.
- Reuse shared gateway ownership enforcement for task-scoped channels.
- Keep websocket contracts stable while centralizing channel behavior in one place.

Boundary:
- No token extraction/auth verification (handled by the gateway route pipeline).
- No alias-origin/deprecation policy (handled by alias_policy in alias routes).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

from backend.config import REASONING_WS_MAX_SUBSCRIPTIONS
from backend.services.tenant.authorization import (
    ACTION_STREAM_REPLAY,
    ACTION_STREAM_SUBSCRIBE,
    ACTION_TASK_CONTROL,
)
from .gateway import enforce_ws_task_ownership, get_ws_task_for_bound_tenant
from .types import (
    AgentMultiControlResponse,
    AgentMultiSubscribeRequest,
    AgentMultiUnsubscribeRequest,
)

logger = logging.getLogger("backend.services.ws_channel_handlers")

TaskOwnershipEnforcer = Callable[..., Awaitable[bool]]


async def serve_docker_task_websocket(
    websocket: WebSocket,
    task_id: int,
    *,
    user_id: int,
    user_sub: str | None = None,
    ownership_enforcer: TaskOwnershipEnforcer = enforce_ws_task_ownership,
) -> None:
    """Serve docker logs/metrics stream over websocket for one task."""
    try:
        if not await ownership_enforcer(
            websocket,
            connection_type="docker",
            task_id=task_id,
            user_id=user_id,
            close_on_forbidden=True,
        ):
            return

        from .docker_stream_session import ws_docker_stream_session_service

        await ws_docker_stream_session_service.serve_docker_websocket(
            websocket,
            task_id,
            user_sub=user_sub,
        )
    except Exception as exc:
        logger.error("Docker WebSocket error: %s", exc, exc_info=True)


async def serve_agent_multi_websocket(
    websocket: WebSocket,
    *,
    user_id: int,
    max_subscriptions: int = REASONING_WS_MAX_SUBSCRIPTIONS,
    ownership_enforcer: TaskOwnershipEnforcer = enforce_ws_task_ownership,
) -> None:
    """Serve multiplexed agent websocket subscribe/unsubscribe protocol."""
    from .reasoning_subscription import ws_reasoning_manager

    async def send_control(response: AgentMultiControlResponse) -> None:
        await websocket.send_text(json.dumps(response))

    try:
        await websocket.send_text('{"type":"connection_established","connection":"agent-multi"}')
        while True:
            msg_text = await websocket.receive_text()
            try:
                msg = json.loads(msg_text)
            except Exception:
                await send_control({"type": "error", "message": "invalid_json"})
                continue

            if msg.get("type") == "ping":
                await websocket.send_text('{"type":"pong"}')
                continue

            if msg.get("action") == "subscribe" and msg.get("channel") == "agent":
                try:
                    subscribe_request: AgentMultiSubscribeRequest = msg
                    task_id = int(subscribe_request.get("taskId"))
                except Exception:
                    await send_control({"type": "error", "message": "invalid_task_id"})
                    continue

                try:
                    last_seen = int(subscribe_request.get("last_seen_sequence") or 0)
                except Exception:
                    last_seen = 0

                if not await ownership_enforcer(
                    websocket,
                    connection_type="agent-multi",
                    task_id=task_id,
                    user_id=user_id,
                    close_on_forbidden=False,
                    action=ACTION_STREAM_REPLAY if last_seen > 0 else ACTION_STREAM_SUBSCRIBE,
                ):
                    continue

                already_subscribed = await ws_reasoning_manager.has_task_subscription(websocket, task_id)
                active_subscriptions = await ws_reasoning_manager.get_subscription_count_async(websocket)
                if active_subscriptions >= max_subscriptions and not already_subscribed:
                    logger.warning(
                        "agent-multi subscribe denied (max_subscriptions): ws=%s user_id=%s task=%s active=%s limit=%s",
                        id(websocket),
                        user_id,
                        task_id,
                        active_subscriptions,
                        max_subscriptions,
                    )
                    await send_control({"type": "error", "message": "max_subscriptions", "taskId": task_id})
                    continue

                if already_subscribed:
                    await send_control({"type": "subscribed", "taskId": task_id})
                    continue

                try:
                    sub_id = await ws_reasoning_manager.subscribe(websocket, task_id, last_seen)
                    active_after = await ws_reasoning_manager.get_subscription_count_async(websocket)
                    logger.info(
                        "agent-multi subscription opened: ws=%s user_id=%s task=%s sub=%s total=%s",
                        id(websocket),
                        user_id,
                        task_id,
                        sub_id,
                        active_after,
                    )
                    await send_control({"type": "subscribed", "taskId": task_id})
                except Exception:
                    logger.warning("agent-multi subscribe failed", exc_info=True)
                    await send_control({"type": "error", "message": "subscribe_failed", "taskId": task_id})
                continue

            if msg.get("action") == "unsubscribe" and msg.get("channel") == "agent":
                try:
                    unsubscribe_request: AgentMultiUnsubscribeRequest = msg
                    task_id = int(unsubscribe_request.get("taskId"))
                except Exception:
                    await send_control({"type": "error", "message": "invalid_task_id"})
                    continue

                try:
                    removed = await ws_reasoning_manager.unsubscribe_task(websocket, task_id)
                    remaining = await ws_reasoning_manager.get_subscription_count_async(websocket)
                except Exception:
                    logger.debug("unsubscribe error", exc_info=True)
                    removed = 0
                    remaining = 0

                if removed:
                    logger.info(
                        "agent-multi subscription closed: ws=%s user_id=%s task=%s removed=%s remaining=%s",
                        id(websocket),
                        user_id,
                        task_id,
                        removed,
                        remaining,
                    )
                await send_control({"type": "unsubscribed", "taskId": task_id})
                continue
    except WebSocketDisconnect as exc:
        logger.info(
            "agent-multi disconnect: ws=%s user_id=%s code=%s reason=%s",
            id(websocket),
            user_id,
            exc.code,
            getattr(exc, "reason", None),
        )
    except Exception as exc:
        logger.error("agent-multi error: %s", exc)
    finally:
        try:
            active_tasks = await ws_reasoning_manager.get_subscribed_tasks(websocket)
            if active_tasks:
                logger.info(
                    "agent-multi socket closing: ws=%s user_id=%s active_subscriptions=%s tasks=%s",
                    id(websocket),
                    user_id,
                    len(active_tasks),
                    active_tasks,
                )
            await ws_reasoning_manager.unsubscribe_all(websocket)
        except Exception:
            pass


async def serve_terminal_task_websocket(
    websocket: WebSocket,
    task_id: int,
    *,
    user_id: int,
    user_sub: str | None = None,
    include_connection_user: bool = True,
    ownership_enforcer: TaskOwnershipEnforcer = enforce_ws_task_ownership,
) -> None:
    """Serve terminal websocket channel using shared PTY terminal handler."""
    try:
        if not await ownership_enforcer(
            websocket,
            connection_type="terminal",
            task_id=task_id,
            user_id=user_id,
            close_on_forbidden=True,
            action=ACTION_TASK_CONTROL,
        ):
            return

        connection_payload: dict[str, Any] = {
            "type": "connection_established",
            "connection": "terminal",
            "task_id": task_id,
        }
        if include_connection_user:
            connection_payload["user"] = user_sub
        await websocket.send_text(json.dumps(connection_payload))

        from backend.services.terminal.ws_handler import handle_terminal_ws

        authorized_task = get_ws_task_for_bound_tenant(
            websocket,
            task_id=task_id,
            user_id=user_id,
        )
        await handle_terminal_ws(
            websocket,
            task_id,
            user_id,
            authorized_task=authorized_task,
        )
    except Exception as exc:
        logger.error("Terminal handler error: %s", exc, exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "terminal_error"}))
        except Exception:
            pass


async def serve_metrics_task_websocket(
    websocket: WebSocket,
    task_id: int,
    *,
    user_id: int,
    ownership_enforcer: TaskOwnershipEnforcer = enforce_ws_task_ownership,
) -> None:
    """Serve task metrics websocket channel via metrics streamer lifecycle."""
    from .connection_manager import websocket_manager

    if not await ownership_enforcer(
        websocket,
        connection_type="metrics",
        task_id=task_id,
        user_id=user_id,
        close_on_forbidden=True,
    ):
        return

    registered = await websocket_manager.register_connection(websocket, task_id)
    if not registered:
        return

    try:
        await websocket_manager.metrics_streamer.serve_metrics_websocket(websocket, task_id)
    finally:
        await websocket_manager.unregister_connection(websocket, task_id)


async def serve_vpn_status_task_websocket(
    websocket: WebSocket,
    task_id: int,
    *,
    user_id: int,
    ownership_enforcer: TaskOwnershipEnforcer = enforce_ws_task_ownership,
) -> None:
    """Serve task VPN status websocket channel with ping/pong keepalive."""
    from .connection_manager import websocket_manager

    try:
        if not await ownership_enforcer(
            websocket,
            connection_type="vpn_status",
            task_id=task_id,
            user_id=user_id,
            close_on_forbidden=True,
        ):
            return

        await websocket_manager.connect_channel(websocket, task_id, "vpn_status")
        await websocket.send_text(
            json.dumps(
                {
                    "type": "vpn_status_subscription",
                    "task_id": task_id,
                    "status": "subscribed",
                }
            )
        )

        while True:
            try:
                data = await websocket.receive_text()
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                break
    finally:
        try:
            await websocket_manager.disconnect_channel(websocket, task_id, "vpn_status")
        except Exception:
            pass
