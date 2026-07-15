"""Focused tests for simplified standalone `knowledge.cve_lookup` behavior."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.tools.knowledge.cve_lookup import CveLookupTool
from agent.tools.tool_registry import available_tools, get_tool
from backend.services.cve_indexing.match_contracts import CveLookupMatch, CveLookupResponse


class _FakeQuery:
    def __init__(
        self,
        row,
        *,
        count_value: int | None = None,
        rows: list[object] | None = None,
        scalar_value: int | None = None,
    ):
        self._row = row
        self._count_value = count_value
        self._rows = list(rows or [])
        self._scalar_value = scalar_value

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row

    def count(self):
        return int(self._count_value or 0)

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar_value

    def group_by(self, *_args, **_kwargs):
        return self


class _FakeDb:
    def __init__(
        self,
        *,
        settings_enabled: bool | None = True,
        cve_record_rows: list[object] | None = None,
        cve_affected_count: int = 1,
    ):
        self._settings_enabled = settings_enabled
        if cve_record_rows is None:
            self._cve_record_rows = [SimpleNamespace(projection_status="projected")]
        else:
            self._cve_record_rows = list(cve_record_rows)
        self._cve_affected_count = cve_affected_count
        self.closed = False

    def query(self, *models):
        if not models:
            return _FakeQuery(None)
        model = models[0]
        model_name = str(getattr(model, "__name__", ""))
        model_repr = " ".join(str(item) for item in models).lower()
        if "projection_status" in model_repr and "count(" in model_repr:
            grouped: dict[str, int] = {}
            for row in self._cve_record_rows:
                key = getattr(row, "projection_status", None)
                grouped[str(key)] = int(grouped.get(str(key), 0)) + 1
            grouped_rows = [(status, count) for status, count in grouped.items()]
            return _FakeQuery(None, rows=grouped_rows)
        if "count(" in model_repr and "id" in model_repr:
            return _FakeQuery(None, scalar_value=len(self._cve_record_rows))
        if model_name == "CveIndexSettings":
            if self._settings_enabled is None:
                return _FakeQuery(None)
            return _FakeQuery(SimpleNamespace(id=1, enabled=bool(self._settings_enabled)))
        if model_name == "CveRecord":
            return _FakeQuery(None, count_value=len(self._cve_record_rows), rows=self._cve_record_rows)
        if model_name == "CveAffectedProduct":
            return _FakeQuery(None, count_value=self._cve_affected_count)
        return _FakeQuery(None)

    def close(self):
        self.closed = True


def test_cve_lookup_tool_is_discoverable_via_tool_registry() -> None:
    assert "knowledge.cve_lookup" in set(available_tools())
    tool_cls = get_tool("knowledge.cve_lookup")
    assert tool_cls is CveLookupTool


def test_cve_lookup_tool_requires_product_and_version() -> None:
    tool = CveLookupTool()

    result_missing_product = tool.validate_and_run({"version": "9.6.0"})
    assert result_missing_product.success is False
    assert result_missing_product.exit_code == -1
    assert "Validation error" in result_missing_product.stderr

    result_missing_version = tool.validate_and_run({"product": "PostgreSQL"})
    assert result_missing_version.success is False
    assert result_missing_version.exit_code == -1
    assert "Validation error" in result_missing_version.stderr


def test_cve_lookup_tool_rejects_removed_legacy_args() -> None:
    tool = CveLookupTool()
    result = tool.validate_and_run(
        {
            "product": "PostgreSQL",
            "version": "9.6.0",
            "service_key": "svc:web:443",
        }
    )

    assert result.success is False
    assert result.exit_code == -1
    assert "Extra inputs are not permitted" in result.stderr


def test_cve_lookup_tool_runs_without_runtime_context(monkeypatch) -> None:
    captured = {}
    fake_db = _FakeDb()

    class _FakeMatchService:
        def __init__(self, _db):
            self._db = _db

        def lookup(self, request):
            captured["request"] = request
            return CveLookupResponse(
                matches=(
                    CveLookupMatch(
                        cve_id="CVE-2026-7001",
                        rationale="product exact match; version exact match",
                        version_applicable=True,
                        score=0.97,
                    ),
                ),
                message="ok",
            )

    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)
    monkeypatch.setattr("backend.services.cve_indexing.match_service.CveMatchService", _FakeMatchService)

    tool = CveLookupTool()
    result = tool.validate_and_run({"product": "PostgreSQL", "version": "9.6.0"})

    assert result.success is True
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["coverage"]["is_partial"] is False
    assert payload["matches"][0]["cve_id"] == "CVE-2026-7001"
    assert captured["request"].product == "postgresql"
    assert captured["request"].version == "9.6.0"
    assert fake_db.closed is True


def test_cve_lookup_tool_returns_partial_index_status_when_projection_incomplete(monkeypatch) -> None:
    fake_db = _FakeDb(
        settings_enabled=True,
        cve_record_rows=[
            SimpleNamespace(projection_status="projected"),
            SimpleNamespace(projection_status="pending"),
            SimpleNamespace(projection_status="projection_error"),
        ],
        cve_affected_count=1,
    )

    class _FakeMatchService:
        def __init__(self, _db):
            self._db = _db

        def lookup(self, _request):
            return CveLookupResponse(matches=(), message="no_cve_match_candidates")

    monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "true")
    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)
    monkeypatch.setattr("backend.services.cve_indexing.match_service.CveMatchService", _FakeMatchService)

    tool = CveLookupTool()
    result = tool.validate_and_run({"product": "PostgreSQL", "version": "9.6.0"})

    assert result.success is True
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial_index"
    assert payload["coverage"]["is_partial"] is True
    assert payload["coverage"]["pending_count"] == 1
    assert payload["coverage"]["error_count"] == 1
    assert "incomplete" in payload["coverage"]["warning"].lower()
    assert fake_db.closed is True


def test_cve_lookup_tool_fails_when_index_is_empty(monkeypatch) -> None:
    fake_db = _FakeDb(
        settings_enabled=True,
        cve_record_rows=[],
        cve_affected_count=0,
    )
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "true")
    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)

    tool = CveLookupTool()
    result = tool.validate_and_run({"product": "PostgreSQL", "version": "9.6.0"})

    assert result.success is False
    assert result.exit_code == 2
    assert result.metadata["cve_lookup"]["status"] == "lookup_index_empty"
    assert "index is empty" in result.stderr
    assert fake_db.closed is True


def test_cve_lookup_tool_marks_no_matches_when_ready_and_empty_result(monkeypatch) -> None:
    fake_db = _FakeDb(
        settings_enabled=True,
        cve_record_rows=[SimpleNamespace(projection_status="projected")],
        cve_affected_count=0,
    )

    class _FakeMatchService:
        def __init__(self, _db):
            self._db = _db

        def lookup(self, _request):
            return CveLookupResponse(matches=(), message="no_cve_match_candidates")

    monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "true")
    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)
    monkeypatch.setattr("backend.services.cve_indexing.match_service.CveMatchService", _FakeMatchService)

    tool = CveLookupTool()
    result = tool.validate_and_run({"product": "PostgreSQL", "version": "9.6.0"})

    assert result.success is True
    payload = json.loads(result.stdout)
    assert payload["status"] == "no_matches"
    assert payload["matches"] == []
    assert payload["coverage"]["is_partial"] is False
    assert fake_db.closed is True
