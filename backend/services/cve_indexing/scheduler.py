"""CVE sync scheduler with durable lease ownership, heartbeat, and stale-run recovery.

Scope:
- Exposes startup/shutdown lifecycle hooks for backend wiring.
- Dispatches automatic and manual runs using DB-backed exclusivity.
- Maintains lease heartbeat while runs execute and reconciles orphaned running state.

Boundary:
- Owns scheduling/dispatch orchestration only; sync business logic remains in sync service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from backend.database import SessionLocal
from backend.models.cve import CveIndexSettings, CveIndexState
from backend.services.cve_indexing.contracts import CveSyncTriggerKind
from backend.services.cve_indexing.lease_service import (
    CveLeaseClaimResult,
    CveLeaseRecoveryResult,
    CveSyncLeaseService,
    ORPHANED_RUN_RECOVERY_ERROR,
    SHUTDOWN_RECOVERY_ERROR,
)
from backend.services.cve_indexing.primitives import normalize_hour, to_utc, utc_now
from backend.services.cve_indexing.scheduler_policy import is_daily_sync_due
from backend.services.cve_indexing.sync_service import CveSyncService
from backend.services.metrics import metrics

logger = logging.getLogger(__name__)
_PROCESS_SYNC_LOCK = threading.Lock()
_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_DEFAULT_LEASE_TTL_SECONDS = 60
_DEFAULT_LEASE_HEARTBEAT_SECONDS = 10
_DEFAULT_PROGRESS_STALE_SECONDS = 300
_DEFAULT_MAX_RUN_DURATION_SECONDS = 3600
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0


class _DbSession(Protocol):
    def query(self, *entities): ...  # noqa: ANN002, ANN003
    def close(self) -> None: ...


class _ProcessLock(Protocol):
    def acquire(self, blocking: bool = True) -> bool: ...
    def release(self) -> None: ...


@dataclass(slots=True)
class _RunFinalizer:
    """Idempotent per-run cleanup guard for lease/process-lock release."""

    _finalized: bool = False

    def mark_finalized(self) -> bool:
        """Return True only for the first finalization call."""
        if self._finalized:
            return False
        self._finalized = True
        return True


@dataclass(slots=True, frozen=True)
class CveSchedulerSnapshot:
    """Config and state values used for one scheduler due decision."""

    enabled: bool
    daily_sync_hour_utc: int
    last_successful_sync_at: datetime | None


@dataclass(slots=True, frozen=True)
class CveSyncDispatchResult:
    """Result payload for a scheduler dispatch attempt."""

    queued: bool
    dispatched: bool
    reason: str | None = None
    active_run_id: int | None = None
    run_id: int | None = None
    owner_id: str | None = None

    def to_api_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "queued": self.queued,
            "dispatched": self.dispatched,
            "active_run_id": self.active_run_id,
            "run_id": self.run_id,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


class CveSyncScheduler:
    """CVE scheduler that coordinates durable run ownership and periodic dispatch."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], _DbSession] = SessionLocal,
        sync_runner: Callable[[CveSyncTriggerKind, str, int], int | None] | None = None,
        process_lock: _ProcessLock | None = None,
        settings_snapshot_provider: Callable[[], CveSchedulerSnapshot] | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        sleep_func: Callable[[float], asyncio.Future] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        instance_id: str | None = None,
        lease_ttl_seconds: int | None = None,
        lease_heartbeat_seconds: int | None = None,
        shutdown_grace_seconds: float = _DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._sync_runner = sync_runner or self._run_sync_blocking
        self._process_lock = process_lock or _PROCESS_SYNC_LOCK
        self._settings_snapshot_provider = settings_snapshot_provider or self._load_settings_snapshot
        self._poll_interval_seconds = max(1.0, float(poll_interval_seconds))
        self._sleep_func = sleep_func or asyncio.sleep
        self._now_provider = now_provider or _utc_now
        self._instance_id = instance_id or _default_instance_id()
        self._lease_ttl_seconds = _env_int(
            "CVE_SYNC_LEASE_TTL_SECONDS",
            lease_ttl_seconds or _DEFAULT_LEASE_TTL_SECONDS,
            minimum=1,
        )
        self._lease_heartbeat_seconds = _env_int(
            "CVE_SYNC_LEASE_HEARTBEAT_SECONDS",
            lease_heartbeat_seconds or _DEFAULT_LEASE_HEARTBEAT_SECONDS,
            minimum=1,
        )
        self._max_run_duration_seconds = _env_int(
            "CVE_SYNC_MAX_RUN_DURATION_SECONDS",
            _DEFAULT_MAX_RUN_DURATION_SECONDS,
            minimum=1,
        )
        self._progress_stale_seconds = _env_int(
            "CVE_SYNC_PROGRESS_STALE_SECONDS",
            _DEFAULT_PROGRESS_STALE_SECONDS,
            minimum=1,
        )
        self._shutdown_grace_seconds = float(
            max(
                1.0,
                _env_float(
                    "CVE_SYNC_SHUTDOWN_GRACE_SECONDS",
                    shutdown_grace_seconds,
                    minimum=1.0,
                ),
            )
        )
        self._loop_task: asyncio.Task | None = None
        self._active_runs: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """Start scheduler loop and run startup stale-run recovery."""
        if self._loop_task is not None and not self._loop_task.done():
            return

        recovered = await self.recover_stale_runs(reason=ORPHANED_RUN_RECOVERY_ERROR)
        if recovered:
            logger.warning("Recovered %s orphaned CVE sync run(s) during startup.", recovered)
        self._loop_task = asyncio.create_task(self._run_loop(), name="cve-sync-scheduler")

    async def stop(self) -> None:
        """Stop scheduler loop and wait for active runs with bounded grace period."""
        task = self._loop_task
        self._loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._await_active_runs_with_grace()

    async def recover_stale_runs(self, *, reason: str = ORPHANED_RUN_RECOVERY_ERROR) -> int:
        """Recover orphaned running state once and return count of recovered runs."""
        result = await asyncio.to_thread(self._recover_once_blocking, reason)
        if not result.recovered:
            return 0
        metrics.inc("cve.sync.recovered_stale_runs")
        logger.warning(
            "Recovered orphaned CVE sync run state: reason=%s run_id=%s",
            result.reason,
            result.run_id,
        )
        return 1

    async def dispatch_sync_once(
        self,
        *,
        trigger_kind: CveSyncTriggerKind = CveSyncTriggerKind.SYSTEM,
    ) -> CveSyncDispatchResult:
        """Attempt to claim and dispatch one run asynchronously."""
        await self.recover_stale_runs(reason=ORPHANED_RUN_RECOVERY_ERROR)
        metrics.inc("cve.sync.dispatch_attempts")
        owner_id = f"{self._instance_id}:{uuid.uuid4().hex[:10]}"
        if not self._process_lock.acquire(blocking=False):
            metrics.inc("cve.sync.claim_failures")
            active_run_id = await asyncio.to_thread(self._active_run_id_blocking)
            return CveSyncDispatchResult(
                queued=False,
                dispatched=False,
                reason="in_process_running",
                active_run_id=active_run_id,
            )

        claim = await asyncio.to_thread(self._claim_blocking, owner_id)
        if not claim.claimed:
            self._safe_release_process_lock()
            metrics.inc("cve.sync.claim_failures")
            return CveSyncDispatchResult(
                queued=False,
                dispatched=False,
                reason=claim.reason,
                active_run_id=claim.active_run_id,
            )

        try:
            run_task = asyncio.create_task(
                self._execute_claimed_run(
                    trigger_kind=trigger_kind,
                    owner_id=owner_id,
                ),
                name=f"cve-sync-run-{trigger_kind.value}",
            )
        except Exception:
            await asyncio.to_thread(self._release_blocking, owner_id)
            self._safe_release_process_lock()
            raise
        self._active_runs[owner_id] = run_task
        run_task.add_done_callback(lambda _task, run_owner_id=owner_id: self._on_run_task_done(run_owner_id))
        logger.info(
            "CVE sync dispatched: trigger=%s owner=%s",
            trigger_kind.value,
            owner_id,
        )
        return CveSyncDispatchResult(
            queued=True,
            dispatched=True,
            owner_id=owner_id,
            active_run_id=claim.active_run_id,
        )

    async def run_sync_once(
        self,
        *,
        trigger_kind: CveSyncTriggerKind = CveSyncTriggerKind.SYSTEM,
    ) -> bool:
        """Compatibility method: dispatch one run and wait for completion."""
        dispatch = await self.dispatch_sync_once(trigger_kind=trigger_kind)
        if not dispatch.dispatched or not dispatch.owner_id:
            return False
        run_task = self._active_runs.get(dispatch.owner_id)
        if run_task is not None:
            await run_task
        return True

    async def cancel_active_run(self) -> CveSyncDispatchResult:
        """Cancel any active sync run, clear DB lease, and release process lock."""
        recovery = await asyncio.to_thread(self._force_clear_all_blocking)

        pending_tasks: list[asyncio.Task] = []
        for owner_id, task in list(self._active_runs.items()):
            if task.done():
                self._active_runs.pop(owner_id, None)
                continue
            task.cancel()
            pending_tasks.append(task)
            self._active_runs.pop(owner_id, None)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._safe_release_process_lock()

        cancelled = recovery.recovered or bool(pending_tasks)
        return CveSyncDispatchResult(
            queued=False,
            dispatched=cancelled,
            reason=(recovery.reason or ("cancelled" if cancelled else "no_active_run")),
            run_id=recovery.run_id,
        )

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("CVE scheduler tick failed", exc_info=True)
            await self._sleep_func(self._poll_interval_seconds)

    async def _tick(self) -> bool:
        await self.recover_stale_runs(reason=ORPHANED_RUN_RECOVERY_ERROR)
        snapshot = self._settings_snapshot_provider()
        if not self._should_attempt_automatic_sync(snapshot):
            return False
        dispatch = await self.dispatch_sync_once(trigger_kind=CveSyncTriggerKind.SCHEDULE)
        return dispatch.dispatched

    def _should_attempt_automatic_sync(self, snapshot: CveSchedulerSnapshot) -> bool:
        if not snapshot.enabled:
            return False
        return is_daily_sync_due(
            last_successful_sync_at=snapshot.last_successful_sync_at,
            daily_sync_hour_utc=snapshot.daily_sync_hour_utc,
            now=self._now_provider(),
        )

    async def _execute_claimed_run(
        self,
        *,
        trigger_kind: CveSyncTriggerKind,
        owner_id: str,
    ) -> None:
        finalizer = _RunFinalizer()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(owner_id=owner_id),
            name=f"cve-sync-heartbeat-{trigger_kind.value}",
        )
        started_at = time.monotonic()
        run_id: int | None = None
        try:
            run_id = await asyncio.wait_for(
                asyncio.to_thread(
                    self._sync_runner,
                    trigger_kind,
                    owner_id,
                    self._lease_ttl_seconds,
                ),
                timeout=float(self._max_run_duration_seconds),
            )
            metrics.inc("cve.sync.run_succeeded")
            logger.info(
                "CVE sync run completed: trigger=%s owner=%s run_id=%s",
                trigger_kind.value,
                owner_id,
                run_id,
            )
        except asyncio.TimeoutError:
            metrics.inc("cve.sync.run_timeout")
            metrics.inc("cve.sync.run_failures")
            logger.warning(
                "CVE sync run timed out: trigger=%s owner=%s timeout_seconds=%s",
                trigger_kind.value,
                owner_id,
                self._max_run_duration_seconds,
            )
        except Exception:
            metrics.inc("cve.sync.run_failures")
            logger.error(
                "CVE sync run failed: trigger=%s owner=%s",
                trigger_kind.value,
                owner_id,
                exc_info=True,
            )
        finally:
            await self._finalize_run_once(
                owner_id=owner_id,
                heartbeat_task=heartbeat_task,
                started_at=started_at,
                run_id=run_id,
                finalizer=finalizer,
            )

    async def _heartbeat_loop(self, *, owner_id: str) -> None:
        try:
            last_progress_at = await asyncio.to_thread(self._progress_updated_at_blocking)
        except Exception:
            last_progress_at = None
        last_progress_change_monotonic = time.monotonic()
        while True:
            await self._sleep_func(float(self._lease_heartbeat_seconds))
            try:
                current_progress_at = await asyncio.to_thread(self._progress_updated_at_blocking)
            except Exception:
                current_progress_at = None
            if current_progress_at != last_progress_at:
                last_progress_at = current_progress_at
                last_progress_change_monotonic = time.monotonic()
            elif (time.monotonic() - last_progress_change_monotonic) > float(
                self._progress_stale_seconds
            ):
                logger.warning(
                    "CVE sync heartbeat stopping due to stale progress: owner=%s",
                    owner_id,
                )
                return
            try:
                refreshed = await asyncio.to_thread(self._refresh_blocking, owner_id)
            except Exception:
                metrics.inc("cve.sync.heartbeat_errors")
                logger.error(
                    "CVE sync lease heartbeat failed: owner=%s",
                    owner_id,
                    exc_info=True,
                )
                return
            if not refreshed:
                metrics.inc("cve.sync.heartbeat_miss")
                logger.warning("CVE sync lease heartbeat lost ownership: owner=%s", owner_id)
                return
            metrics.inc("cve.sync.heartbeat_ok")

    async def _finalize_run_once(
        self,
        *,
        owner_id: str,
        heartbeat_task: asyncio.Task,
        started_at: float,
        run_id: int | None,
        finalizer: _RunFinalizer,
    ) -> None:
        """Finalize one run exactly once and never skip later cleanup steps."""
        if not finalizer.mark_finalized():
            return

        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning(
                "CVE heartbeat task ended with error during finalization: owner=%s",
                owner_id,
                exc_info=True,
            )

        try:
            await asyncio.to_thread(self._release_blocking, owner_id)
        except Exception:
            logger.error(
                "CVE sync lease release failed during finalization: owner=%s",
                owner_id,
                exc_info=True,
            )
        finally:
            self._safe_release_process_lock()
            duration = max(0.0, time.monotonic() - started_at)
            metrics.gauge("cve.sync.run_duration_seconds", duration)
            metrics.gauge("cve.sync.last_run_id", float(run_id or 0))

    async def _await_active_runs_with_grace(self) -> None:
        pending = [
            (owner_id, task)
            for owner_id, task in list(self._active_runs.items())
            if not task.done()
        ]
        if not pending:
            return

        pending_tasks = [task for _, task in pending]
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending_tasks, return_exceptions=True),
                timeout=self._shutdown_grace_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "CVE scheduler stop timed out waiting for %s active run(s); forcing reconciliation.",
                len(pending),
            )
            metrics.inc("cve.sync.shutdown_force_recovery", len(pending))
            for owner_id, task in pending:
                await asyncio.to_thread(
                    self._force_fail_owner_blocking,
                    owner_id,
                    SHUTDOWN_RECOVERY_ERROR,
                )
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    def _on_run_task_done(self, owner_id: str) -> None:
        self._active_runs.pop(owner_id, None)

    def _load_settings_snapshot(self) -> CveSchedulerSnapshot:
        db = self._session_factory()
        try:
            settings = db.query(CveIndexSettings).order_by(CveIndexSettings.id.asc()).first()
            state = db.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
            if settings is None:
                return CveSchedulerSnapshot(
                    enabled=False,
                    daily_sync_hour_utc=2,
                    last_successful_sync_at=(
                        to_utc(state.last_successful_sync_at)
                        if state is not None and state.last_successful_sync_at is not None
                        else None
                    ),
                )
            return CveSchedulerSnapshot(
                enabled=bool(settings.enabled),
                daily_sync_hour_utc=normalize_hour(getattr(settings, "daily_sync_hour_utc", 2)),
                last_successful_sync_at=(
                    to_utc(state.last_successful_sync_at)
                    if state is not None and state.last_successful_sync_at is not None
                    else None
                ),
            )
        finally:
            db.close()

    def _claim_blocking(self, owner_id: str) -> CveLeaseClaimResult:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).claim(
                owner_id=owner_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
        finally:
            db.close()

    def _refresh_blocking(self, owner_id: str) -> bool:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).refresh(
                owner_id=owner_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
        finally:
            db.close()

    def _release_blocking(self, owner_id: str) -> bool:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).release(owner_id=owner_id)
        finally:
            db.close()

    def _recover_once_blocking(self, reason: str) -> CveLeaseRecoveryResult:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).recover_orphaned_running(recovery_error=reason)
        finally:
            db.close()

    def _force_fail_owner_blocking(self, owner_id: str, reason: str) -> CveLeaseRecoveryResult:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).force_fail_owner_run(
                owner_id=owner_id,
                recovery_error=reason,
            )
        finally:
            db.close()

    def _force_clear_all_blocking(self) -> CveLeaseRecoveryResult:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).force_clear_all()
        finally:
            db.close()

    def _active_run_id_blocking(self) -> int | None:
        db = self._session_factory()
        try:
            return CveSyncLeaseService(db).current_active_run_id()
        finally:
            db.close()

    def _progress_updated_at_blocking(self) -> datetime | None:
        db = self._session_factory()
        try:
            state = db.query(CveIndexState).order_by(CveIndexState.id.asc()).first()
            if state is None or state.progress_updated_at is None:
                return None
            return to_utc(state.progress_updated_at)
        finally:
            db.close()

    def _run_sync_blocking(
        self,
        trigger_kind: CveSyncTriggerKind,
        owner_id: str,
        lease_ttl_seconds: int,
    ) -> int | None:
        db = self._session_factory()
        try:
            run = CveSyncService(db).run_sync_with_lease(
                trigger_kind=trigger_kind,
                owner_id=owner_id,
                lease_ttl_seconds=lease_ttl_seconds,
            )
            return int(run.id)
        finally:
            db.close()

    def _safe_release_process_lock(self) -> None:
        try:
            self._process_lock.release()
        except Exception:
            return

def _utc_now() -> datetime:
    return utc_now()


def _default_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(minimum, int(default))
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, int(default))


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return max(minimum, float(default))
    try:
        return max(minimum, float(raw))
    except ValueError:
        return max(minimum, float(default))

__all__ = [
    "CveSchedulerSnapshot",
    "CveSyncDispatchResult",
    "CveSyncScheduler",
]
