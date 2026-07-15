"""Tests for CVE scheduler dispatch semantics, due checks, and shutdown handling."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import backend.services.cve_indexing.scheduler as scheduler_module
from backend.services.cve_indexing.contracts import CveSyncTriggerKind
from backend.services.cve_indexing.lease_service import CveLeaseClaimResult, CveLeaseRecoveryResult
from backend.services.cve_indexing.scheduler import CveSchedulerSnapshot, CveSyncScheduler


class _CountingLock:
    """Simple lock double used to assert acquire/release call counts."""

    def __init__(self) -> None:
        self._locked = False
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, blocking: bool = True) -> bool:  # noqa: ARG002
        self.acquire_calls += 1
        if self._locked:
            return False
        self._locked = True
        return True

    def release(self) -> None:
        self.release_calls += 1
        self._locked = False


def _wire_lease_success(scheduler: CveSyncScheduler, *, claim_active_run_id: int | None = None) -> None:
    scheduler._claim_blocking = lambda owner_id: CveLeaseClaimResult(  # type: ignore[method-assign]
        claimed=True,
        owner_id=owner_id,
        active_run_id=claim_active_run_id,
    )
    scheduler._refresh_blocking = lambda owner_id: True  # type: ignore[method-assign]
    scheduler._release_blocking = lambda owner_id: True  # type: ignore[method-assign]
    scheduler._recover_once_blocking = lambda reason: CveLeaseRecoveryResult(recovered=False)  # type: ignore[method-assign]
    scheduler._active_run_id_blocking = lambda: None  # type: ignore[method-assign]


def test_run_sync_once_uses_to_thread(monkeypatch) -> None:
    calls: list[tuple[CveSyncTriggerKind, str, int]] = []
    to_thread_calls: list[tuple] = []

    def _runner(trigger_kind: CveSyncTriggerKind, owner_id: str, lease_ttl_seconds: int) -> int:
        calls.append((trigger_kind, owner_id, lease_ttl_seconds))
        return 321

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=_runner, process_lock=threading.Lock())
    _wire_lease_success(scheduler)

    dispatched = asyncio.run(
        scheduler.run_sync_once(trigger_kind=CveSyncTriggerKind.MANUAL)
    )

    assert dispatched is True
    assert calls and calls[0][0] == CveSyncTriggerKind.MANUAL
    assert len(to_thread_calls) >= 2  # claim + sync runner


def test_dispatch_sync_once_returns_false_when_claim_fails(monkeypatch) -> None:
    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=lambda *_args: 1, process_lock=threading.Lock())
    scheduler._claim_blocking = lambda owner_id: CveLeaseClaimResult(  # type: ignore[method-assign]
        claimed=False,
        owner_id=owner_id,
        reason="already_running",
        active_run_id=77,
    )
    scheduler._active_run_id_blocking = lambda: 77  # type: ignore[method-assign]

    dispatch = asyncio.run(
        scheduler.dispatch_sync_once(trigger_kind=CveSyncTriggerKind.MANUAL)
    )

    assert dispatch.dispatched is False
    assert dispatch.queued is False
    assert dispatch.reason == "already_running"
    assert dispatch.active_run_id == 77


def test_tick_dispatches_when_automatic_mode_is_due(monkeypatch) -> None:
    now = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    started = threading.Event()

    def _runner(_kind: CveSyncTriggerKind, _owner_id: str, _ttl_seconds: int) -> int:
        started.set()
        return 555

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(
        sync_runner=_runner,
        process_lock=threading.Lock(),
        settings_snapshot_provider=lambda: CveSchedulerSnapshot(
            enabled=True,
            daily_sync_hour_utc=11,
            last_successful_sync_at=now - timedelta(days=1),
        ),
        now_provider=lambda: now,
    )
    _wire_lease_success(scheduler)

    async def _exercise() -> bool:
        dispatched = await scheduler._tick()
        await asyncio.to_thread(started.wait, 1)
        await scheduler.stop()
        return dispatched

    dispatched = asyncio.run(_exercise())

    assert dispatched is True


def test_stop_forces_recovery_when_active_runs_exceed_grace_period(monkeypatch) -> None:
    scheduler = CveSyncScheduler(
        sync_runner=lambda *_args: 1,
        process_lock=threading.Lock(),
        shutdown_grace_seconds=1,
    )
    forced_owner_ids: list[str] = []
    scheduler._force_fail_owner_blocking = (  # type: ignore[method-assign]
        lambda owner_id, reason: forced_owner_ids.append(owner_id) or CveLeaseRecoveryResult(recovered=True, reason=reason)
    )

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    async def _exercise() -> None:
        task = asyncio.create_task(_never_finishes())
        scheduler._active_runs["owner-1"] = task
        scheduler._shutdown_grace_seconds = 0.01
        await scheduler._await_active_runs_with_grace()

    asyncio.run(_exercise())
    assert forced_owner_ids == ["owner-1"]


def test_run_finalization_releases_lease_and_lock_once_when_heartbeat_errors(monkeypatch) -> None:
    lock = _CountingLock()
    release_calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        # Yield once so the heartbeat task executes before sync returns.
        await asyncio.sleep(0)
        return func(*args, **kwargs)

    def _runner(_kind: CveSyncTriggerKind, _owner_id: str, _ttl_seconds: int) -> int:
        return 404

    async def _failing_heartbeat(*, owner_id: str) -> None:  # noqa: ARG001
        raise RuntimeError("heartbeat failed")

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=_runner, process_lock=lock)
    scheduler._recover_once_blocking = lambda reason: CveLeaseRecoveryResult(recovered=False)  # type: ignore[method-assign]
    scheduler._claim_blocking = lambda owner_id: CveLeaseClaimResult(  # type: ignore[method-assign]
        claimed=True,
        owner_id=owner_id,
        active_run_id=None,
    )
    def _release(_owner_id: str) -> bool:
        release_calls["count"] += 1
        return True

    scheduler._release_blocking = _release  # type: ignore[method-assign]
    scheduler._active_run_id_blocking = lambda: None  # type: ignore[method-assign]
    scheduler._heartbeat_loop = _failing_heartbeat  # type: ignore[method-assign]

    dispatched = asyncio.run(scheduler.run_sync_once(trigger_kind=CveSyncTriggerKind.MANUAL))

    assert dispatched is True
    assert release_calls["count"] == 1
    assert lock.release_calls == 1


def test_heartbeat_loop_handles_refresh_errors_without_bubbling(monkeypatch) -> None:
    async def _failing_to_thread(func, *args, **kwargs):  # noqa: ANN001, ARG001
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(asyncio, "to_thread", _failing_to_thread)
    scheduler = CveSyncScheduler(
        sync_runner=lambda *_args: 1,
        process_lock=threading.Lock(),
        sleep_func=lambda _seconds: asyncio.sleep(0),
    )

    # Should exit cleanly after recording heartbeat error metric.
    asyncio.run(scheduler._heartbeat_loop(owner_id="owner-1"))


def test_finalize_run_once_is_idempotent(monkeypatch) -> None:
    lock = _CountingLock()
    release_calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    def _release(_owner_id: str) -> bool:
        release_calls["count"] += 1
        return True

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=lambda *_args: 1, process_lock=lock)
    scheduler._release_blocking = _release  # type: ignore[method-assign]

    async def _exercise() -> None:
        heartbeat_task = asyncio.create_task(asyncio.sleep(3600))
        finalizer = scheduler_module._RunFinalizer()
        await scheduler._finalize_run_once(
            owner_id="owner-1",
            heartbeat_task=heartbeat_task,
            started_at=0.0,
            run_id=None,
            finalizer=finalizer,
        )
        await scheduler._finalize_run_once(
            owner_id="owner-1",
            heartbeat_task=heartbeat_task,
            started_at=0.0,
            run_id=None,
            finalizer=finalizer,
        )

    asyncio.run(_exercise())

    assert release_calls["count"] == 1
    assert lock.release_calls == 1


def test_cancel_active_run_clears_db_lease(monkeypatch) -> None:
    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=lambda *_args: 1, process_lock=threading.Lock())
    scheduler._force_clear_all_blocking = lambda: CveLeaseRecoveryResult(  # type: ignore[method-assign]
        recovered=True,
        reason="force_cleared",
        run_id=77,
    )

    result = asyncio.run(scheduler.cancel_active_run())

    assert result.dispatched is True
    assert result.queued is False
    assert result.reason == "force_cleared"
    assert result.run_id == 77


def test_cancel_active_run_releases_process_lock(monkeypatch) -> None:
    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    lock = _CountingLock()
    lock.acquire()
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=lambda *_args: 1, process_lock=lock)
    scheduler._force_clear_all_blocking = lambda: CveLeaseRecoveryResult(  # type: ignore[method-assign]
        recovered=True,
        reason="force_cleared",
    )

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    async def _exercise() -> CveSyncDispatchResult:
        task = asyncio.create_task(_never_finishes())
        scheduler._active_runs["owner-1"] = task
        result = await scheduler.cancel_active_run()
        return result

    result = asyncio.run(_exercise())

    assert result.dispatched is True
    assert lock.release_calls == 1
    assert scheduler._active_runs == {}


def test_cancel_when_no_active_run(monkeypatch) -> None:
    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(sync_runner=lambda *_args: 1, process_lock=threading.Lock())
    scheduler._force_clear_all_blocking = lambda: CveLeaseRecoveryResult(  # type: ignore[method-assign]
        recovered=False,
        reason="not_running",
    )

    result = asyncio.run(scheduler.cancel_active_run())

    assert result.dispatched is False
    assert result.queued is False
    assert result.reason == "not_running"


def test_heartbeat_stops_when_no_progress(monkeypatch) -> None:
    refresh_calls = {"count": 0}
    fixed_progress = datetime(2026, 3, 15, 12, tzinfo=UTC)

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(
        sync_runner=lambda *_args: 1,
        process_lock=threading.Lock(),
        sleep_func=lambda _seconds: asyncio.sleep(0),
    )
    scheduler._progress_stale_seconds = -1
    scheduler._progress_updated_at_blocking = lambda: fixed_progress  # type: ignore[method-assign]
    scheduler._refresh_blocking = lambda _owner_id: refresh_calls.__setitem__("count", refresh_calls["count"] + 1) or True  # type: ignore[method-assign]

    asyncio.run(scheduler._heartbeat_loop(owner_id="owner-1"))

    assert refresh_calls["count"] == 0


def test_heartbeat_continues_when_progress_advancing(monkeypatch) -> None:
    progress_values = iter(
        [
            datetime(2026, 3, 15, 12, 0, tzinfo=UTC),
            datetime(2026, 3, 15, 12, 0, 5, tzinfo=UTC),
            datetime(2026, 3, 15, 12, 0, 10, tzinfo=UTC),
        ]
    )
    refresh_values = iter([True, False])
    refresh_calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    scheduler = CveSyncScheduler(
        sync_runner=lambda *_args: 1,
        process_lock=threading.Lock(),
        sleep_func=lambda _seconds: asyncio.sleep(0),
    )
    scheduler._progress_stale_seconds = 5
    scheduler._progress_updated_at_blocking = lambda: next(progress_values)  # type: ignore[method-assign]

    def _refresh(_owner_id: str) -> bool:
        refresh_calls["count"] += 1
        return next(refresh_values)

    scheduler._refresh_blocking = _refresh  # type: ignore[method-assign]

    asyncio.run(scheduler._heartbeat_loop(owner_id="owner-1"))

    assert refresh_calls["count"] == 2


def test_max_run_duration_timeout_marks_failed(monkeypatch) -> None:
    lock = _CountingLock()
    release_calls = {"count": 0}

    async def _fake_to_thread(func, *args, **kwargs):  # noqa: ANN001
        return func(*args, **kwargs)

    async def _fake_wait_for(awaitable, timeout):  # noqa: ANN001, ARG001
        await awaitable
        raise asyncio.TimeoutError()

    def _runner(_kind: CveSyncTriggerKind, _owner_id: str, _ttl_seconds: int) -> int:
        return 909

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    scheduler = CveSyncScheduler(sync_runner=_runner, process_lock=lock, lease_ttl_seconds=60)
    scheduler._max_run_duration_seconds = 1
    scheduler._recover_once_blocking = lambda reason: CveLeaseRecoveryResult(recovered=False)  # type: ignore[method-assign]
    scheduler._claim_blocking = lambda owner_id: CveLeaseClaimResult(  # type: ignore[method-assign]
        claimed=True,
        owner_id=owner_id,
        active_run_id=42,
    )

    def _release(_owner_id: str) -> bool:
        release_calls["count"] += 1
        return True

    scheduler._release_blocking = _release  # type: ignore[method-assign]
    scheduler._active_run_id_blocking = lambda: 42  # type: ignore[method-assign]

    dispatched = asyncio.run(scheduler.run_sync_once(trigger_kind=CveSyncTriggerKind.MANUAL))

    assert dispatched is True
    assert release_calls["count"] == 1
    assert lock.release_calls == 1
