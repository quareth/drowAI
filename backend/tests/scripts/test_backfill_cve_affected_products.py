"""Tests for deterministic CVE affected-product backfill script behavior.

Scope:
- Validates projection population from existing `cve_records`.
- Confirms idempotent rerun and cursor-based resumability semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.models.cve import CveAffectedProduct, CveRecord
from backend.scripts import backfill_cve_affected_products as script


def _make_payload(*, vendor: str, product: str, version: str) -> dict:
    return {
        "containers": {
            "cna": {
                "affected": [
                    {
                        "vendor": vendor,
                        "product": product,
                        "defaultStatus": "affected",
                        "versions": [{"version": version, "status": "affected"}],
                    }
                ]
            }
        }
    }


def _insert_record(*, row_id: int, cve_id: str, vendor: str, product: str, version: str) -> CveRecord:
    row = CveRecord(
        id=row_id,
        cve_id=cve_id,
        source="cvelist_v5",
        record_state="published",
        cve_json=_make_payload(vendor=vendor, product=product, version=version),
    )
    row.affected_products = []
    return row


def _insert_non_projectable_record(*, row_id: int, cve_id: str) -> CveRecord:
    row = CveRecord(
        id=row_id,
        cve_id=cve_id,
        source="cvelist_v5",
        record_state="published",
        cve_json={"containers": {"cna": {"affected": []}}},
    )
    row.affected_products = []
    return row


@dataclass
class _RecordIdFilter:
    greater_than: int | None = None


class _FakeQuery:
    def __init__(
        self,
        db: "_FakeDb",
        model: Any,
        rows: list[Any],
        *,
        scalar_value: int | None = None,
    ):
        self._db = db
        self._model = model
        self._rows = rows
        self._scalar_value = scalar_value
        self._filter = _RecordIdFilter()
        self._limit: int | None = None

    def order_by(self, *_args, **_kwargs):
        return self

    def filter(self, expression):
        if self._model is CveRecord:
            value = getattr(expression, "right", None)
            bound_value = getattr(value, "value", None)
            if bound_value is not None:
                self._filter.greater_than = int(bound_value)
        return self

    def limit(self, value: int):
        self._limit = int(value)
        return self

    def _apply(self) -> list[Any]:
        rows = list(self._rows)
        if self._model is CveRecord and self._filter.greater_than is not None:
            rows = [row for row in rows if int(row.id) > int(self._filter.greater_than)]
        rows = sorted(rows, key=lambda item: int(getattr(item, "id", 0) or 0))
        if self._limit is not None:
            rows = rows[: int(self._limit)]
        return rows

    def all(self):
        return self._apply()

    def first(self):
        rows = self._apply()
        return rows[0] if rows else None

    def count(self):
        return len(self._apply())

    def scalar(self):
        return self._scalar_value

    def group_by(self, *_args, **_kwargs):
        return self


class _FakeDb:
    def __init__(self, records: list[CveRecord]):
        self._records = list(records)

    def query(self, *entities):
        if not entities:
            return _FakeQuery(self, None, [])

        entity_name = " ".join(str(entity) for entity in entities).lower()
        if "projection_status" in entity_name and "count(" in entity_name:
            grouped: dict[str, int] = {}
            for row in self._records:
                key = str(getattr(row, "projection_status", None))
                grouped[key] = int(grouped.get(key, 0)) + 1
            return _FakeQuery(self, tuple(entities), list(grouped.items()))
        if "count(" in entity_name and "id" in entity_name:
            return _FakeQuery(self, tuple(entities), [], scalar_value=len(self._records))

        model = entities[0]
        if len(entities) == 1 and model is CveRecord:
            return _FakeQuery(self, CveRecord, self._records)
        if len(entities) == 1 and model is CveAffectedProduct:
            affected_rows = []
            for row in self._records:
                affected_rows.extend(list(getattr(row, "affected_products", []) or []))
            return _FakeQuery(self, CveAffectedProduct, affected_rows)
        return _FakeQuery(self, tuple(entities), [])

    def add(self, obj):
        if isinstance(obj, CveRecord) and obj not in self._records:
            self._records.append(obj)


def test_backfill_populates_projection_for_existing_records() -> None:
    db = _FakeDb(
        [
            _insert_record(row_id=1, cve_id="CVE-2026-9001", vendor="Acme", product="Widget", version="1.0.0"),
            _insert_record(row_id=2, cve_id="CVE-2026-9002", vendor="Beta", product="Portal", version="2.5.1"),
        ]
    )

    result = script.run_backfill(db=db, batch_size=100)

    projected = db.query(CveAffectedProduct).all()
    assert result["ok"] is True
    assert result["processed_count"] == 2
    assert result["updated_count"] == 2
    assert result["unchanged_count"] == 0
    assert result["projection_ready"] is True
    assert result["missing_projection_records"] == 0
    assert len(projected) == 2
    assert projected[0].vendor_norm == "acme"
    assert projected[1].product_norm == "portal"


def test_backfill_is_idempotent_on_second_run() -> None:
    db = _FakeDb(
        [
            _insert_record(row_id=1, cve_id="CVE-2026-9011", vendor="Acme", product="Gateway", version="3.1.0"),
        ]
    )

    first = script.run_backfill(db=db, batch_size=100)
    second = script.run_backfill(db=db, batch_size=100)

    assert first["updated_count"] == 1
    assert second["updated_count"] == 0
    assert second["unchanged_count"] == 1
    assert second["projection_ready"] is True
    assert db.query(CveAffectedProduct).count() == 1


def test_backfill_supports_cursor_resumption() -> None:
    first_record = _insert_record(row_id=1, cve_id="CVE-2026-9021", vendor="Gamma", product="Edge", version="1.2.0")
    db = _FakeDb(
        [
            first_record,
            _insert_record(row_id=2, cve_id="CVE-2026-9022", vendor="Delta", product="Node", version="4.0.0"),
        ]
    )

    first_batch = script.run_backfill(db=db, batch_size=1)
    assert first_batch["processed_count"] == 1
    assert first_batch["has_more"] is True
    assert first_batch["next_cursor_id"] == int(first_record.id)
    assert first_batch["projection_ready"] is False
    assert first_batch["missing_projection_records"] == 1

    second_batch = script.run_backfill(
        db=db,
        batch_size=1,
        cursor_after_id=int(first_batch["next_cursor_id"]),
    )
    assert second_batch["processed_count"] == 1
    assert second_batch["has_more"] is False
    assert second_batch["projection_ready"] is True
    assert second_batch["missing_projection_records"] == 0
    assert db.query(CveAffectedProduct).count() == 2


def test_backfill_treats_non_projectable_records_as_ready_when_no_blocking_statuses() -> None:
    db = _FakeDb(
        [
            _insert_non_projectable_record(row_id=1, cve_id="CVE-2026-9031"),
            _insert_non_projectable_record(row_id=2, cve_id="CVE-2026-9032"),
        ]
    )

    result = script.run_backfill(db=db, batch_size=100)

    assert result["ok"] is True
    assert result["projection_ready"] is True
    assert result["affected_product_count"] == 0
    assert result["non_projectable_cve_count"] == 2
    assert result["pending_projection_count"] == 0
    assert result["projection_error_count"] == 0
    assert result["missing_projection_records"] == 0
    assert result["readiness_reason"] == "ok"
