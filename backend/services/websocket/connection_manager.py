"""WebSocket connection lifecycle manager.

Scope:
- Own websocket connection lifecycle and registries for task streams/channels.
- Apply connection gating and bookkeeping via extracted rate-limiter/streamers.
- Provide task and channel broadcast helpers used by backend wired paths.

Boundary:
- No websocket auth/ownership policy (handled by gateway and routers).
- No embedded rate-limiter or log/metrics stream implementations.
"""

import json
import asyncio
import logging
from contextlib import suppress
from typing import Dict, Set, Any, Optional
from dataclasses import dataclass, field
from fastapi import WebSocket
from datetime import datetime
from backend.core.time_utils import utc_now
from .metrics_streamer import WSMetricsStreamer
from .rate_limiter import WSRateLimiter


@dataclass
class ConnectionMetadata:
    websocket: WebSocket
    task_id: int
    client_ip: str
    connected_at: datetime = field(default_factory=utc_now)


logger = logging.getLogger("backend.services.ws_connection_manager")


class WebSocketManager:
    def __init__(self):
        # Store active connections by task_id
        self.active_connections: Dict[int, Set[WebSocket]] = {}

        # Metrics streaming ownership is delegated to dedicated streamer.
        self.metrics_streamer = WSMetricsStreamer(start_cleanup_task=self.start_cleanup_task)

        # Connection metadata and limits
        self.connection_metadata: Dict[WebSocket, ConnectionMetadata] = {}
        self.rate_limiter = WSRateLimiter()

        # Metrics tracking
        self.metrics_summary = {
            "total_connections": 0,
            "active_connections": 0,
            "messages_sent": 0,
            "avg_connection_duration": 0,
        }

        # Periodic cleanup task is lifecycle-managed (must not start at import time).
        self.cleanup_task: Optional[asyncio.Task] = None

        # Channel-based subscriptions (task_id, channel) -> websockets
        self.active_channel_connections: Dict[tuple[int, str], Set[WebSocket]] = {}

    def start_cleanup_task(self) -> None:
        """Start stale-connection cleanup loop when a running event loop exists."""
        task = self.cleanup_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("[WebSocketManager] Skipping cleanup start: no running event loop")
            return
        self.cleanup_task = loop.create_task(self._periodic_cleanup())
        logger.debug("[WebSocketManager] Cleanup task started")

    async def stop_cleanup_task(self) -> None:
        """Stop stale-connection cleanup loop if running."""
        task = self.cleanup_task
        self.cleanup_task = None
        if task is None:
            return
        if task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.debug("[WebSocketManager] Cleanup task stopped")

    async def _check_rate_limit(self, client_ip: str) -> bool:
        return await self.rate_limiter.check_rate_limit(client_ip)

    def validate_connection_limits(self, task_id: int) -> bool:
        return self.rate_limiter.validate_connection_limits(
            task_id=task_id,
            active_connections=self.metrics_summary["active_connections"],
            task_connection_count=self.get_connection_count(task_id),
        )

    def _should_allow_connection(self, task_id: int) -> bool:
        return self.rate_limiter.should_allow_connection(task_id)

    def _update_connection_metrics(self, *, on_connect: bool = False) -> None:
        if on_connect:
            self.metrics_summary["total_connections"] += 1
            self.metrics_summary["active_connections"] += 1
        else:
            self.metrics_summary["active_connections"] = max(
                0, self.metrics_summary["active_connections"] - 1
            )

    def track_connection_metadata(self, websocket: WebSocket, task_id: int, client_ip: str) -> None:
        """Store metadata about a connection for monitoring and auditing."""
        self.connection_metadata[websocket] = ConnectionMetadata(
            websocket=websocket,
            task_id=task_id,
            client_ip=client_ip,
        )
        logger.debug(f"[WebSocketManager] Tracked connection metadata for task {task_id}, IP {client_ip}.")

    def get_connection_metrics(self) -> Dict[str, Any]:
        return self.metrics_summary

    def get_connection_stats(self) -> Dict[str, Any]:
        """Return detailed stats for monitoring, including per-task connection counts and total active connections."""
        stats = {
            "tasks": {tid: len(ws_set) for tid, ws_set in self.active_connections.items()},
            "total": self.metrics_summary["active_connections"],
        }
        logger.debug(f"[WebSocketManager] Connection stats: {stats}")
        return stats

    def _calculate_connection_duration(self, websocket: WebSocket) -> float:
        meta = self.connection_metadata.get(websocket)
        if not meta:
            return 0.0
        return (utc_now() - meta.connected_at).total_seconds()

    def export_metrics_prometheus(self) -> str:
        lines = [f"websocket_{k} {v}" for k, v in self.metrics_summary.items()]
        return "\n".join(lines)

    async def cleanup_stale_connections(self) -> None:
        """Remove closed WebSocket connections from active lists and clean up resources."""
        for task_id, websockets in list(self.active_connections.items()):
            for ws in list(websockets):
                if ws.client_state.name == "DISCONNECTED":
                    logger.info(f"[WebSocketManager] Cleaning up stale connection for task {task_id}.")
                    await self.unregister_connection(ws, task_id)

        # Clean up channel connections as well
        for key, websockets in list(self.active_channel_connections.items()):
            for ws in list(websockets):
                if ws.client_state.name == "DISCONNECTED":
                    try:
                        self.active_channel_connections[key].remove(ws)
                    except KeyError:
                        pass
            if not self.active_channel_connections.get(key):
                self.active_channel_connections.pop(key, None)

    async def _periodic_cleanup(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await self.cleanup_stale_connections()
        except asyncio.CancelledError:
            return

    async def register_connection(
        self,
        websocket: WebSocket,
        task_id: int,
    ) -> bool:
        """Register websocket transport connection after gateway auth/accept."""
        self.start_cleanup_task()
        logger.debug(
            "register_connection() called with task_id=%s",
            task_id,
        )
        client_ip = websocket.client.host if websocket.client else "unknown"
        rate_limit_reserved = False

        if not await self._check_rate_limit(client_ip):
            await websocket.close(code=1013, reason="Rate limit exceeded")
            return False
        rate_limit_reserved = True

        if not self.validate_connection_limits(task_id):
            if rate_limit_reserved:
                self.rate_limiter.decrement_connection_limit(client_ip)
            await websocket.close(code=1013, reason="Connection limit reached")
            return False

        if not self._should_allow_connection(task_id):
            if rate_limit_reserved:
                self.rate_limiter.decrement_connection_limit(client_ip)
            await websocket.close(code=1013, reason="Service temporarily unavailable")
            return False

        if task_id not in self.active_connections:
            self.active_connections[task_id] = set()

        self.active_connections[task_id].add(websocket)
        self.track_connection_metadata(websocket, task_id, client_ip)

        self._update_connection_metrics(on_connect=True)

        logger.info(
            f"WebSocket connected for task {task_id}. Total connections: {len(self.active_connections[task_id])}"
        )
        return True

    async def unregister_connection(self, websocket: WebSocket, task_id: int) -> None:
        """Unregister websocket transport connection and clean up bookkeeping."""
        if task_id in self.active_connections and websocket in self.active_connections[task_id]:
            self.active_connections[task_id].remove(websocket)
            logger.info(f"WebSocket disconnected for task {task_id}")

            duration = self._calculate_connection_duration(websocket)
            prev_avg = self.metrics_summary["avg_connection_duration"]
            total = self.metrics_summary["total_connections"]
            if total:
                self.metrics_summary["avg_connection_duration"] = (
                    prev_avg * (total - 1) + duration
                ) / total

            self._update_connection_metrics(on_connect=False)

            # Decrement per-IP connection limit counter (never below 0)
            meta = self.connection_metadata.get(websocket)
            if meta and meta.client_ip in self.rate_limiter.connection_limits:
                self.rate_limiter.decrement_connection_limit(meta.client_ip)
            self.connection_metadata.pop(websocket, None)

            if not self.active_connections[task_id]:
                del self.active_connections[task_id]

        # Remove from channel groups
        for key in list(self.active_channel_connections.keys()):
            if websocket in self.active_channel_connections.get(key, set()):
                self.active_channel_connections[key].remove(websocket)
                if not self.active_channel_connections[key]:
                    del self.active_channel_connections[key]

    async def broadcast_to_task(self, task_id: int, message: Dict[str, Any]):
        """Broadcast message to all connections for a specific task."""
        active_connections = self.active_connections.get(task_id, set()).copy()
        metrics_connections = self.metrics_streamer.get_task_connections(task_id)

        if not active_connections and not metrics_connections:
            return

        # Merge task-level subscribers from transport and metrics channel paths.
        connections = active_connections | metrics_connections

        for websocket in connections:
            try:
                await websocket.send_text(json.dumps(message))
                self.metrics_summary["messages_sent"] += 1
            except Exception as e:
                logger.error(f"Error sending to WebSocket for task {task_id}: {e}")
                # Remove failed connection(s) from their owning registries.
                if websocket in active_connections:
                    await self.unregister_connection(websocket, task_id)
                if websocket in metrics_connections:
                    await self.metrics_streamer.disconnect_metrics(websocket, task_id)

    # ------- Channel subscriptions (for vpn_status, etc.) -------
    async def connect_channel(self, websocket: WebSocket, task_id: int, channel: str) -> None:
        self.start_cleanup_task()
        key = (task_id, channel)
        if key not in self.active_channel_connections:
            self.active_channel_connections[key] = set()
        self.active_channel_connections[key].add(websocket)
        logger.info(
            f"[WebSocketManager] Channel connected: task={task_id}, channel={channel}, total={len(self.active_channel_connections[key])}"
        )

    async def disconnect_channel(self, websocket: WebSocket, task_id: int, channel: str) -> None:
        key = (task_id, channel)
        conns = self.active_channel_connections.get(key)
        if conns and websocket in conns:
            conns.remove(websocket)
            if not conns:
                self.active_channel_connections.pop(key, None)
            logger.info(f"[WebSocketManager] Channel disconnected: task={task_id}, channel={channel}")

    def has_channel_subscribers(self, task_id: int, channel: str) -> bool:
        return bool(self.active_channel_connections.get((task_id, channel)))

    async def broadcast_to_task_channel(self, task_id: int, channel: str, message: Dict[str, Any]) -> None:
        key = (task_id, channel)
        conns = self.active_channel_connections.get(key)
        if not conns:
            return
        for ws in conns.copy():
            try:
                await ws.send_text(json.dumps(message))
                self.metrics_summary["messages_sent"] += 1
            except Exception:
                await self.disconnect_channel(ws, task_id, channel)

    def get_connection_count(self, task_id: int) -> int:
        """Get number of active connections for a task."""
        return len(self.active_connections.get(task_id, set()))


# Create global instance
websocket_manager = WebSocketManager()
