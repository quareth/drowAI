"""Run report generation workers from the backend background-service lifecycle.

This module owns report scheduler startup, shutdown, stale job recovery, and
periodic worker dispatch. It does not generate report sections or persist
report content; claimed jobs are executed by ``ReportWorker``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.database import SessionLocal
from backend.services.reporting.report_job_service import (
    ReportJobClaimLimits,
    ReportJobService,
)
from backend.services.reporting.report_worker import run_report_worker_once
from backend.services.reporting.report_worker_types import ReportWorkerRunResult

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_STALE_AFTER = timedelta(minutes=7)
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0
_DEFAULT_MAX_RECOVERY_ATTEMPTS = 3


class _DbSession(Protocol):
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class _SchedulerState:
    loop_task: asyncio.Task[None] | None = None
    active_tasks: set[asyncio.Task[ReportWorkerRunResult]] = field(default_factory=set)


class ReportScheduler:
    """Coordinate periodic report job recovery and worker claim dispatch."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], _DbSession] = SessionLocal,
        worker_runner: Callable[
            ..., Awaitable[ReportWorkerRunResult]
        ] = run_report_worker_once,
        claim_limits: ReportJobClaimLimits | None = None,
        stale_after: timedelta = _DEFAULT_STALE_AFTER,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        shutdown_grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS,
        sleep_func: Callable[[float], Awaitable[object]] = asyncio.sleep,
        instance_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._worker_runner = worker_runner
        self._claim_limits = claim_limits or ReportJobClaimLimits()
        self._stale_after = stale_after
        self._poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self._shutdown_grace_seconds = max(1.0, float(shutdown_grace_seconds))
        self._sleep_func = sleep_func
        self._instance_id = instance_id or _default_instance_id()
        self._state = _SchedulerState()

    @property
    def is_running(self) -> bool:
        """Return whether the scheduler loop is currently alive."""

        task = self._state.loop_task
        return task is not None and not task.done()

    async def start(self) -> None:
        """Start the scheduler loop once and recover stale jobs at startup."""

        task = self._state.loop_task
        if task is not None and not task.done():
            return

        await self.recover_stale_jobs(raise_on_error=True)
        self._state.loop_task = asyncio.create_task(
            self._run_loop(),
            name="report-generation-scheduler",
        )
        logger.info("Report scheduler started instance_id=%s", self._instance_id)

    async def stop(self) -> None:
        """Stop the scheduler loop and wait briefly for active workers."""

        task = self._state.loop_task
        self._state.loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._await_active_workers_with_grace()
        logger.info("Report scheduler stopped instance_id=%s", self._instance_id)

    async def recover_stale_jobs(self, *, raise_on_error: bool = False) -> None:
        """Recover stale generating jobs once using the durable job service."""

        db = self._session_factory()
        try:
            result = ReportJobService(db).recover_stale_jobs(
                now=datetime.now(UTC),
                stale_after=self._stale_after,
                max_attempts=_DEFAULT_MAX_RECOVERY_ATTEMPTS,
            )
            db.commit()
            if result.requeued or result.failed:
                logger.warning(
                    "Recovered stale report jobs requeued=%s failed=%s",
                    result.requeued,
                    result.failed,
                )
        except Exception:
            db.rollback()
            logger.exception("Report scheduler stale-job recovery failed")
            if raise_on_error:
                raise
        finally:
            db.close()

    async def dispatch_once(self) -> int:
        """Dispatch worker claim tasks up to the configured active limit."""

        before = len(self._state.active_tasks)
        self._dispatch_workers_to_limit()
        return max(0, len(self._state.active_tasks) - before)

    async def _run_loop(self) -> None:
        while True:
            self._discard_finished_workers()
            try:
                await self.recover_stale_jobs()
                await self.dispatch_once()
            except Exception:
                logger.exception("Report scheduler tick failed")
            await self._sleep_func(self._poll_interval_seconds)

    def _dispatch_workers_to_limit(self) -> None:
        self._discard_finished_workers()
        limit = max(1, int(self._claim_limits.global_limit))
        while len(self._state.active_tasks) < limit:
            task = asyncio.create_task(
                self._run_worker_claim_once(),
                name="report-generation-worker-claim",
            )
            self._state.active_tasks.add(task)
            task.add_done_callback(self._on_worker_done)

    async def _run_worker_claim_once(self) -> ReportWorkerRunResult:
        db = self._session_factory()
        try:
            result = await self._worker_runner(
                db,
                worker_id=f"report-scheduler-{self._instance_id}-{uuid.uuid4().hex[:10]}",
                claim_limits=self._claim_limits,
                stale_after=self._stale_after,
            )
            return result
        except Exception:
            db.rollback()
            logger.exception("Report scheduler worker claim failed")
            raise
        finally:
            db.close()

    def _on_worker_done(self, task: asyncio.Task[ReportWorkerRunResult]) -> None:
        self._state.active_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            return

    def _discard_finished_workers(self) -> None:
        self._state.active_tasks = {
            task for task in self._state.active_tasks if not task.done()
        }

    async def _await_active_workers_with_grace(self) -> None:
        active = [task for task in self._state.active_tasks if not task.done()]
        if not active:
            self._state.active_tasks.clear()
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in active)),
                timeout=self._shutdown_grace_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Report scheduler stop timed out waiting for %s active worker(s)",
                len(active),
            )
        except Exception:
            logger.exception("Report scheduler active worker failed during shutdown")
        finally:
            self._discard_finished_workers()


def _default_instance_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


report_scheduler = ReportScheduler()


__all__ = ["ReportScheduler", "report_scheduler"]
