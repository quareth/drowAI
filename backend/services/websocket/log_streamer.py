"""WebSocket log streaming helpers.

Scope:
- Stream container startup status, log entries, and heartbeats to one websocket.
- Handle log-stream transient conditions and emit stable client-facing stream errors.

Boundary:
- No connection ownership/auth checks or websocket registration lifecycle.
- No rate limiting, circuit breaker, or channel routing responsibilities.
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
from typing import Any, Mapping

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from backend.core.time_utils import format_iso, utc_now
from backend.database import SessionLocal
from backend.models import Task
from backend.services.runtime_provider import RuntimeActorType, RuntimeOperationService

logger = logging.getLogger("backend.services.ws_log_streamer")


def _normalize_startup_progress(delegate_result: object) -> dict[str, Any]:
    """Normalize local and runner provider startup projections."""
    if not isinstance(delegate_result, Mapping):
        return {}
    progress = dict(delegate_result)
    if "container_exists" not in progress:
        container_status = str(progress.get("container_status") or "").lower()
        job_status = str(progress.get("job_status") or "").lower()
        progress["container_exists"] = container_status not in {"", "missing", "not_found"}
        if job_status == "running" or container_status == "running":
            progress.setdefault("status", "running")
            progress.setdefault("message", "Container is now running. Streaming logs...")
        elif progress.get("startup_phase") == "container_starting":
            progress.setdefault("status", "starting")
            progress.setdefault("message", "Runtime container is starting...")
    return progress


def _normalize_log_entries(delegate_result: object) -> list[dict[str, Any]]:
    """Normalize local-provider log rows and runner raw log metadata."""
    if isinstance(delegate_result, list):
        return [
            item if isinstance(item, dict) else {"message": str(item)}
            for item in delegate_result
        ]
    if not isinstance(delegate_result, Mapping):
        return []
    raw_logs = delegate_result.get("logs")
    if isinstance(raw_logs, list):
        return [
            item if isinstance(item, dict) else {"message": str(item)}
            for item in raw_logs
        ]
    if not isinstance(raw_logs, str) or not raw_logs.strip():
        return []
    entries: list[dict[str, Any]] = []
    for line in raw_logs.splitlines():
        if not line.strip():
            continue
        timestamp, sep, message = line.partition(" ")
        if sep and timestamp[:4].isdigit():
            entries.append(
                {
                    "timestamp": timestamp,
                    "service": "kali-container",
                    "level": "info",
                    "message": message,
                }
            )
        else:
            entries.append(
                {
                    "timestamp": format_iso(utc_now()),
                    "service": "kali-container",
                    "level": "info",
                    "message": line,
                }
            )
    return entries


async def _emit_provider_stream_failure(
    websocket: WebSocket,
    *,
    task_id: int,
    operation: str,
    result,
) -> None:
    """Emit deterministic stopped payloads when waited provider calls fail."""
    error_code = str(getattr(result, "error_code", None) or "RUNNER_RUNTIME_OPERATION_FAILED")
    error_message = str(
        getattr(result, "error_message", None) or "Runtime provider failed to produce a logs snapshot."
    )
    provider = str(getattr(result, "provider", "") or "")
    status = getattr(getattr(result, "status", None), "value", None) or str(
        getattr(result, "status", "failed")
    )
    await websocket.send_text(
        json.dumps(
            {
                "type": "container_status",
                "task_id": task_id,
                "status": "stopped",
                "message": "Runtime log stream stopped due to provider failure.",
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


async def stream_logs_to_client(websocket: WebSocket, task_id: int) -> None:
    """Stream real-time logs to a specific client with startup awareness."""
    try:
        emitted_log_count = 0
        recent_log_keys: set[str] = set()
        recent_log_order: deque[str] = deque()
        consecutive_not_found_count = 0
        max_not_found_attempts = 5
        container_startup_phase = True
        last_status_check = utc_now()

        while True:
            try:
                if _runner_assignment_pending(task_id=task_id):
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "container_status",
                                "task_id": task_id,
                                "status": "starting",
                                "message": "Runtime is waiting for runner assignment.",
                                "timestamp": format_iso(utc_now()),
                            }
                        )
                    )
                    await asyncio.sleep(5)
                    continue

                current_time = utc_now()
                if (current_time - last_status_check).total_seconds() >= 10:
                    progress_result = await _run_stream_operation(
                        task_id=task_id,
                        operation="get_runtime_startup_progress",
                        call=lambda provider, request: provider.get_runtime_startup_progress(request),
                        metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                    )
                    if not progress_result.ok:
                        await _emit_provider_stream_failure(
                            websocket,
                            task_id=task_id,
                            operation="get_runtime_startup_progress",
                            result=progress_result,
                        )
                        break
                    progress = _normalize_startup_progress(
                        progress_result.metadata.get("delegate_result")
                    )
                    last_status_check = current_time

                    if not progress.get("container_exists") and container_startup_phase:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "container_status",
                                    "task_id": task_id,
                                    "status": progress.get("status", "starting"),
                                    "message": progress.get("message", "Runtime startup pending"),
                                    "timestamp": progress.get("timestamp", format_iso(utc_now())),
                                    "details": {
                                        "image_exists": progress.get("image_exists"),
                                        "active_pull": progress.get("active_pull"),
                                    },
                                }
                            )
                        )
                        await asyncio.sleep(5)
                        continue
                    if progress.get("container_exists") and container_startup_phase:
                        container_startup_phase = False
                        consecutive_not_found_count = 0
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "container_status",
                                    "task_id": task_id,
                                    "status": "running",
                                    "message": "Container is now running. Streaming logs...",
                                    "timestamp": format_iso(utc_now()),
                                }
                            )
                        )

                logs_result = await _run_stream_operation(
                    task_id=task_id,
                    operation="get_runtime_logs",
                    call=lambda provider, request: provider.get_runtime_logs(request),
                    metadata={"wait_for_result": True, "wait_timeout_seconds": 5.0},
                )
                if not logs_result.ok:
                    await _emit_provider_stream_failure(
                        websocket,
                        task_id=task_id,
                        operation="get_runtime_logs",
                        result=logs_result,
                    )
                    break
                current_logs = _normalize_log_entries(logs_result.metadata.get("delegate_result"))

                if current_logs:
                    for log_entry in current_logs:
                        log_key = json.dumps(log_entry, sort_keys=True, default=str)
                        if log_key in recent_log_keys:
                            continue
                        await websocket.send_text(
                            json.dumps({"type": "log_entry", "task_id": task_id, "data": log_entry})
                        )
                        emitted_log_count += 1
                        recent_log_keys.add(log_key)
                        recent_log_order.append(log_key)
                        while len(recent_log_order) > 1000:
                            expired_key = recent_log_order.popleft()
                            recent_log_keys.discard(expired_key)
                    consecutive_not_found_count = 0

                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "heartbeat",
                            "timestamp": format_iso(utc_now()),
                            "task_id": task_id,
                            "log_count": emitted_log_count,
                            "websocket_active": True,
                            "container_startup_phase": container_startup_phase,
                        }
                    )
                )
            except Exception as e:
                error_message = str(e)
                logger.error(f"Error getting logs for task {task_id}: {e}")

                if "not found" in error_message.lower() or "no such container" in error_message.lower():
                    consecutive_not_found_count += 1

                    if container_startup_phase:
                        if consecutive_not_found_count <= max_not_found_attempts:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "container_status",
                                        "task_id": task_id,
                                        "status": "starting",
                                        "message": f"Container is starting up... (attempt {consecutive_not_found_count}/{max_not_found_attempts})",
                                        "timestamp": format_iso(utc_now()),
                                    }
                                )
                            )
                            await asyncio.sleep(3)
                            continue
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "error",
                                    "message": "Container startup is taking longer than expected. Please check Docker status.",
                                    "timestamp": format_iso(utc_now()),
                                }
                            )
                        )
                        await asyncio.sleep(10)
                        continue

                    if consecutive_not_found_count >= 3:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "container_status",
                                    "task_id": task_id,
                                    "status": "stopped",
                                    "message": "Container appears to have been stopped or removed.",
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
                                "type": "error",
                                "message": "stream_error",
                                "timestamp": format_iso(utc_now()),
                            }
                        )
                    )

            wait_time = 5 if container_startup_phase else 2
            await asyncio.sleep(wait_time)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except asyncio.CancelledError:
        logger.info(f"WebSocket streaming cancelled for task {task_id}")
    except Exception:
        logger.error("Unexpected docker log stream error for task %s", task_id, exc_info=True)


async def _run_stream_operation(task_id: int, operation: str, call, metadata: dict[str, Any] | None = None):
    """Run provider operation for an already-authorized websocket stream."""
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


def _runner_assignment_pending(*, task_id: int) -> bool:
    """Return true when a runner-mode task has not been assigned a runner yet."""
    db = SessionLocal()
    try:
        task = db.execute(select(Task).where(Task.id == int(task_id))).scalar_one_or_none()
        if task is None:
            return False
        runtime_mode = str(getattr(task, "runtime_placement_mode", "") or "").strip().lower()
        runner_id = str(getattr(task, "runner_id", "") or "").strip()
        return runtime_mode == "runner" and not runner_id
    finally:
        db.close()
