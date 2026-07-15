"""WebSocket transport, gateway, channel handlers, and streaming helpers.

Selected convenience symbols are exposed lazily via ``__getattr__`` to avoid
forcing imports of cycle-prone or DB-backed submodules at package import time.
The full backward-compatibility contract remains the legacy
``backend.services.ws_<module>`` aliases — package-root exports are an
opt-in convenience surface, not a second required public API.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "WSAuthContext",
    "authorize_ws_connection",
    "enforce_ws_task_ownership",
    "WebSocketManager",
    "websocket_manager",
    "WSMetricsStreamer",
    "WSRateLimiter",
    "stream_logs_to_client",
    "WSDockerStreamSessionService",
    "ws_docker_stream_session_service",
    "AgentMultiSubscribeRequest",
    "AgentMultiUnsubscribeRequest",
    "AgentMultiControlResponse",
]


def __getattr__(name: str) -> Any:
    if name == "WSAuthContext":
        from .gateway import WSAuthContext

        return WSAuthContext
    if name == "authorize_ws_connection":
        from .gateway import authorize_ws_connection

        return authorize_ws_connection
    if name == "enforce_ws_task_ownership":
        from .gateway import enforce_ws_task_ownership

        return enforce_ws_task_ownership
    if name == "WebSocketManager":
        from .connection_manager import WebSocketManager

        return WebSocketManager
    if name == "websocket_manager":
        from .connection_manager import websocket_manager

        return websocket_manager
    if name == "WSMetricsStreamer":
        from .metrics_streamer import WSMetricsStreamer

        return WSMetricsStreamer
    if name == "WSRateLimiter":
        from .rate_limiter import WSRateLimiter

        return WSRateLimiter
    if name == "stream_logs_to_client":
        from .log_streamer import stream_logs_to_client

        return stream_logs_to_client
    if name == "WSDockerStreamSessionService":
        from .docker_stream_session import WSDockerStreamSessionService

        return WSDockerStreamSessionService
    if name == "ws_docker_stream_session_service":
        from .docker_stream_session import ws_docker_stream_session_service

        return ws_docker_stream_session_service
    if name == "AgentMultiSubscribeRequest":
        from .types import AgentMultiSubscribeRequest

        return AgentMultiSubscribeRequest
    if name == "AgentMultiUnsubscribeRequest":
        from .types import AgentMultiUnsubscribeRequest

        return AgentMultiUnsubscribeRequest
    if name == "AgentMultiControlResponse":
        from .types import AgentMultiControlResponse

        return AgentMultiControlResponse
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
