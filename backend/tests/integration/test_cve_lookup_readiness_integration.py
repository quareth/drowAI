"""Integration coverage for lookup readiness gating and payload contract flow."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from agent.tools.knowledge.cve_lookup import CveLookupTool
from backend.database import Base
from backend.models.cve import CveIndexSettings, CveRecord
from backend.scripts.backfill_cve_affected_products import run_backfill


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    db.close()
    return engine, session_factory


def test_lookup_gating_transitions_after_backfill_and_emits_stable_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, session_factory = _build_session()
    try:
        db = session_factory()
        db.add(CveIndexSettings(enabled=True, daily_sync_hour_utc=2))
        db.flush()

        # Seed a pending state that becomes terminal non_projectable after backfill.
        record = CveRecord(
            id=1,
            cve_id="CVE-2026-5555",
            source="cvelist_v5",
            record_state="published",
            projection_status="pending",
            cve_json={
                "containers": {
                    "cna": {
                        "affected": []
                    }
                }
            },
        )
        db.add(record)
        db.flush()
        db.commit()
        db.close()

        monkeypatch.setenv("ENABLE_KNOWLEDGE_CVE_LOOKUP", "true")
        monkeypatch.setattr("backend.database.SessionLocal", lambda: session_factory())

        tool = CveLookupTool()
        blocked = tool.validate_and_run({"product": "widget", "version": "1.0.0"})

        assert blocked.success is True
        blocked_payload = json.loads(blocked.stdout)
        assert blocked_payload["status"] == "partial_index"
        assert blocked_payload["coverage"]["is_partial"] is True
        assert blocked_payload["coverage"]["record_count"] == 1
        assert blocked_payload["coverage"]["pending_count"] == 1

        backfill_db = session_factory()
        result = run_backfill(db=backfill_db, batch_size=100)
        backfill_db.commit()
        backfill_db.close()
        assert result["ok"] is True
        assert result["projection_ready"] is True

        ok = tool.validate_and_run({"product": "widget", "version": "1.0.0"})

        assert ok.success is True
        payload = json.loads(ok.stdout)
        assert payload["status"] == "no_matches"
        assert payload["coverage"]["is_partial"] is False
        assert payload["matches"] == []
        assert ok.metadata["cve_lookup"]["availability"]["projection_ready"] is True
    finally:
        engine.dispose()
