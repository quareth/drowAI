"""Service layer for global CVE indexing settings and status read models.

Scope:
- Owns lazy initialization of singleton settings/state rows.
- Centralizes validation of mutable settings updates.
- Merges settings, operational state, and latest run into one API response shape.

Boundary:
- Does not execute sync work or schedule background jobs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.models.cve import CveAffectedProduct, CveIndexSettings, CveIndexState, CveIndexSyncRun, CveRecord
from backend.services.cve_indexing.lease_service import CveSyncLeaseService
from backend.services.cve_indexing.schemas import CvePurgeResponse, CveSettingsStaticResponse, CveSettingsResponse, CveSettingsStatusResponse, CveSettingsUpdateRequest, CveSyncRunSummaryResponse, CveSyncStatusResponse
from backend.services.cve_indexing.primitives import normalize_hour
from backend.services.cve_indexing.state_store import get_or_create_cve_index_state

DEFAULT_DAILY_SYNC_HOUR_UTC = 2


class CveSettingsValidationError(ValueError):
    """Raised when a CVE settings update payload is invalid."""


class CveSettingsConflictError(ValueError):
    """Raised when the requested operation conflicts with active CVE execution state."""


class CveSettingsService:
    """Read/write service for global CVE settings and merged status payloads."""

    def __init__(self, db: Session):
        self._db = db

    def get_settings_response(self) -> CveSettingsResponse:
        """Return settings merged with current operational sync status."""
        settings = self._get_or_create_settings()
        status = self.get_status_response()
        return CveSettingsResponse(
            enabled=settings.enabled,
            daily_sync_hour_utc=_normalize_daily_hour(getattr(settings, "daily_sync_hour_utc", DEFAULT_DAILY_SYNC_HOUR_UTC)),
            status=status.status,
            latest_run=status.latest_run,
        )

    def get_settings_config_response(self) -> CveSettingsStaticResponse:
        """Return static CVE config fields for instant settings-panel rendering."""
        settings = self._get_or_create_settings()
        return CveSettingsStaticResponse(
            enabled=settings.enabled,
            daily_sync_hour_utc=_normalize_daily_hour(getattr(settings, "daily_sync_hour_utc", DEFAULT_DAILY_SYNC_HOUR_UTC)),
        )

    def get_status_response(self) -> CveSettingsStatusResponse:
        """Return live CVE sync status and latest run summary."""
        state = self._get_or_create_state()
        latest_run = self._get_latest_run()
        return CveSettingsStatusResponse(
            status=self._to_status_response(state),
            latest_run=self._to_latest_run_response(latest_run),
        )

    def update_settings(self, payload: CveSettingsUpdateRequest) -> CveSettingsResponse:
        """Apply mutable setting updates after centralized validation."""
        settings = self._get_or_create_settings()
        update_data = payload.model_dump(exclude_unset=True)
        self._validate_update(update_data)

        if "enabled" in update_data:
            settings.enabled = bool(update_data["enabled"])
        if "daily_sync_hour_utc" in update_data:
            settings.daily_sync_hour_utc = _normalize_daily_hour(update_data["daily_sync_hour_utc"])

        self._db.add(settings)
        self._db.commit()
        self._db.refresh(settings)

        return self.get_settings_response()

    def purge_index(self, *, force: bool = False) -> CvePurgeResponse:
        """Delete indexed CVE records/run history and reset operational cursor state."""
        state = self._get_or_create_state()
        if state.last_sync_status == "running":
            if force:
                CveSyncLeaseService(self._db).force_clear_all()
                state = self._get_or_create_state()
            else:
                raise CveSettingsConflictError("Cannot purge CVE index while a sync run is active.")

        if state.last_sync_status == "running":
            raise CveSettingsConflictError("Cannot purge CVE index while a sync run is active.")

        # Reset state first so FK references do not block run-history deletion.
        state.last_sync_status = "idle"
        state.last_successful_sync_at = None
        state.last_attempt_started_at = None
        state.last_attempt_finished_at = None
        state.last_error = None
        state.last_applied_baseline_date = None
        state.last_applied_delta_hour_utc = None
        state.rebuild_required = False
        state.active_run_id = None
        state.lease_owner_id = None
        state.lease_heartbeat_at = None
        state.lease_expires_at = None
        self._db.add(state)
        self._db.commit()

        purged_records = int(self._db.query(CveRecord).delete(synchronize_session=False))
        self._db.query(CveAffectedProduct).delete(synchronize_session=False)
        purged_runs = int(self._db.query(CveIndexSyncRun).delete(synchronize_session=False))
        self._db.commit()
        return CvePurgeResponse(
            purged_records=purged_records,
            purged_runs=purged_runs,
            state_reset=True,
        )

    def _get_or_create_settings(self) -> CveIndexSettings:
        settings = self._db.query(CveIndexSettings).order_by(CveIndexSettings.id.asc()).first()
        if settings is not None:
            return settings

        settings = CveIndexSettings(
            enabled=False,
            daily_sync_hour_utc=DEFAULT_DAILY_SYNC_HOUR_UTC,
        )
        self._db.add(settings)
        self._db.commit()
        self._db.refresh(settings)
        return settings

    def _get_or_create_state(self) -> CveIndexState:
        return get_or_create_cve_index_state(self._db, lock=False)

    def _get_latest_run(self) -> CveIndexSyncRun | None:
        return self._db.query(CveIndexSyncRun).order_by(desc(CveIndexSyncRun.started_at)).first()

    def _validate_update(self, update_data: dict[str, Any]) -> None:
        hour = update_data.get("daily_sync_hour_utc")
        if hour is not None and not 0 <= int(hour) <= 23:
            raise CveSettingsValidationError("daily_sync_hour_utc must be between 0 and 23.")

    @staticmethod
    def _to_status_response(state: CveIndexState) -> CveSyncStatusResponse:
        return CveSyncStatusResponse(
            last_sync_status=state.last_sync_status,
            last_successful_sync_at=state.last_successful_sync_at,
            last_attempt_started_at=state.last_attempt_started_at,
            last_attempt_finished_at=state.last_attempt_finished_at,
            last_error=state.last_error,
            last_applied_baseline_date=state.last_applied_baseline_date,
            last_applied_delta_hour_utc=state.last_applied_delta_hour_utc,
            rebuild_required=bool(state.rebuild_required),
            active_run_id=state.active_run_id,
            current_phase=state.current_phase,
            progress_updated_at=state.progress_updated_at,
        )

    @staticmethod
    def _to_latest_run_response(run: CveIndexSyncRun | None) -> CveSyncRunSummaryResponse | None:
        if run is None:
            return None

        return CveSyncRunSummaryResponse(
            id=run.id,
            trigger_kind=run.trigger_kind,
            sync_kind=_normalize_sync_kind(run.sync_kind),
            status=run.status,
            baseline_date=run.baseline_date,
            delta_from_hour_utc=run.delta_from_hour_utc,
            delta_to_hour_utc=run.delta_to_hour_utc,
            phase=run.phase,
            progress_updated_at=run.progress_updated_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            processed_records=run.processed_records,
            inserted_records=run.inserted_records,
            updated_records=run.updated_records,
            error_message=run.error_message,
        )

def _normalize_daily_hour(value: Any) -> int:
    return normalize_hour(int(value))


def _normalize_sync_kind(value: Any) -> str:
    """Map legacy sync kind values to current public contract."""
    normalized = str(value or "").strip().lower()
    if normalized == "rebuild":
        return "baseline"
    if normalized in {"baseline", "delta", "noop"}:
        return normalized
    return "noop"

