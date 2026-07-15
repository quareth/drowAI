"""Tests for CVE indexing API-facing Pydantic contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime

from backend.services.cve_indexing.schemas import CveSettingsResponse, CveSettingsUpdateRequest, CveSyncRunSummaryResponse, CveSyncStatusResponse
from backend.services.cve_indexing.contracts import (
    CveSyncKind,
    CveSyncStatus,
    CveSyncTriggerKind,
)


def test_cve_settings_response_includes_configuration_and_status_summary() -> None:
    status = CveSyncStatusResponse(
        last_sync_status=CveSyncStatus.SUCCEEDED,
        last_successful_sync_at=datetime(2026, 3, 15, 11, tzinfo=UTC),
        rebuild_required=False,
    )

    payload = CveSettingsResponse(
        enabled=True,
        daily_sync_hour_utc=3,
        status=status,
    ).model_dump()

    assert payload["enabled"] is True
    assert payload["daily_sync_hour_utc"] == 3
    assert payload["status"]["last_sync_status"] == CveSyncStatus.SUCCEEDED


def test_sync_run_summary_shape_does_not_expose_raw_json() -> None:
    summary = CveSyncRunSummaryResponse(
        id=1,
        trigger_kind=CveSyncTriggerKind.MANUAL,
        sync_kind=CveSyncKind.DELTA,
        status=CveSyncStatus.SUCCEEDED,
        baseline_date=date(2026, 3, 15),
        started_at=datetime(2026, 3, 15, 11, tzinfo=UTC),
        finished_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
        processed_records=100,
        inserted_records=25,
        updated_records=75,
    ).model_dump()

    assert "raw_json" not in summary
    assert "cve_json" not in summary


def test_status_and_run_summary_include_optional_progress_fields() -> None:
    status = CveSyncStatusResponse(
        last_sync_status=CveSyncStatus.RUNNING,
        current_phase="upserting",
        progress_updated_at=datetime(2026, 3, 16, 9, tzinfo=UTC),
    )
    summary = CveSyncRunSummaryResponse(
        id=2,
        trigger_kind=CveSyncTriggerKind.SYSTEM,
        sync_kind=CveSyncKind.BASELINE,
        status=CveSyncStatus.RUNNING,
        phase="upserting",
        progress_updated_at=datetime(2026, 3, 16, 9, tzinfo=UTC),
        started_at=datetime(2026, 3, 16, 8, tzinfo=UTC),
    )

    status_payload = status.model_dump()
    summary_payload = summary.model_dump()

    assert status_payload["current_phase"] == "upserting"
    assert status_payload["progress_updated_at"] is not None
    assert summary_payload["phase"] == "upserting"
    assert summary_payload["progress_updated_at"] is not None


def test_settings_update_request_allows_partial_update_fields() -> None:
    request = CveSettingsUpdateRequest(daily_sync_hour_utc=9)

    assert request.daily_sync_hour_utc == 9
    assert request.enabled is None


def test_settings_update_request_rejects_removed_fields() -> None:
    try:
        CveSettingsUpdateRequest.model_validate({"update_interval_minutes": 120})
        assert False, "Expected removed fields to be rejected"
    except Exception as exc:
        assert "extra_forbidden" in str(exc)
