"""Durable lease coordination and stale-run recovery for CVE sync execution.

Scope:
- Owns DB-backed lease claim/release/heartbeat operations for sync ownership.
- Recovers orphaned running rows when lease data is stale or inconsistent.
- Provides shutdown-time force-fail helpers for bounded lifecycle cleanup.

Boundary:
- Does not schedule runs, execute sync logic, or expose HTTP endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.models.cve import CveIndexState, CveIndexSyncRun
from backend.services.cve_indexing.primitives import to_utc, utc_now
from backend.services.cve_indexing.state_store import get_or_create_cve_index_state

ORPHANED_RUN_RECOVERY_ERROR = "orphaned_run_recovered"
SHUTDOWN_RECOVERY_ERROR = "shutdown_timeout_reconciled"


@dataclass(slots=True, frozen=True)
class CveLeaseClaimResult:
    """Result payload for a lease claim attempt."""

    claimed: bool
    owner_id: str
    reason: str | None = None
    active_run_id: int | None = None


@dataclass(slots=True, frozen=True)
class CveLeaseRecoveryResult:
    """Result payload for a stale-running-state recovery attempt."""

    recovered: bool
    reason: str | None = None
    run_id: int | None = None


class CveSyncLeaseService:
    """DB-backed lease manager for CVE sync ownership and stale-run healing."""

    def __init__(self, db: Session):
        self._db = db

    def claim(self, *, owner_id: str, ttl_seconds: int) -> CveLeaseClaimResult:
        """Claim durable ownership lease for one sync execution."""
        now = utc_now()
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        state = self._get_or_create_state(lock=True)

        if self._is_active_lease(state=state, now=now):
            return CveLeaseClaimResult(
                claimed=False,
                owner_id=owner_id,
                reason="already_running",
                active_run_id=state.active_run_id,
            )

        state.lease_owner_id = owner_id
        state.lease_heartbeat_at = now
        state.lease_expires_at = expires_at
        self._db.add(state)
        self._db.commit()
        return CveLeaseClaimResult(
            claimed=True,
            owner_id=owner_id,
            active_run_id=state.active_run_id,
        )

    def refresh(self, *, owner_id: str, ttl_seconds: int) -> bool:
        """Refresh lease heartbeat and expiry when this owner still holds the lease."""
        now = utc_now()
        state = self._get_or_create_state(lock=True)
        if state.lease_owner_id != owner_id:
            return False

        state.lease_heartbeat_at = now
        state.lease_expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        self._db.add(state)
        self._db.commit()
        return True

    def release(self, *, owner_id: str) -> bool:
        """Release lease ownership if the state is still owned by this owner."""
        state = self._get_or_create_state(lock=True)
        if state.lease_owner_id != owner_id:
            return False

        state.lease_owner_id = None
        state.lease_heartbeat_at = None
        state.lease_expires_at = None
        self._db.add(state)
        self._db.commit()
        return True

    def recover_orphaned_running(
        self,
        *,
        recovery_error: str = ORPHANED_RUN_RECOVERY_ERROR,
        force: bool = False,
    ) -> CveLeaseRecoveryResult:
        """Mark orphaned running state as failed when lease data is stale/inconsistent."""
        now = utc_now()
        state = self._get_or_create_state(lock=True)
        if state.last_sync_status != "running":
            return CveLeaseRecoveryResult(recovered=False)

        should_recover = force or self._should_recover_running_state(state=state, now=now)
        if not should_recover:
            return CveLeaseRecoveryResult(recovered=False)
        return self._apply_failure_recovery(state=state, recovery_error=recovery_error, now=now)

    def force_clear_all(self) -> CveLeaseRecoveryResult:
        """Unconditionally clear running state regardless of lease ownership."""
        state = self._get_or_create_state(lock=True)
        if state.last_sync_status not in ("running",):
            return CveLeaseRecoveryResult(recovered=False, reason="not_running")
        return self._apply_failure_recovery(
            state=state,
            recovery_error="force_cleared",
            now=utc_now(),
        )

    def force_fail_owner_run(
        self,
        *,
        owner_id: str,
        recovery_error: str = SHUTDOWN_RECOVERY_ERROR,
    ) -> CveLeaseRecoveryResult:
        """Force-fail currently running state for one owner (shutdown safety path)."""
        state = self._get_or_create_state(lock=True)
        if state.lease_owner_id != owner_id or state.last_sync_status != "running":
            return CveLeaseRecoveryResult(recovered=False)
        return self._apply_failure_recovery(state=state, recovery_error=recovery_error, now=utc_now())

    def current_active_run_id(self) -> int | None:
        """Return active run id from state if present."""
        state = self._get_or_create_state(lock=False)
        return state.active_run_id

    @staticmethod
    def _is_active_lease(*, state: CveIndexState, now: datetime) -> bool:
        if state.lease_owner_id and state.lease_expires_at is not None:
            return to_utc(state.lease_expires_at) > now
        return False

    @staticmethod
    def _should_recover_running_state(*, state: CveIndexState, now: datetime) -> bool:
        if (
            not state.lease_owner_id
            or state.lease_heartbeat_at is None
            or state.lease_expires_at is None
        ):
            return True
        return to_utc(state.lease_expires_at) <= now

    def _get_or_create_state(self, *, lock: bool) -> CveIndexState:
        return get_or_create_cve_index_state(self._db, lock=lock)

    def _resolve_run_for_recovery(self, *, state: CveIndexState) -> CveIndexSyncRun | None:
        if state.active_run_id is not None:
            run = (
                self._db.query(CveIndexSyncRun)
                .filter(CveIndexSyncRun.id == state.active_run_id)
                .first()
            )
            if run is not None:
                return run

        return (
            self._db.query(CveIndexSyncRun)
            .filter(CveIndexSyncRun.status == "running")
            .order_by(CveIndexSyncRun.started_at.desc())
            .first()
        )

    def _apply_failure_recovery(
        self,
        *,
        state: CveIndexState,
        recovery_error: str,
        now: datetime,
    ) -> CveLeaseRecoveryResult:
        run = self._resolve_run_for_recovery(state=state)
        if run is not None:
            run.status = "failed"
            run.finished_at = now
            run.error_message = run.error_message or recovery_error
            self._db.add(run)

        state.last_sync_status = "failed"
        state.last_attempt_finished_at = now
        state.last_error = recovery_error
        state.active_run_id = None
        state.lease_owner_id = None
        state.lease_heartbeat_at = None
        state.lease_expires_at = None
        self._db.add(state)
        self._db.commit()
        return CveLeaseRecoveryResult(
            recovered=True,
            reason=recovery_error,
            run_id=(run.id if run is not None else None),
        )


__all__ = [
    "CveLeaseClaimResult",
    "CveLeaseRecoveryResult",
    "CveSyncLeaseService",
    "ORPHANED_RUN_RECOVERY_ERROR",
    "SHUTDOWN_RECOVERY_ERROR",
]
