"""WebSocket metrics streaming helpers.

Scope:
- Maintain metrics websocket subscriptions and per-connection stream tasks.
- Stream task metrics/status updates and broadcast metrics updates to subscribers.

Boundary:
- No auth/ownership enforcement, websocket accept/close policy, or routing.
- No global connection lifecycle/rate-limiter/circuit-breaker responsibilities.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from backend.core.time_utils import format_iso, utc_now
from backend.database import SessionLocal
from backend.services.runtime_provider import RuntimeActorType, RuntimeOperationService
from backend.services.runtime_provider.snapshot_normalization import (
    normalize_runtime_metrics_snapshot,
    normalize_runtime_status_snapshot,
)

logger = logging.getLogger("backend.services.ws_metrics_streamer")


async def _emit_provider_stream_failure(
    websocket: WebSocket,
    *,
    task_id: int,
    operation: str,
    result,
) -> None:
    """Emit deterministic stop payloads when waited provider calls fail."""
    error_code = str(getattr(result, "error_code", None) or "RUNNER_RUNTIME_OPERATION_FAILED")
    error_message = str(
        getattr(result, "error_message", None) or "Runtime provider failed to produce a metrics snapshot."
    )
    provider = str(getattr(result, "provider", "") or "")
    status = getattr(getattr(result, "status", None), "value", None) or str(
        getattr(result, "status", "failed")
    )
    await websocket.send_text(
        json.dumps(
            {
                "type": "metrics_stopped",
                "task_id": task_id,
                "reason": error_code,
                "operation": operation,
                "provider": provider,
                "provider_status": status,
                "error_code": error_code,
                "error_message": error_message,
                "timestamp": format_iso(utc_now()),
            }
        )
    )


class WSMetricsStreamer:
    """Own task-scoped metrics websocket subscriptions and streaming."""

    def __init__(self, start_cleanup_task: Optional[Callable[[], None]] = None) -> None:
        self._start_cleanup_task = start_cleanup_task
        self.metrics_connections: Dict[int, Set[WebSocket]] = {}
        self.metrics_tasks: Dict[WebSocket, asyncio.Task[Any]] = {}

    async def _stream_metrics_to_client(self, websocket: WebSocket, task_id: int) -> None:
        """Stream real-time metrics to a specific client."""
        try:
            consecutive_not_found_count = 0
            max_not_found_attempts = 3

            while True:
                try:
                    metrics_result = await _run_metrics_operation(
                        task_id=task_id,
                        operation="get_runtime_metrics",
                        call=lambda provider, request: provider.get_runtime_metrics(request),
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                    )
                    if not metrics_result.ok:
                        await _emit_provider_stream_failure(
                            websocket,
                            task_id=task_id,
                            operation="get_runtime_metrics",
                            result=metrics_result,
                        )
                        break
                    metrics = (
                        normalize_runtime_metrics_snapshot(metrics_result.metadata.get("delegate_result"))
                    )

                    if metrics:
                        if metrics.get("status") == "not_found" or metrics.get("container_running") is False:
                            consecutive_not_found_count += 1
                            logger.debug(
                                f"Container for task {task_id} not found/running (attempt {consecutive_not_found_count})"
                            )

                            if consecutive_not_found_count >= max_not_found_attempts:
                                logger.info(
                                    f"Stopping metrics monitoring for task {task_id} - container no longer exists"
                                )
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "metrics_stopped",
                                            "task_id": task_id,
                                            "reason": "container_not_found",
                                            "timestamp": format_iso(utc_now()),
                                        }
                                    )
                                )
                                break
                        else:
                            consecutive_not_found_count = 0

                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "metrics",
                                    "task_id": task_id,
                                    "data": metrics,
                                    "timestamp": format_iso(utc_now()),
                                }
                            )
                        )
                    else:
                        status_result = await _run_metrics_operation(
                            task_id=task_id,
                            operation="get_runtime_status",
                            call=lambda provider, request: provider.get_runtime_status(request),
                            metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                        )
                        if not status_result.ok:
                            await _emit_provider_stream_failure(
                                websocket,
                                task_id=task_id,
                                operation="get_runtime_status",
                                result=status_result,
                            )
                            break
                        status_snapshot = (
                            normalize_runtime_status_snapshot(status_result.metadata.get("delegate_result"))
                        )
                        container_status = status_snapshot[1] if status_snapshot is not None else "unknown"
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "status",
                                    "task_id": task_id,
                                    "status": container_status,
                                    "timestamp": format_iso(utc_now()),
                                }
                            )
                        )

                except Exception as e:
                    logger.error(f"Error getting metrics for task {task_id}: {e}")
                    if "not running" in str(e) or "not found" in str(e):
                        consecutive_not_found_count += 1
                        if consecutive_not_found_count >= max_not_found_attempts:
                            logger.info(
                                f"Stopping metrics monitoring for task {task_id} due to persistent errors"
                            )
                            break
                    else:
                        consecutive_not_found_count = 0

                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": "stream_error",
                                "timestamp": format_iso(utc_now()),
                            }
                        )
                    )

                await asyncio.sleep(5)

        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected during metrics streaming for task {task_id}")
        except asyncio.CancelledError:
            logger.info(f"Metrics streaming cancelled for task {task_id}")
        except Exception as e:
            logger.error(f"Unexpected error in metrics streaming for task {task_id}: {e}")

    async def stream_metrics_to_client(self, websocket: WebSocket, task_id: int) -> None:
        """Public stream method used by external websocket session services."""
        await self._stream_metrics_to_client(websocket, task_id)

    async def handle_metrics_subscription(
        self,
        websocket: WebSocket,
        task_id: int,
    ) -> None:
        """Register websocket metrics subscription and start streaming task."""
        if self._start_cleanup_task is not None:
            self._start_cleanup_task()

        if task_id not in self.metrics_connections:
            self.metrics_connections[task_id] = set()

        self.metrics_connections[task_id].add(websocket)
        logger.info(
            f"Metrics WebSocket connected for task {task_id}. Total connections: {len(self.metrics_connections[task_id])}"
        )

        task = asyncio.create_task(self._stream_metrics_to_client(websocket, task_id))
        self.metrics_tasks[websocket] = task

    async def broadcast_metrics_update(self, task_id: int, metrics: Dict[str, Any]) -> None:
        """Broadcast metrics update payload to all subscribers."""
        if task_id not in self.metrics_connections:
            return

        connections = self.metrics_connections[task_id].copy()
        for websocket in connections:
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "metrics_update",
                            "task_id": task_id,
                            "metrics": metrics,
                        }
                    )
                )
            except Exception as e:
                logger.error(f"Error sending metrics to WebSocket for task {task_id}: {e}")
                await self.disconnect_metrics(websocket, task_id)

    async def disconnect_metrics(self, websocket: WebSocket, task_id: int) -> None:
        """Disconnect websocket from task metrics stream and cancel stream task."""
        if task_id in self.metrics_connections and websocket in self.metrics_connections[task_id]:
            self.metrics_connections[task_id].remove(websocket)

            if websocket in self.metrics_tasks:
                self.metrics_tasks[websocket].cancel()
                del self.metrics_tasks[websocket]

            if not self.metrics_connections[task_id]:
                del self.metrics_connections[task_id]

    def get_task_connections(self, task_id: int) -> Set[WebSocket]:
        """Return a copy of current metrics subscribers for a task."""
        return self.metrics_connections.get(task_id, set()).copy()

    async def serve_metrics_websocket(self, websocket: WebSocket, task_id: int) -> None:
        """Run the full metrics websocket lifecycle for one task subscription."""
        await self.handle_metrics_subscription(websocket, task_id)
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            return
        except Exception:
            logger.error("Metrics websocket lifecycle error for task %s", task_id, exc_info=True)
        finally:
            await self.disconnect_metrics(websocket, task_id)


async def _run_metrics_operation(
    task_id: int,
    operation: str,
    call,
    metadata: dict[str, Any] | None = None,
):
    """Run provider operation for an already-authorized metrics stream."""
    db = SessionLocal()
    try:
        runtime_operations = RuntimeOperationService(db)
        context = runtime_operations.context_for_internal_task(
            task_id=task_id,
            actor_type=RuntimeActorType.SYSTEM,
            actor_id=f"websocket:{operation}",
        )
        return await runtime_operations.run_for_context(
            context=context,
            operation=operation,
            call=call,
            metadata=metadata,
        )
    finally:
        db.close()
