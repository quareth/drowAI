"""Router tests for CVE settings/status/read, manual sync dispatch, and purge APIs."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.models.cve import CveIndexSettings, CveIndexState, CveIndexSyncRun, CveRecord
from backend.routers import cve_settings as cve_routes
from backend.services.cve_indexing.lease_service import CveLeaseRecoveryResult
from backend.services.cve_indexing.scheduler import CveSyncDispatchResult
from backend.services.cve_indexing.settings_service import CveSyncLeaseService


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


@contextmanager
def _make_client(fake_db: _FakeDb) -> Generator[TestClient, None, None]:
    app = FastAPI()
    app.include_router(cve_routes.router)

    def _fake_get_db():
        yield fake_db

    def _fake_get_current_user():
        return SimpleNamespace(id=1, username="tester")

    app.dependency_overrides[cve_routes.get_db] = _fake_get_db
    app.dependency_overrides[cve_routes.get_current_user] = _fake_get_current_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_cve_settings_creates_default_rows() -> None:
    fake_db = _FakeDb()
    with _make_client(fake_db) as client:
        response = client.get("/api/settings/cve")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["daily_sync_hour_utc"] == 2
    assert payload["status"]["last_sync_status"] == "idle"

    assert len(fake_db._rows_by_model[CveIndexSettings]) == 1
    assert len(fake_db._rows_by_model[CveIndexState]) == 1


def test_get_cve_settings_config_returns_static_fields() -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=5))

    with _make_client(fake_db) as client:
        response = client.get("/api/settings/cve/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "source_kind": "cvelist_v5",
        "enabled": True,
        "daily_sync_hour_utc": 5,
    }


def test_get_cve_settings_status_returns_live_status_and_latest_run() -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=5))
    fake_db.add(CveIndexState(last_sync_status="running", active_run_id=12))
    fake_db.add(
        CveIndexSyncRun(
            id=12,
            trigger_kind="manual",
            sync_kind="delta",
            status="running",
            started_at=datetime(2026, 3, 15, 12, 0, tzinfo=UTC),
            processed_records=17,
            inserted_records=9,
            updated_records=8,
        )
    )

    with _make_client(fake_db) as client:
        response = client.get("/api/settings/cve/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["last_sync_status"] == "running"
    assert payload["status"]["active_run_id"] == 12
    assert payload["latest_run"]["id"] == 12
    assert payload["latest_run"]["sync_kind"] == "delta"


def test_put_cve_settings_updates_mutable_fields() -> None:
    fake_db = _FakeDb()
    with _make_client(fake_db) as client:
        response = client.put(
            "/api/settings/cve",
            json={
                "enabled": True,
                "daily_sync_hour_utc": 7,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["daily_sync_hour_utc"] == 7


def test_put_cve_settings_rejects_invalid_daily_hour() -> None:
    fake_db = _FakeDb()
    with _make_client(fake_db) as client:
        response = client.put(
            "/api/settings/cve",
            json={"daily_sync_hour_utc": 24},
        )

    assert response.status_code == 400
    assert "daily_sync_hour_utc must be between 0 and 23" in response.json()["detail"]


def test_post_sync_returns_dispatched_payload(monkeypatch) -> None:
    fake_db = _FakeDb()

    async def _fake_dispatch_sync_once(**kwargs):  # noqa: ANN003
        return CveSyncDispatchResult(
            queued=True,
            dispatched=True,
            owner_id="test-owner",
            active_run_id=None,
        )

    monkeypatch.setattr(cve_routes.cve_sync_scheduler, "dispatch_sync_once", _fake_dispatch_sync_once)

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/sync")

    assert response.status_code == 202
    assert response.json() == {
        "queued": True,
        "dispatched": True,
        "active_run_id": None,
        "run_id": None,
        "reason": None,
    }


def test_post_sync_returns_already_running_payload(monkeypatch) -> None:
    fake_db = _FakeDb()

    async def _fake_dispatch_sync_once(**kwargs):  # noqa: ANN003
        return CveSyncDispatchResult(
            queued=False,
            dispatched=False,
            reason="already_running",
            active_run_id=91,
        )

    monkeypatch.setattr(cve_routes.cve_sync_scheduler, "dispatch_sync_once", _fake_dispatch_sync_once)

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/sync")

    assert response.status_code == 202
    assert response.json() == {
        "queued": False,
        "dispatched": False,
        "reason": "already_running",
        "active_run_id": 91,
        "run_id": None,
    }


def test_post_cancel_sync_returns_dispatch_payload(monkeypatch) -> None:
    fake_db = _FakeDb()

    async def _fake_cancel_active_run():
        return CveSyncDispatchResult(
            queued=False,
            dispatched=True,
            reason="force_cleared",
            active_run_id=None,
            run_id=12,
        )

    monkeypatch.setattr(cve_routes.cve_sync_scheduler, "cancel_active_run", _fake_cancel_active_run)

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/sync/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "queued": False,
        "dispatched": True,
        "reason": "force_cleared",
        "active_run_id": None,
        "run_id": 12,
    }


def test_post_purge_returns_counts() -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=5))
    fake_db.add(CveIndexState(last_sync_status="idle"))
    fake_db.add(CveIndexSyncRun(id=1, trigger_kind="manual", sync_kind="baseline", status="succeeded", started_at=datetime(2026, 3, 15, 12, tzinfo=UTC)))
    fake_db.add(CveRecord(cve_id="CVE-2026-0001", source="cvelist_v5", record_state="published", cve_json={"k": "v"}))

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/purge")

    assert response.status_code == 200
    assert response.json() == {
        "purged_records": 1,
        "purged_runs": 1,
        "state_reset": True,
    }


def test_post_purge_rejects_while_running() -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=5))
    fake_db.add(CveIndexState(last_sync_status="running", active_run_id=7))

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/purge")

    assert response.status_code == 409
    assert "Cannot purge CVE index while a sync run is active" in response.json()["detail"]


def test_post_purge_force_cancels_active_run_and_purges(monkeypatch) -> None:
    fake_db = _FakeDb()
    fake_db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=5))
    fake_db.add(CveIndexState(last_sync_status="running", active_run_id=7))
    fake_db.add(
        CveIndexSyncRun(
            id=7,
            trigger_kind="manual",
            sync_kind="baseline",
            status="running",
            started_at=datetime(2026, 3, 15, 12, tzinfo=UTC),
        )
    )
    fake_db.add(CveRecord(cve_id="CVE-2026-9999", source="cvelist_v5", record_state="published", cve_json={"k": "v"}))
    cancel_called = {"value": False}
    force_clear_called = {"value": False}

    async def _fake_cancel_active_run():
        cancel_called["value"] = True
        return CveSyncDispatchResult(queued=False, dispatched=True, reason="force_cleared", active_run_id=None, run_id=7)

    def _fake_force_clear_all(self):  # noqa: ANN001
        force_clear_called["value"] = True
        state = self._db.query(CveIndexState).first()
        if state is not None:
            state.last_sync_status = "failed"
            state.active_run_id = None
        return CveLeaseRecoveryResult(recovered=True, reason="forced")

    monkeypatch.setattr(cve_routes.cve_sync_scheduler, "cancel_active_run", _fake_cancel_active_run)
    monkeypatch.setattr(CveSyncLeaseService, "force_clear_all", _fake_force_clear_all)

    with _make_client(fake_db) as client:
        response = client.post("/api/settings/cve/purge?force=true")

    assert response.status_code == 200
    assert cancel_called["value"] is True
    assert force_clear_called["value"] is True
    assert response.json() == {
        "purged_records": 1,
        "purged_runs": 1,
        "state_reset": True,
    }


def test_put_cve_settings_rejects_removed_interval_field() -> None:
    fake_db = _FakeDb()
    with _make_client(fake_db) as client:
        response = client.put(
            "/api/settings/cve",
            json={"update_interval_minutes": 120},
        )

    assert response.status_code == 422
