"""Tests for CVE settings service initialization, validation, purge, and merged responses."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from backend.models.cve import CveAffectedProduct, CveIndexSettings, CveIndexState, CveIndexSyncRun, CveRecord
from backend.services.cve_indexing.lease_service import CveLeaseRecoveryResult
from backend.services.cve_indexing.schemas import CveSettingsUpdateRequest
from backend.services.cve_indexing.settings_service import (
    CveSyncLeaseService,
    CveSettingsConflictError,
    CveSettingsService,
    CveSettingsValidationError,
)


class _FakeQuery:
    def __init__(self, records: list[Any]):
        self._records = records

    def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def first(self):
        return self._records[0] if self._records else None

    def delete(self, synchronize_session: bool = False) -> int:  # noqa: ARG002
        deleted = len(self._records)
        self._records.clear()
        return deleted


class _FakeDb:
    def __init__(self):
        self._rows_by_model: dict[type[Any], list[Any]] = {
            CveAffectedProduct: [],
            CveIndexSettings: [],
            CveIndexState: [],
            CveIndexSyncRun: [],
            CveRecord: [],
        }

    def query(self, model: type[Any]) -> _FakeQuery:
        return _FakeQuery(self._rows_by_model.setdefault(model, []))

    def add(self, obj: Any) -> None:
        model_rows = self._rows_by_model.setdefault(type(obj), [])
        if getattr(obj, "id", None) is None:
            obj.id = len(model_rows) + 1
        if obj not in model_rows:
            model_rows.append(obj)

    def commit(self) -> None:
        return None

    def refresh(self, obj: Any) -> None:  # noqa: ARG002
        return None


def test_get_settings_response_lazily_initializes_singletons() -> None:
    fake_db = _FakeDb()
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    response = service.get_settings_response()

    assert response.enabled is False
    assert response.daily_sync_hour_utc == 2
    assert response.status.last_sync_status == "idle"
    assert len(fake_db._rows_by_model[CveIndexSettings]) == 1
    assert len(fake_db._rows_by_model[CveIndexState]) == 1


def test_get_settings_config_response_returns_static_fields_only() -> None:
    fake_db = _FakeDb()
    fake_db.add(
        CveIndexSettings(
            enabled=True,
            daily_sync_hour_utc=6,
        )
    )
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    response = service.get_settings_config_response()

    assert response.enabled is True
    assert response.daily_sync_hour_utc == 6


def test_update_settings_rejects_invalid_daily_hour() -> None:
    fake_db = _FakeDb()
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    with pytest.raises(CveSettingsValidationError, match="daily_sync_hour_utc must be between 0 and 23"):
        service.update_settings(CveSettingsUpdateRequest(daily_sync_hour_utc=24))


def test_get_settings_response_includes_state_and_latest_run_summary() -> None:
    fake_db = _FakeDb()
    fake_db.add(
        CveIndexSettings(
            enabled=True,
            daily_sync_hour_utc=9,
        )
    )
    fake_db.add(
        CveIndexState(
            last_sync_status="succeeded",
            last_successful_sync_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
            current_phase="finalizing",
            progress_updated_at=datetime(2026, 3, 15, 12, 4, tzinfo=UTC),
            rebuild_required=False,
        )
    )
    fake_db.add(
        CveIndexSyncRun(
            trigger_kind="manual",
            sync_kind="baseline",
            status="succeeded",
            baseline_date=date(2026, 3, 15),
            phase="finalizing",
            progress_updated_at=datetime(2026, 3, 15, 12, 4, tzinfo=UTC),
            started_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
            finished_at=datetime(2026, 3, 15, 12, 5, tzinfo=UTC),
            processed_records=42,
            inserted_records=40,
            updated_records=2,
        )
    )
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    response = service.get_settings_response()

    assert response.status.last_sync_status == "succeeded"
    assert response.status.last_successful_sync_at == datetime(2026, 3, 15, 12, tzinfo=UTC)
    assert response.status.current_phase == "finalizing"
    assert response.status.progress_updated_at == datetime(2026, 3, 15, 12, 4, tzinfo=UTC)
    assert response.latest_run is not None
    assert response.latest_run.status == "succeeded"
    assert response.latest_run.phase == "finalizing"
    assert response.latest_run.progress_updated_at == datetime(2026, 3, 15, 12, 4, tzinfo=UTC)
    assert response.latest_run.processed_records == 42


def test_get_status_response_maps_legacy_rebuild_sync_kind_to_baseline() -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=9))
    fake_db.add(CveIndexState(last_sync_status="succeeded"))
    fake_db.add(
        CveIndexSyncRun(
            trigger_kind="manual",
            sync_kind="rebuild",
            status="succeeded",
            started_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
            processed_records=0,
            inserted_records=0,
            updated_records=0,
        )
    )
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    response = service.get_status_response()

    assert response.latest_run is not None
    assert response.latest_run.sync_kind == "baseline"


def test_purge_index_clears_records_runs_and_resets_state() -> None:
    fake_db = _FakeDb()
    fake_db.add(
        CveIndexSettings(
            enabled=True,
            daily_sync_hour_utc=8,
        )
    )
    fake_db.add(
        CveIndexState(
            last_sync_status="failed",
            last_error="something",
            rebuild_required=True,
            active_run_id=1,
        )
    )
    fake_db.add(
        CveIndexSyncRun(
            id=1,
            trigger_kind="manual",
            sync_kind="baseline",
            status="failed",
            started_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
        )
    )
    fake_db.add(CveRecord(cve_id="CVE-2026-0001", source="cvelist_v5", record_state="published", cve_json={"k": "v"}))
    fake_db.add(
        CveAffectedProduct(
            cve_record_id=1,
            cve_id="CVE-2026-0001",
            vendor_raw="Acme",
            vendor_norm="acme",
            product_raw="Widget",
            product_norm="widget",
            default_status="affected",
            versions_json=None,
            cpes_json=None,
        )
    )

    service = CveSettingsService(fake_db)  # type: ignore[arg-type]
    response = service.purge_index()

    assert response.purged_records == 1
    assert response.purged_runs == 1
    assert len(fake_db._rows_by_model[CveAffectedProduct]) == 0
    state = fake_db._rows_by_model[CveIndexState][0]
    assert state.last_sync_status == "idle"
    assert state.last_error is None
    assert state.active_run_id is None
    assert state.rebuild_required is False


def test_purge_index_rejects_when_running() -> None:
    fake_db = _FakeDb()
    fake_db.add(
        CveIndexSettings(
            enabled=True,
            daily_sync_hour_utc=8,
        )
    )
    fake_db.add(CveIndexState(last_sync_status="running", active_run_id=99))
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    with pytest.raises(CveSettingsConflictError, match="Cannot purge CVE index while a sync run is active"):
        service.purge_index()


def test_force_purge_clears_running_state(monkeypatch) -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=8))
    fake_db.add(CveIndexState(last_sync_status="running", active_run_id=1))
    fake_db.add(
        CveIndexSyncRun(
            id=1,
            trigger_kind="manual",
            sync_kind="baseline",
            status="running",
            started_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
        )
    )
    fake_db.add(CveRecord(cve_id="CVE-2026-0002", source="cvelist_v5", record_state="published", cve_json={"k": "v"}))
    forced = {"called": False}

    def _fake_force_clear_all(self):  # noqa: ANN001
        forced["called"] = True
        state = fake_db._rows_by_model[CveIndexState][0]
        state.last_sync_status = "failed"
        state.active_run_id = None
        state.last_error = "force_cleared"
        return CveLeaseRecoveryResult(recovered=True, reason="force_cleared")

    monkeypatch.setattr(CveSyncLeaseService, "force_clear_all", _fake_force_clear_all)
    service = CveSettingsService(fake_db)  # type: ignore[arg-type]

    response = service.purge_index(force=True)

    assert forced["called"] is True
    assert response.purged_records == 1
    assert response.purged_runs == 1
    state = fake_db._rows_by_model[CveIndexState][0]
    assert state.last_sync_status == "idle"
    assert state.active_run_id is None
