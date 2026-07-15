"""Tests for CVE sync orchestration, upsert behavior, and cursor safety."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from backend.models.cve import CveAffectedProduct, CveIndexState, CveIndexSyncRun, CveRecord
from backend.services.cve_indexing.contracts import CveSyncTriggerKind
from backend.services.cve_indexing.parser import CveParsedRecord
from backend.services.cve_indexing.source_client import CveSourceAsset
from backend.services.cve_indexing.sync_service import CveSyncService


def _hour(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _make_raw_payload(cve_id: str, *, title: str, score: float) -> dict[str, Any]:
    return {
        "cveMetadata": {
            "cveId": cve_id,
            "state": "published",
            "datePublished": "2026-03-15T10:00:00Z",
            "dateUpdated": "2026-03-15T11:00:00Z",
        },
        "containers": {
            "cna": {
                "title": title,
                "descriptions": [{"lang": "en", "value": title}],
                "metrics": [{"cvssV3_1": {"baseScore": score, "baseSeverity": "HIGH"}}],
                "problemTypes": [{"descriptions": [{"lang": "en", "description": "CWE-79"}]}],
                "references": [{"url": f"https://example.com/{cve_id.lower()}"}],
                "affected": [
                    {
                        "vendor": "Example Vendor",
                        "product": "Example Product",
                        "defaultStatus": "affected",
                        "versions": [{"version": "1.0.0", "status": "affected"}],
                    }
                ],
            }
        },
    }


def _content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _make_record(cve_id: str, *, title: str, score: float) -> CveParsedRecord:
    raw = _make_raw_payload(cve_id, title=title, score=score)
    return CveParsedRecord(
        cve_id=cve_id,
        record_state="published",
        title=title,
        description=title,
        published_at=datetime(2026, 3, 15, 10, tzinfo=UTC),
        source_updated_at=datetime(2026, 3, 15, 11, tzinfo=UTC),
        severity="high",
        cvss_version="3.1",
        cvss_score=score,
        raw_json=raw,
        content_hash=_content_hash(raw),
    )


class _FakeQuery:
    def __init__(self, records: list[Any]):
        self._records = records

    def order_by(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self

    def first(self):
        return self._records[0] if self._records else None

    def all(self):
        return list(self._records)


class _FakeDb:
    def __init__(self):
        self._rows_by_model: dict[type[Any], list[Any]] = {
            CveAffectedProduct: [],
            CveIndexState: [],
            CveIndexSyncRun: [],
            CveRecord: [],
        }

    def query(self, model: type[Any]) -> _FakeQuery:
        return _FakeQuery(self._rows_by_model.setdefault(model, []))

    def add(self, obj: Any) -> None:
        rows = self._rows_by_model.setdefault(type(obj), [])
        if getattr(obj, "id", None) is None:
            obj.id = len(rows) + 1
        if obj not in rows:
            rows.append(obj)

    def commit(self) -> None:
        return None

    def refresh(self, obj: Any) -> None:  # noqa: ARG002
        return None

    def rollback(self) -> None:
        return None


class _FakeSourceClient:
    def __init__(
        self,
        *,
        baseline_asset: CveSourceAsset,
        delta_assets: tuple[CveSourceAsset, ...],
        payload_by_asset_name: dict[str, bytes],
    ) -> None:
        self._baseline_asset = baseline_asset
        self._delta_assets = delta_assets
        self._payload_by_asset_name = payload_by_asset_name

    def resolve_latest_baseline_asset(self) -> CveSourceAsset:
        return self._baseline_asset

    def resolve_missing_delta_assets(
        self,
        *,
        baseline_day: date,
        applied_hours: tuple[datetime, ...] = (),
    ) -> tuple[CveSourceAsset, ...]:
        applied = set(applied_hours)
        available = [
            asset
            for asset in self._delta_assets
            if asset.baseline_day == baseline_day and asset.delta_hour_utc is not None
        ]
        missing = [asset for asset in available if asset.delta_hour_utc not in applied]
        return tuple(sorted(missing, key=lambda item: item.delta_hour_utc))

    def download_asset(self, asset: CveSourceAsset) -> bytes:
        return self._payload_by_asset_name[asset.name]


class _FakeParser:
    def __init__(self, records_by_payload: dict[bytes, list[CveParsedRecord]], fail_on_payload: bytes | None = None):
        self._records_by_payload = records_by_payload
        self._fail_on_payload = fail_on_payload

    def iter_records(self, payload: bytes):
        if self._fail_on_payload is not None and payload == self._fail_on_payload:
            raise RuntimeError("delta parse failed")
        for record in self._records_by_payload.get(payload, []):
            yield record


class _TestableSyncService(CveSyncService):
    def _load_existing_records(self, cve_ids: set[str]) -> dict[str, CveRecord]:
        rows = self._db.query(CveRecord).all()
        return {row.cve_id: row for row in rows if row.cve_id in cve_ids}


def test_baseline_sync_imports_dataset_and_updates_state() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    parser = _FakeParser({baseline_payload: [_make_record("CVE-2026-0001", title="A", score=7.1)]})
    db = _FakeDb()
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=parser,  # type: ignore[arg-type]
        batch_size=2,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    state = db.query(CveIndexState).first()

    assert run.status == "succeeded"
    assert run.sync_kind == "baseline"
    assert run.processed_records == 1
    assert run.inserted_records == 1
    rows = db.query(CveRecord).all()
    assert len(rows) == 1
    assert rows[0].projection_status == "projected"
    assert int(rows[0].projection_affected_count or 0) == 1
    assert state is not None
    assert state.last_sync_status == "succeeded"
    assert state.last_applied_baseline_date == baseline_day
    assert state.last_applied_delta_hour_utc is None
    assert state.lease_owner_id is None


def test_run_sync_with_lease_sets_running_lease_then_clears_on_success() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    parser = _FakeParser({baseline_payload: [_make_record("CVE-2026-0009", title="Lease", score=7.0)]})
    db = _FakeDb()
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=parser,  # type: ignore[arg-type]
        batch_size=2,
    )

    run = service.run_sync_with_lease(
        trigger_kind=CveSyncTriggerKind.MANUAL,
        owner_id="instance-1:run-abc",
        lease_ttl_seconds=60,
    )
    state = db.query(CveIndexState).first()

    assert run.status == "succeeded"
    assert state is not None
    assert state.last_sync_status == "succeeded"
    assert state.lease_owner_id is None
    assert state.lease_expires_at is None


def test_delta_sync_updates_existing_and_inserts_new_records() -> None:
    baseline_day = date(2026, 3, 15)
    hour_10 = _hour(baseline_day, 10)
    hour_11 = _hour(baseline_day, 11)

    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    delta_asset = CveSourceAsset(
        name="delta-11.zip",
        download_url="https://example.com/delta-11.zip",
        published_at=hour_11,
        baseline_day=baseline_day,
        delta_hour_utc=hour_11,
    )
    delta_payload = b"delta-11"
    updated_record = _make_record("CVE-2026-0001", title="Updated", score=8.8)
    new_record = _make_record("CVE-2026-0002", title="New", score=5.5)

    db = _FakeDb()
    db.add(
        CveIndexState(
            last_sync_status="succeeded",
            last_applied_baseline_date=baseline_day,
            last_applied_delta_hour_utc=hour_10,
            rebuild_required=False,
        )
    )
    existing = _make_record("CVE-2026-0001", title="Original", score=6.1)
    db.add(
        CveRecord(
            cve_id=existing.cve_id,
            source="cvelist_v5",
            record_state=existing.record_state,
            title=existing.title,
            description=existing.description,
            published_at=existing.published_at,
            source_updated_at=existing.source_updated_at,
            severity=existing.severity,
            cve_json=existing.raw_json,
        )
    )

    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(delta_asset,),
            payload_by_asset_name={delta_asset.name: delta_payload, baseline_asset.name: b"baseline"},
        ),
        parser=_FakeParser({delta_payload: [updated_record, new_record]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.SCHEDULE)
    rows = db.query(CveRecord).all()
    state = db.query(CveIndexState).first()

    assert run.sync_kind == "delta"
    assert run.status == "succeeded"
    assert run.inserted_records == 1
    assert run.updated_records == 1
    assert len(rows) == 2
    assert state is not None
    assert state.last_applied_delta_hour_utc == hour_11


def test_unchanged_records_are_skipped_via_content_hash() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    unchanged = _make_record("CVE-2026-0003", title="Stable", score=4.0)

    db = _FakeDb()
    existing = CveRecord(
        cve_id=unchanged.cve_id,
        source="cvelist_v5",
        record_state=unchanged.record_state,
        title=unchanged.title,
        description=unchanged.description,
        published_at=unchanged.published_at,
        source_updated_at=unchanged.source_updated_at,
        severity=unchanged.severity,
        cve_json=unchanged.raw_json,
    )
    existing.projection_status = "projected"
    existing.projection_affected_count = 1
    existing.projection_last_projected_at = datetime(2026, 3, 15, 12, tzinfo=UTC)
    existing.affected_products = [
        CveAffectedProduct(
            cve_id=unchanged.cve_id,
            vendor_raw="Example Vendor",
            vendor_norm="example vendor",
            product_raw="Example Product",
            product_norm="example product",
            default_status="affected",
            versions_json=[{"version": "1.0.0", "status": "affected"}],
            cpes_json=None,
        )
    ]
    db.add(existing)

    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=_FakeParser({baseline_payload: [unchanged]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    rows = db.query(CveRecord).all()

    assert run.processed_records == 1
    assert run.inserted_records == 0
    assert run.updated_records == 0
    assert len(rows) == 1
    assert rows[0].title == "Stable"


def test_unchanged_pending_records_are_projection_refreshed() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    unchanged = _make_record("CVE-2026-0004", title="Stable pending", score=4.4)

    db = _FakeDb()
    db.add(
        CveRecord(
            cve_id=unchanged.cve_id,
            source="cvelist_v5",
            record_state=unchanged.record_state,
            title=unchanged.title,
            description=unchanged.description,
            published_at=unchanged.published_at,
            source_updated_at=unchanged.source_updated_at,
            severity=unchanged.severity,
            cve_json=unchanged.raw_json,
        )
    )

    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=_FakeParser({baseline_payload: [unchanged]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    rows = db.query(CveRecord).all()

    assert run.processed_records == 1
    assert run.updated_records == 1
    assert run.inserted_records == 0
    assert len(rows) == 1
    assert rows[0].projection_status == "projected"
    assert int(rows[0].projection_affected_count or 0) == 1
    assert len(rows[0].affected_products) == 1


def test_duplicate_cve_ids_within_same_batch_do_not_double_insert() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    duplicated = _make_record("CVE-2026-0010", title="Duplicate", score=4.2)

    db = _FakeDb()
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=_FakeParser({baseline_payload: [duplicated, duplicated]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    rows = db.query(CveRecord).all()

    assert run.processed_records == 2
    assert run.inserted_records == 1
    assert run.updated_records == 0
    assert len(rows) == 1


def test_resync_unchanged_record_keeps_single_projection_row() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    stable_record = _make_record("CVE-2026-0011", title="Stable projection", score=4.8)

    db = _FakeDb()
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=_FakeParser({baseline_payload: [stable_record]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    first_run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    second_run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)
    rows = db.query(CveRecord).all()

    assert first_run.inserted_records == 1
    assert second_run.updated_records == 0
    assert len(rows) == 1
    assert len(rows[0].affected_products) == 1


def test_update_rewrites_projection_rows_and_deduplicates_same_payload_entries() -> None:
    baseline_day = date(2026, 3, 15)
    hour_10 = _hour(baseline_day, 10)
    hour_11 = _hour(baseline_day, 11)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    delta_asset = CveSourceAsset(
        name="delta-11.zip",
        download_url="https://example.com/delta-11.zip",
        published_at=hour_11,
        baseline_day=baseline_day,
        delta_hour_utc=hour_11,
    )
    delta_payload = b"delta-11"
    updated = _make_record("CVE-2026-0012", title="Updated projection", score=8.1)
    updated.raw_json["containers"]["cna"]["affected"] = [
        {"vendor": "Acme", "product": "Widget", "defaultStatus": "affected"},
        {"vendor": "Acme", "product": "Widget", "defaultStatus": "affected"},
    ]
    updated = CveParsedRecord(
        cve_id=updated.cve_id,
        record_state=updated.record_state,
        title=updated.title,
        description=updated.description,
        published_at=updated.published_at,
        source_updated_at=updated.source_updated_at,
        severity=updated.severity,
        cvss_version=updated.cvss_version,
        cvss_score=updated.cvss_score,
        raw_json=updated.raw_json,
        content_hash=_content_hash(updated.raw_json),
    )

    db = _FakeDb()
    db.add(
        CveIndexState(
            last_sync_status="succeeded",
            last_applied_baseline_date=baseline_day,
            last_applied_delta_hour_utc=hour_10,
            rebuild_required=False,
        )
    )
    existing = _make_record("CVE-2026-0012", title="Original projection", score=5.5)
    current_row = CveRecord(
        cve_id=existing.cve_id,
        source="cvelist_v5",
        record_state=existing.record_state,
        title=existing.title,
        description=existing.description,
        published_at=existing.published_at,
        source_updated_at=existing.source_updated_at,
        severity=existing.severity,
        cve_json=existing.raw_json,
    )
    current_row.affected_products = [
        CveAffectedProduct(
            cve_id=existing.cve_id,
            vendor_raw="Legacy",
            vendor_norm="legacy",
            product_raw="LegacyProduct",
            product_norm="legacyproduct",
            default_status="affected",
            versions_json=None,
            cpes_json=None,
        )
    ]
    db.add(current_row)

    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(delta_asset,),
            payload_by_asset_name={delta_asset.name: delta_payload},
        ),
        parser=_FakeParser({delta_payload: [updated]}),  # type: ignore[arg-type]
        batch_size=10,
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.SCHEDULE)
    rows = db.query(CveRecord).all()

    assert run.updated_records == 1
    assert len(rows) == 1
    assert len(rows[0].affected_products) == 1
    assert rows[0].projection_status == "projected"
    assert int(rows[0].projection_affected_count or 0) == 1
    assert rows[0].projection_error_code is None
    assert rows[0].affected_products[0].vendor_norm == "acme"
    assert rows[0].affected_products[0].product_norm == "widget"


def test_failed_delta_sync_sets_rebuild_required() -> None:
    baseline_day = date(2026, 3, 15)
    hour_10 = _hour(baseline_day, 10)
    hour_11 = _hour(baseline_day, 11)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    delta_asset = CveSourceAsset(
        name="delta-11.zip",
        download_url="https://example.com/delta-11.zip",
        published_at=hour_11,
        baseline_day=baseline_day,
        delta_hour_utc=hour_11,
    )
    delta_payload = b"delta-11"

    db = _FakeDb()
    db.add(
        CveIndexState(
            last_sync_status="succeeded",
            last_applied_baseline_date=baseline_day,
            last_applied_delta_hour_utc=hour_10,
            rebuild_required=False,
        )
    )
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(delta_asset,),
            payload_by_asset_name={delta_asset.name: delta_payload},
        ),
        parser=_FakeParser({delta_payload: []}, fail_on_payload=delta_payload),  # type: ignore[arg-type]
        batch_size=10,
    )

    with pytest.raises(RuntimeError, match="delta parse failed"):
        service.run_sync(trigger_kind=CveSyncTriggerKind.SCHEDULE)

    state = db.query(CveIndexState).first()
    run = db.query(CveIndexSyncRun).first()
    assert state is not None
    assert run is not None
    assert run.status == "failed"
    assert state.last_sync_status == "failed"
    assert state.rebuild_required is True


def test_failed_delta_sync_does_not_advance_cursor() -> None:
    baseline_day = date(2026, 3, 15)
    hour_10 = _hour(baseline_day, 10)
    hour_11 = _hour(baseline_day, 11)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    delta_asset = CveSourceAsset(
        name="delta-11.zip",
        download_url="https://example.com/delta-11.zip",
        published_at=hour_11,
        baseline_day=baseline_day,
        delta_hour_utc=hour_11,
    )
    delta_payload = b"delta-11"

    db = _FakeDb()
    db.add(
        CveIndexState(
            last_sync_status="succeeded",
            last_applied_baseline_date=baseline_day,
            last_applied_delta_hour_utc=hour_10,
            rebuild_required=False,
        )
    )
    service = _TestableSyncService(
        db,  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(delta_asset,),
            payload_by_asset_name={delta_asset.name: delta_payload},
        ),
        parser=_FakeParser({delta_payload: []}, fail_on_payload=delta_payload),  # type: ignore[arg-type]
        batch_size=10,
    )

    with pytest.raises(RuntimeError):
        service.run_sync(trigger_kind=CveSyncTriggerKind.SCHEDULE)

    state = db.query(CveIndexState).first()
    assert state is not None
    assert state.last_applied_delta_hour_utc == hour_10


def test_progress_callback_invoked_per_batch() -> None:
    baseline_day = date(2026, 3, 15)
    baseline_asset = CveSourceAsset(
        name="baseline.zip",
        download_url="https://example.com/baseline.zip",
        published_at=_hour(baseline_day, 0),
        baseline_day=baseline_day,
    )
    baseline_payload = b"baseline"
    callback_events: list[tuple[int, int, int, str | None]] = []

    service = _TestableSyncService(
        _FakeDb(),  # type: ignore[arg-type]
        source_client=_FakeSourceClient(
            baseline_asset=baseline_asset,
            delta_assets=(),
            payload_by_asset_name={baseline_asset.name: baseline_payload},
        ),
        parser=_FakeParser(
            {
                baseline_payload: [
                    _make_record("CVE-2026-1010", title="One", score=5.0),
                    _make_record("CVE-2026-1011", title="Two", score=5.1),
                ]
            }
        ),  # type: ignore[arg-type]
        batch_size=1,
        on_progress=lambda run: callback_events.append(
            (run.processed_records, run.inserted_records, run.updated_records, run.phase)
        ),
    )

    run = service.run_sync(trigger_kind=CveSyncTriggerKind.MANUAL)

    assert run.status == "succeeded"
    assert len(callback_events) == 2
    assert callback_events[0] == (1, 1, 0, "upserting")
    assert callback_events[1] == (2, 2, 0, "upserting")

