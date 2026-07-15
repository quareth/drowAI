"""Lifecycle manager for backend background services after setup is complete.

This module owns idempotent startup and shutdown for services that should not
run while the first-run setup wizard is pending.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from backend.config import AGENT_REASONING_MOCK_MODE
from backend.database import SessionLocal
from backend.services.cve_indexing.runtime import cve_sync_scheduler
from backend.services.metrics import metrics
from backend.services.retention import cleanup_agent_logs
from backend.services.reporting.report_scheduler import report_scheduler
from backend.services.terminal_session_manager import terminal_session_manager

logger = logging.getLogger(__name__)


@dataclass
class _BackgroundServiceState:
    started: bool = False
    retention_task: asyncio.Task[None] | None = None


_state = _BackgroundServiceState()
_start_lock = asyncio.Lock()


async def start_background_services() -> bool:
    """Start post-setup background services once for this backend process."""
    async with _start_lock:
        if _state.started:
            if not report_scheduler.is_running:
                logger.warning(
                    "Report scheduler is not running; repairing background service state"
                )
                await report_scheduler.start()
            return report_scheduler.is_running

        try:
            await metrics.start()
            await cve_sync_scheduler.start()
            await report_scheduler.start()
            terminal_session_manager.start()

            from backend.services.websocket.connection_manager import websocket_manager

            websocket_manager.start_cleanup_task()

            if AGENT_REASONING_MOCK_MODE:
                logger.info(
                    "[MOCK MODE] Agent reasoning mock mode is ENABLED - "
                    "No API tokens will be consumed"
                )
            else:
                logger.info(
                    "[REAL MODE] Agent reasoning mock mode is DISABLED - "
                    "Real AI reasoning will execute"
                )

            _state.retention_task = asyncio.create_task(
                _retention_loop(),
                name="agent-log-retention",
            )
            _state.started = True
            logger.info("Backend background services started")
            return report_scheduler.is_running
        except Exception:
            logger.exception("Failed to start backend background services")
            await stop_background_services(force=True)
            raise


async def stop_background_services(*, force: bool = False) -> bool:
    """Stop background services if they were started in this process."""
    if (
        not _state.started
        and _state.retention_task is None
        and not report_scheduler.is_running
        and not force
    ):
        return False

    task = _state.retention_task
    _state.retention_task = None
    _state.started = False

    await metrics.stop()
    await cve_sync_scheduler.stop()
    await report_scheduler.stop()
    await terminal_session_manager.cleanup_all_sessions()

    from backend.services.websocket.connection_manager import websocket_manager

    await websocket_manager.stop_cleanup_task()

    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Backend background services stopped")
    return True


def background_service_status() -> dict[str, bool]:
    """Return non-secret process-local background service health."""

    return {
        "background_services_started": bool(_state.started),
        "report_scheduler_running": report_scheduler.is_running,
    }


async def _retention_loop() -> None:
    while True:
        try:
            await asyncio.sleep(3600)
            db = SessionLocal()
            try:
                deleted = cleanup_agent_logs(db)
                if deleted:
                    logger.info(
                        "[retention] finalized %s expired retention items", deleted
                    )
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.error("retention loop error", exc_info=True)
