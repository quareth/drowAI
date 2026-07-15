"""Tests for shared CVE projection-readiness contracts and evaluator behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.models.cve import CveAffectedProduct, CveRecord
from backend.services.cve_indexing.projection_readiness import CveProjectionReadinessService


class _FakeQuery:
    def __init__(self, rows: list[Any], *, scalar_value: int | None = None):
        self._rows = list(rows)
        self._scalar_value = scalar_value

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)

    def scalar(self):
        return self._scalar_value

    def group_by(self, *_args, **_kwargs):
        return self


class _FakeDb:
    def __init__(self, *, cve_records: list[Any], affected_rows: list[Any]):
        self._rows_by_model = {
            CveRecord: list(cve_records),
            CveAffectedProduct: list(affected_rows),
        }

    def query(self, *entities):
        if not entities:
            return _FakeQuery([])

        entity_name = " ".join(str(entity) for entity in entities).lower()
        cve_rows = self._rows_by_model.get(CveRecord, [])
        affected_rows = self._rows_by_model.get(CveAffectedProduct, [])

        if "projection_status" in entity_name and "count(" in entity_name:
            grouped: dict[str, int] = {}
            for row in cve_rows:
                key = getattr(row, "projection_status", None)
                grouped[str(key)] = int(grouped.get(str(key), 0)) + 1
            grouped_rows = [(status, count) for status, count in grouped.items()]
            return _FakeQuery(grouped_rows)
        if "count(" in entity_name and "id" in entity_name:
            return _FakeQuery([], scalar_value=len(cve_rows))
        if len(entities) == 1 and entities[0] is CveRecord:
            return _FakeQuery(cve_rows)
        if len(entities) == 1 and entities[0] is CveAffectedProduct:
            return _FakeQuery(affected_rows)
        return _FakeQuery([])


def test_projection_readiness_reports_empty_index() -> None:
    db = _FakeDb(cve_records=[], affected_rows=[])

    result = CveProjectionReadinessService(db).evaluate()

    assert result.ready is False
    assert result.reason == "lookup_index_empty"
    assert result.record_count == 0
    assert result.affected_product_count == 0


def test_projection_readiness_blocks_when_pending_rows_exist() -> None:
    db = _FakeDb(
        cve_records=[
            SimpleNamespace(projection_status="projected"),
            SimpleNamespace(projection_status="pending"),
            SimpleNamespace(projection_status="non_projectable"),
        ],
        affected_rows=[object()],
    )

    result = CveProjectionReadinessService(db).evaluate()

    assert result.ready is False
    assert result.reason == "lookup_projection_incomplete"
    assert result.status_counts["pending"] == 1
    assert result.blocking_status_counts["pending"] == 1


def test_projection_readiness_blocks_when_projection_errors_exist() -> None:
    db = _FakeDb(
        cve_records=[
            SimpleNamespace(projection_status="projected"),
            SimpleNamespace(projection_status="projection_error"),
        ],
        affected_rows=[object()],
    )

    result = CveProjectionReadinessService(db).evaluate()

    assert result.ready is False
    assert result.reason == "lookup_projection_errors"
    assert result.status_counts["projection_error"] == 1
    assert result.blocking_status_counts["projection_error"] == 1


def test_projection_readiness_allows_non_projectable_only_index() -> None:
    db = _FakeDb(
        cve_records=[
            SimpleNamespace(projection_status="non_projectable"),
            SimpleNamespace(projection_status="non_projectable"),
        ],
        affected_rows=[],
    )

    result = CveProjectionReadinessService(db).evaluate()

    assert result.ready is True
    assert result.reason == "ok"
    assert result.record_count == 2
    assert result.affected_product_count == 0
    assert result.status_counts["non_projectable"] == 2


def test_projection_readiness_normalizes_unknown_status_to_pending() -> None:
    db = _FakeDb(
        cve_records=[SimpleNamespace(projection_status="mystery_status")],
        affected_rows=[],
    )

    result = CveProjectionReadinessService(db).evaluate()

    assert result.ready is False
    assert result.reason == "lookup_projection_incomplete"
    assert result.status_counts["pending"] == 1
