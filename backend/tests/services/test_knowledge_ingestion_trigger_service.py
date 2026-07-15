"""Tests for runtime ingestion trigger payload forwarding behavior.

Scope:
- Verify trigger worker forwards compact output and post-tool candidate payload
  into KnowledgeIngestionService without constructing ingestion-time LLMClients.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.services.knowledge import ingestion_trigger_service as trigger_service


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def test_run_execution_ingestion_once_forwards_post_tool_candidate_payload(
    monkeypatch,
) -> None:
    engine, db = _build_session()
    try:
        captured: dict[str, Any] = {}

        monkeypatch.setattr(
            "backend.database.SessionLocal",
            lambda: db,
        )

        class _FakeIngestionService:
            def __init__(self, _db, **_kwargs):
                pass

            def ingest_execution(self, **kwargs):
                captured.update(kwargs)
                return {"ok": True}

        monkeypatch.setattr(
            "backend.services.knowledge.ingestion_service.KnowledgeIngestionService",
            _FakeIngestionService,
        )

        candidate_payload: Mapping[str, Any] = {
            "candidate_observations": [
                {
                    "observation_type": "finding.vulnerability_detected",
                    "subject_type": "finding.instance",
                    "subject_key_hint": "cve-2024-0001:service.socket:10.0.0.8/tcp/5432",
                    "assertion_level": "candidate",
                    "confidence": 0.85,
                    "attributes": [{"key": "version", "value": "11.5"}],
                    "rationale": "Version appears vulnerable by advisory matrix.",
                    "evidence_refs": [
                        {
                            "source_artifact_id": "artifact-1",
                            "excerpt": "PostgreSQL 11.5 reported by scanner output",
                        }
                    ],
                    "vulnerability_confidence": 0.91,
                }
            ],
            "analyst_notes": [],
            "no_signal": False,
        }
        candidate_usage = {
            "input_tokens": 120,
            "output_tokens": 90,
            "total_tokens": 210,
            "estimated_cost_usd": 0.0,
        }
        trigger_service.run_execution_ingestion_once(
            task_id=1,
            execution_id="exec-1",
            tool_name="information_gathering.network_discovery.nmap",
            compact_output={"summary": "compact summary"},
            post_tool_candidate_payload=candidate_payload,
            post_tool_candidate_usage=candidate_usage,
        )

        assert captured.get("task_id") == 1
        assert captured.get("source_execution_id") == "exec-1"
        assert captured.get("tool_name_hint") == "information_gathering.network_discovery.nmap"
        assert captured.get("compact_output_hint") == {"summary": "compact summary"}
        assert captured.get("post_tool_candidate_payload") == dict(candidate_payload)
        assert captured.get("post_tool_candidate_usage") == dict(candidate_usage)
    finally:
        db.close()
        engine.dispose()


def test_run_execution_ingestion_once_handles_null_candidate_payload(monkeypatch) -> None:
    engine, db = _build_session()
    try:
        captured: dict[str, Any] = {}

        monkeypatch.setattr(
            "backend.database.SessionLocal",
            lambda: db,
        )

        class _FakeIngestionService:
            def __init__(self, _db, **_kwargs):
                pass

            def ingest_execution(self, **kwargs):
                captured.update(kwargs)
                return {"ok": True}

        monkeypatch.setattr(
            "backend.services.knowledge.ingestion_service.KnowledgeIngestionService",
            _FakeIngestionService,
        )

        trigger_service.run_execution_ingestion_once(
            task_id=42,
            execution_id="exec-42",
            tool_name="shell.exec",
            compact_output={},
            post_tool_candidate_payload=None,
            post_tool_candidate_usage=None,
        )

        assert captured.get("post_tool_candidate_payload") is None
        assert captured.get("post_tool_candidate_usage") is None
    finally:
        db.close()
        engine.dispose()
