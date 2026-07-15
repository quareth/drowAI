"""Tests for operational historical backfill completion-gate orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.tenant import Tenant
from backend.services.knowledge.contracts import IngestionRunCreate, ObservationCreate
from backend.services.knowledge.historical_backfill_service import (
    KnowledgeHistoricalBackfillService,
)
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_engagement(db, *, username_suffix: str, engagement_name: str) -> Engagement:
    tenant = db.get(Tenant, 1)
    if tenant is None:
        tenant = Tenant(id=1, slug="backfill", name="Backfill")
        db.add(tenant)
        db.flush()
    user = User(username=f"execution-plane-backfill-{username_suffix}-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=tenant.id, name=engagement_name, status="active")
    db.add(engagement)
    db.flush()
    return engagement


def _append_observations(
    db,
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    observations: list[ObservationCreate],
    extractor_family: str = "runtime.ingestion.test",
    extractor_version: str | None = None,
) -> None:
    ingestion = KnowledgeIngestionService(db)
    run = ingestion.create_or_get_ingestion_run(
        IngestionRunCreate(
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=None,
            source_execution_id=str(source_execution_id),
            extractor_family=str(extractor_family),
            extractor_version=str(extractor_version or f"seed-{source_execution_id}"),
        )
    )
    normalized = [
        ObservationCreate(
            user_id=int(item.user_id),
            engagement_id=int(item.engagement_id),
            task_id=item.task_id,
            source_execution_id=str(item.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type=str(item.observation_type),
            subject_type=str(item.subject_type),
            subject_key=str(item.subject_key),
            assertion_level=str(item.assertion_level),
            payload=dict(item.payload or {}),
            observed_at=item.observed_at,
            dedupe_key=item.dedupe_key,
        )
        for item in observations
    ]
    ingestion.insert_observations(
        ingestion_run_id=str(run.id),
        observations=normalized,
    )


def _observation_rows(
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    host_ip: str,
    port: int,
    base_time: datetime,
) -> list[ObservationCreate]:
    return [
        ObservationCreate(
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key=f"host.ip:{host_ip}",
            assertion_level="observed",
            payload={"host_status": "up", "confidence": "medium"},
            observed_at=base_time,
        ),
        ObservationCreate(
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="network.open_port",
            subject_type="service.socket",
            subject_key=f"service.socket:{host_ip}/tcp/{port}",
            assertion_level="observed",
            payload={"confidence": "medium"},
            observed_at=base_time + timedelta(seconds=1),
        ),
    ]


def _web_path_observation_rows(
    *,
    user_id: int,
    engagement_id: int,
    source_execution_id: str,
    canonical_url: str,
    base_time: datetime,
) -> list[ObservationCreate]:
    return [
        ObservationCreate(
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=None,
            source_execution_id=source_execution_id,
            ingestion_run_id="seed-placeholder",
            observation_type="web.path_discovered",
            subject_type="web.path",
            subject_key=f"web.path:{canonical_url}",
            assertion_level="observed",
            payload={
                "source": "ffuf",
                "status_code": 200,
                "response_size": 321,
            },
            observed_at=base_time,
        ),
    ]


def test_historical_backfill_gate_reports_per_engagement_success_and_idempotency() -> None:
    engine, db = _build_session()
    try:
        engagement_a = _seed_engagement(db, username_suffix="a", engagement_name="Backfill A")
        engagement_b = _seed_engagement(db, username_suffix="b", engagement_name="Backfill B")
        now = datetime.now(timezone.utc)

        exec_a = str(uuid_lib.uuid4())
        exec_b = str(uuid_lib.uuid4())
        _append_observations(
            db,
            user_id=engagement_a.user_id,
            engagement_id=engagement_a.id,
            source_execution_id=exec_a,
            observations=_observation_rows(
                user_id=engagement_a.user_id,
                engagement_id=engagement_a.id,
                source_execution_id=exec_a,
                host_ip="10.10.60.1",
                port=443,
                base_time=now,
            ),
        )
        _append_observations(
            db,
            user_id=engagement_b.user_id,
            engagement_id=engagement_b.id,
            source_execution_id=exec_b,
            observations=_observation_rows(
                user_id=engagement_b.user_id,
                engagement_id=engagement_b.id,
                source_execution_id=exec_b,
                host_ip="10.10.70.1",
                port=22,
                base_time=now + timedelta(minutes=1),
            ),
        )

        result = KnowledgeHistoricalBackfillService(db).run_backfill()

        assert result["completion_gate_passed"] is True
        assert result["targeted_engagement_count"] == 2
        assert result["attempted_engagement_count"] == 2
        assert result["succeeded_engagement_count"] == 2
        assert result["failed_engagement_count"] == 0
        assert result["failed_engagements"] == []
        assert result["web_path_upsert_count"] == 0
        assert result["web_path_insert_count"] == 0

        statuses = sorted(
            list(result["engagement_statuses"]),
            key=lambda item: int(item["engagement_id"]),
        )
        assert [int(status["engagement_id"]) for status in statuses] == sorted(
            [engagement_a.id, engagement_b.id]
        )
        for status in statuses:
            assert status["attempted"] is True
            assert status["status"] == "succeeded"
            assert status["result"] is not None
            assert status["idempotent_rerun"]["checked"] is True
            assert status["idempotent_rerun"]["ok"] is True
            assert status["idempotent_rerun"]["before_counts"] == status["idempotent_rerun"]["after_counts"]
    finally:
        db.close()
        engine.dispose()


def test_historical_backfill_summary_includes_web_path_counters_when_enabled() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="web", engagement_name="Backfill Web")
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        _append_observations(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            source_execution_id=source_execution_id,
            observations=[
                *_observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=source_execution_id,
                    host_ip="10.10.81.1",
                    port=8443,
                    base_time=now,
                ),
                *_web_path_observation_rows(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    source_execution_id=source_execution_id,
                    canonical_url="https://10.10.81.1/admin",
                    base_time=now + timedelta(seconds=2),
                ),
            ],
        )

        result = KnowledgeHistoricalBackfillService(db).run_backfill(
            target_engagement_ids=[engagement.id],
            verify_idempotent_rerun=True,
        )

        assert result["completion_gate_passed"] is True
        assert result["web_path_upsert_count"] >= 1
        assert result["web_path_insert_count"] >= 1
        status = result["engagement_statuses"][0]
        assert int(status["result"]["web_path_upsert_count"]) >= 1
        assert int(status["result"]["web_path_insert_count"]) >= 1
        assert "web_paths" in status["idempotent_rerun"]["before_counts"]
        assert "engagement_web_path_links" in status["idempotent_rerun"]["before_counts"]
    finally:
        db.close()
        engine.dispose()


def test_historical_backfill_emits_knowledge_backfill_total_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inc_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "backend.services.knowledge.historical_backfill_service.safe_inc",
        lambda name, value=1: inc_calls.append((str(name), int(value))),
    )

    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="metric", engagement_name="Backfill Metric")
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        _append_observations(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            source_execution_id=source_execution_id,
            observations=_observation_rows(
                user_id=engagement.user_id,
                engagement_id=engagement.id,
                source_execution_id=source_execution_id,
                host_ip="10.10.80.1",
                port=8080,
                base_time=now,
            ),
        )

        result = KnowledgeHistoricalBackfillService(db).run_backfill()
        assert result["completion_gate_passed"] is True
    finally:
        db.close()
        engine.dispose()

    totals: dict[str, int] = {}
    for name, value in inc_calls:
        totals[name] = totals.get(name, 0) + value
    assert totals.get("knowledge_backfill_total", 0) >= 1


class _FailingRebuildService:
    def __init__(self, *, failing_engagement_id: int) -> None:
        self.failing_engagement_id = int(failing_engagement_id)
        self.calls: list[int] = []

    def rebuild_engagement(self, *, engagement_id: int) -> dict[str, Any]:
        self.calls.append(int(engagement_id))
        if int(engagement_id) == self.failing_engagement_id:
            raise RuntimeError("synthetic rebuild failure")
        return {"ok": True, "scope": "engagement", "engagement_id": int(engagement_id)}


def test_historical_backfill_gate_captures_failed_engagement_rerun_plan() -> None:
    engine, db = _build_session()
    try:
        stub = _FailingRebuildService(failing_engagement_id=2)
        result = KnowledgeHistoricalBackfillService(db, rebuild_service=stub).run_backfill(
            target_engagement_ids=[1, 2],
            verify_idempotent_rerun=True,
        )

        assert result["completion_gate_passed"] is False
        assert result["attempted_engagement_count"] == 2
        assert result["succeeded_engagement_count"] == 1
        assert result["failed_engagement_count"] == 1
        assert len(result["failed_engagements"]) == 1
        failed = result["failed_engagements"][0]
        assert failed["engagement_id"] == 2
        assert "synthetic rebuild failure" in failed["error_reason"]
        assert failed["rerun_plan"]["operation"] == (
            "knowledge_read_model_rebuild_service.rebuild_engagement"
        )
        assert failed["rerun_plan"]["params"]["engagement_id"] == 2
    finally:
        db.close()
        engine.dispose()


def test_post_replay_backfill_verification_reports_success_with_idempotent_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, username_suffix="candidate-replay-ok", engagement_name="Candidate Replay Verify OK")
        now = datetime.now(timezone.utc)
        source_execution_id = str(uuid_lib.uuid4())
        _append_observations(
            db,
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            source_execution_id=source_execution_id,
            observations=[
                ObservationCreate(
                    user_id=engagement.user_id,
                    engagement_id=engagement.id,
                    task_id=None,
                    source_execution_id=source_execution_id,
                    ingestion_run_id="seed-placeholder",
                    observation_type="finding.vulnerability_detected",
                    subject_type="finding.instance",
                    subject_key="finding.instance:candidate-verify-ok",
                    assertion_level="candidate",
                    payload={
                        "evidence_refs": [
                            {"evidence_archive_id": "evidence-1", "excerpt": "candidate marker"}
                        ]
                    },
                    observation_metadata={
                        "source_kind": "llm_candidate",
                        "extractor_family": "llm.candidate_extraction",
                        "extractor_version": "2.1",
                        "extraction_mode": "candidate_replay",
                        "durable_masking_applied": False,
                        "audit_summary": {"llm_status": "succeeded"},
                    },
                    observed_at=now,
                )
            ],
            extractor_family="llm.candidate_extraction",
            extractor_version="2.1",
        )

        result = KnowledgeHistoricalBackfillService(db).verify_after_replay_backfill(
            target_engagement_ids=[engagement.id],
            verify_idempotent_rerun=True,
            replay_extractor_family="llm.candidate_extraction",
            replay_extractor_version="2.1",
            require_replay_runs=True,
        )

        assert result["completion_gate_passed"] is True
        assert result["verification_scope"] == "post_replay_backfill"
        assert result["targeted_engagement_count"] == 1
        assert result["succeeded_engagement_count"] == 1
        assert result["failed_engagement_count"] == 0
        assert result["web_path_upsert_count"] == 0
        assert result["web_path_insert_count"] == 0
        status = result["engagement_statuses"][0]
        assert status["status"] == "succeeded"
        assert status["idempotent_rerun"]["checked"] is True
        assert status["idempotent_rerun"]["ok"] is True
        assert int(status["replay_context"]["matching_run_count"]) == 1
        assert source_execution_id in list(status["replay_context"]["matched_source_execution_ids"])
    finally:
        db.close()
        engine.dispose()


def test_post_replay_backfill_verification_fails_when_replay_runs_missing_with_rerun_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(
            db,
            username_suffix="candidate-replay-missing",
            engagement_name="Candidate Replay Verify Missing",
        )
        result = KnowledgeHistoricalBackfillService(db).verify_after_replay_backfill(
            target_engagement_ids=[engagement.id],
            verify_idempotent_rerun=True,
            replay_extractor_family="llm.candidate_extraction",
            replay_extractor_version="9.9",
            require_replay_runs=True,
        )
        assert result["completion_gate_passed"] is False
        assert result["failed_engagement_count"] == 1
        status = result["engagement_statuses"][0]
        assert status["attempted"] is False
        assert status["status"] == "failed"
        assert int(status["replay_context"]["matching_run_count"]) == 0
        failed = result["failed_engagements"][0]
        assert failed["engagement_id"] == engagement.id
        assert failed["rerun_plan"]["operation"] == "knowledge_replay_service.replay_execution"
        assert failed["rerun_plan"]["params"]["extractor_family"] == "llm.candidate_extraction"
        assert failed["rerun_plan"]["params"]["target_extractor_version"] == "9.9"
    finally:
        db.close()
        engine.dispose()


def test_post_replay_backfill_verification_fails_when_scope_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        result = KnowledgeHistoricalBackfillService(db).verify_after_replay_backfill(
            target_engagement_ids=None,
            verify_idempotent_rerun=True,
            replay_extractor_family="llm.candidate_extraction",
            replay_extractor_version="1.0",
            require_replay_runs=True,
        )

        assert result["ok"] is False
        assert result["completion_gate_passed"] is False
        assert result["targeted_engagement_count"] == 0
        assert result["attempted_engagement_count"] == 0
        assert result["failed_engagement_count"] == 0
        assert "No matching replay/backfill ingestion runs found" in str(result["error_reason"])
        assert result["rerun_plan"]["operation"] == "knowledge_replay_service.replay_execution"
        assert result["rerun_plan"]["params"]["extractor_family"] == "llm.candidate_extraction"
        assert result["rerun_plan"]["params"]["target_extractor_version"] == "1.0"
    finally:
        db.close()
        engine.dispose()


def test_post_replay_backfill_verification_fails_fast_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "false")
    engine, db = _build_session()
    try:
        result = KnowledgeHistoricalBackfillService(db).verify_after_replay_backfill(
            target_engagement_ids=[1],
            verify_idempotent_rerun=True,
            replay_extractor_family="llm.candidate_extraction",
            replay_extractor_version="1.0",
            require_replay_runs=True,
        )
        assert result["ok"] is False
        assert result["completion_gate_passed"] is False
        assert "ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION is false" in str(result["error_reason"])
        assert result["targeted_engagement_count"] == 0
        assert result["rerun_plan"]["operation"] == "set ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION=true"
    finally:
        db.close()
        engine.dispose()
