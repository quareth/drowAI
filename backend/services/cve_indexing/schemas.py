"""Pydantic API schemas for CVE indexing settings, status, and dispatch surfaces.

Scope:
- Defines request and response models used by CVE indexing HTTP endpoints.
- Encodes sync status, sync run summaries, settings payloads, and purge/dispatch outcomes.

Boundaries:
- API contract shapes only. No ORM tables, scheduling logic, or sync orchestration.
- Internal CVE planning contracts remain in backend.services.cve_indexing.contracts.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CveSyncStatusResponse(BaseModel):
    """Current operational sync state for the global CVE index."""

    last_sync_status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    last_successful_sync_at: datetime | None = None
    last_attempt_started_at: datetime | None = None
    last_attempt_finished_at: datetime | None = None
    last_error: str | None = None
    last_applied_baseline_date: date | None = None
    last_applied_delta_hour_utc: datetime | None = None
    rebuild_required: bool = False
    active_run_id: int | None = None
    current_phase: str | None = None
    progress_updated_at: datetime | None = None


class CveSyncRunSummaryResponse(BaseModel):
    """Condensed read model for one sync run history row."""

    id: int
    trigger_kind: Literal["manual", "schedule", "system"]
    sync_kind: Literal["baseline", "delta", "noop"]
    status: Literal["idle", "running", "succeeded", "failed"]
    baseline_date: date | None = None
    delta_from_hour_utc: datetime | None = None
    delta_to_hour_utc: datetime | None = None
    phase: str | None = None
    progress_updated_at: datetime | None = None
    started_at: datetime
    finished_at: datetime | None = None
    processed_records: int = 0
    inserted_records: int = 0
    updated_records: int = 0
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CveSettingsUpdateRequest(BaseModel):
    """Write contract for mutating global CVE indexing settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    daily_sync_hour_utc: int | None = None


class CveSettingsResponse(BaseModel):
    """Read contract returning CVE settings plus current sync status."""

    source_kind: Literal["cvelist_v5"] = "cvelist_v5"
    enabled: bool
    daily_sync_hour_utc: int
    status: CveSyncStatusResponse
    latest_run: CveSyncRunSummaryResponse | None = None


class CveSettingsStaticResponse(BaseModel):
    """Read contract for static CVE indexing configuration fields."""

    source_kind: Literal["cvelist_v5"] = "cvelist_v5"
    enabled: bool
    daily_sync_hour_utc: int


class CveSettingsStatusResponse(BaseModel):
    """Read contract for live CVE sync status and latest run summary."""

    status: CveSyncStatusResponse
    latest_run: CveSyncRunSummaryResponse | None = None


class CveSyncDispatchResponse(BaseModel):
    """Response contract for manual/automatic CVE sync dispatch attempts."""

    queued: bool
    dispatched: bool
    reason: str | None = None
    active_run_id: int | None = None
    run_id: int | None = None


class CvePurgeResponse(BaseModel):
    """Response contract for CVE index purge/reset operations."""

    purged_records: int
    purged_runs: int
    state_reset: bool = True


__all__ = [
    "CvePurgeResponse",
    "CveSettingsResponse",
    "CveSettingsStaticResponse",
    "CveSettingsStatusResponse",
    "CveSettingsUpdateRequest",
    "CveSyncDispatchResponse",
    "CveSyncRunSummaryResponse",
    "CveSyncStatusResponse",
]
