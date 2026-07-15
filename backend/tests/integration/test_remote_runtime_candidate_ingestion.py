"""Integration tests for post-tool candidate payload ingestion behavior.

This module verifies candidate-only ingestion is persisted durably, hidden from
authoritative query rollups by default, and sourced from post-tool decision
payloads without ingestion-time candidate extractor LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeFinding, KnowledgeIngestionRun, KnowledgeObservation, KnowledgeService
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.query_service import FindingsFilters, KnowledgeQueryService


@pytest.fixture(autouse=True)
def _enable_candidate_feature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db):
    user = User(username=f"candidate-replay-integration-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, name="Candidate Replay Integration Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, name="Candidate Replay Integration Task")
    db.add(task)
    db.flush()
    return engagement, task


def _seed_execution_with_stdout_artifact(
    db,
    *,
    task_id: int,
    content_text: str,
    tool_name: str = "shell.exec",
    command: str = "echo candidate-replay",
) -> tuple[str, str]:
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        task_id=task_id,
        tool_name=tool_name,
        tool_arguments={"command": command},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    db.flush()
    artifact = ExecutionArtifact(
        id=uuid_lib.uuid4(),
        execution_id=execution.id,
        task_id=task_id,
        artifact_kind="stdout",
        content_text=content_text,
        content_sha256="e" * 64,
        byte_size=len(content_text.encode("utf-8")),
        mime_type="text/plain",
        is_text=True,
    )
    db.add(artifact)
    db.flush()
    return str(execution.id), str(artifact.id)


def _nmap_service_stdout() -> str:
    return "\n".join(
        [
            "Starting Nmap 7.94 ( https://nmap.org )",
            "Nmap scan report for 10.0.0.21",
            "Host is up (0.012s latency).",
            "PORT    STATE SERVICE VERSION",
            "443/tcp open  https   nginx 1.14.0",
            "",
        ]
    )


def _build_post_tool_payload(
    *,
    source_artifact_id: str,
    vulnerability_confidence: float,
) -> dict[str, object]:
    return {
        "candidate_observations": [
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.instance",
                "subject_key_hint": "cve-2024-9999:service.socket:10.0.0.21/tcp/443",
                "assertion_level": "candidate",
                "confidence": 0.92,
                "attributes": [{"key": "title", "value": "Potential TLS service vulnerability"}],
                "rationale": "Nmap service/version evidence suggests potential vulnerable build.",
                "evidence_refs": [
                    {
                        "source_artifact_id": source_artifact_id,
                        "excerpt": "443/tcp open https nginx 1.14.0",
                    }
                ],
                "vulnerability": {
                    "id": "CVE-2024-9999",
                    "title": "TLS service likely vulnerable to known issue",
                    "severity": "high",
                },
                "vulnerability_confidence": float(vulnerability_confidence),
            }
        ],
        "analyst_notes": [],
        "no_signal": False,
    }


def test_remote_runtime_nmap_candidate_above_threshold_projects_candidate_finding_and_hides_by_default() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        execution_id, source_artifact_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            content_text=_nmap_service_stdout(),
            tool_name="information_gathering.network_discovery.nmap",
            command="nmap -sV 10.0.0.21",
        )
        ingestion = KnowledgeIngestionService(db)
        ingest_result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=_build_post_tool_payload(
                source_artifact_id=source_artifact_id,
                vulnerability_confidence=0.92,
            ),
            post_tool_candidate_usage={
                "input_tokens": 200,
                "output_tokens": 140,
                "total_tokens": 340,
                "estimated_cost_usd": 0.0,
            },
            raise_on_error=True,
        )
        assert ingest_result["ok"] is True
        assert ingest_result["candidate_extraction_status"] == "ran"

        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == ingest_result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(run.run_metadata or {})
        adapter_stats = dict(run_metadata.get("adapter_stats") or {})
        assert run_metadata.get("candidate_extraction_status") == "ran"
        assert run_metadata.get("candidate_extraction_reason") == "candidates_extracted"
        assert int(adapter_stats.get("observation_count_non_finding_total") or 0) >= 1
        assert int(adapter_stats.get("observation_count_finding_total") or 0) == 0
        assert int(adapter_stats.get("observation_count_finding_authoritative") or 0) == 0
        assert run_metadata.get("candidate_usage_summary") == {
            "input_tokens": 200,
            "output_tokens": 140,
            "total_tokens": 340,
            "estimated_cost_usd": 0.0,
        }

        projected_service = (
            db.query(KnowledgeService)
            .filter(KnowledgeService.engagement_id == engagement.id)
            .one()
        )
        assert projected_service.service_key == "service.socket:10.0.0.21/tcp/443"
        assert projected_service.service_name == "https"

        query = KnowledgeQueryService(db)
        summary = query.get_summary(user_id=engagement.user_id)
        assert summary["open_findings_total"] == 0

        default_findings = query.list_findings(user_id=engagement.user_id, filters=FindingsFilters(limit=50, offset=0))
        assert default_findings["total"] == 0

        with_candidates = query.list_findings(
            user_id=engagement.user_id,
            filters=FindingsFilters(limit=50, offset=0, include_candidates=True),
        )
        assert with_candidates["total"] == 1
        row = with_candidates["items"][0]
        assert row["status"] == "candidate"
        assert row["assertion_level"] == "candidate"
        assert row["is_candidate"] is True
        assert row["authority_source_kind"] == "llm_candidate"

        persisted_finding = (
            db.query(KnowledgeFinding)
            .filter(
                KnowledgeFinding.engagement_id == engagement.id,
                KnowledgeFinding.status == "candidate",
            )
            .one()
        )
        assert persisted_finding.status == "candidate"
        assert persisted_finding.assertion_level == "candidate"
        assert persisted_finding.confidence == "high"
        assert persisted_finding.service_id == projected_service.id
        assert persisted_finding.asset_id == projected_service.asset_id
        persisted_metadata = dict(persisted_finding.finding_metadata or {})
        assert dict(persisted_metadata.get("authority") or {}) == {
            "source_kind": "llm_candidate",
            "candidate_only": True,
        }
    finally:
        db.close()
        engine.dispose()


def test_remote_runtime_nmap_candidate_below_threshold_does_not_create_candidate_finding_row() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        execution_id, source_artifact_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            content_text=_nmap_service_stdout(),
            tool_name="information_gathering.network_discovery.nmap",
            command="nmap -sV 10.0.0.21",
        )
        ingestion = KnowledgeIngestionService(db)
        ingest_result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=_build_post_tool_payload(
                source_artifact_id=source_artifact_id,
                vulnerability_confidence=0.79,
            ),
            raise_on_error=True,
        )

        assert ingest_result["ok"] is True
        assert ingest_result["candidate_extraction_status"] == "no_signal"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == ingest_result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(run.run_metadata or {})
        assert run_metadata.get("candidate_extraction_status") == "no_signal"
        assert run_metadata.get("candidate_observation_count") == 0
        drop_reasons = dict(run_metadata.get("candidate_vulnerability_drop_reasons") or {})
        assert int(drop_reasons.get("below_vulnerability_confidence_threshold") or 0) >= 1
        assert (
            db.query(KnowledgeService)
            .filter(KnowledgeService.engagement_id == engagement.id)
            .count()
        ) >= 1
        assert (
            db.query(KnowledgeFinding)
            .filter(
                KnowledgeFinding.engagement_id == engagement.id,
                KnowledgeFinding.status == "candidate",
            )
            .count()
        ) == 0
    finally:
        db.close()
        engine.dispose()


def test_remote_runtime_missing_post_tool_payload_records_no_signal_without_failing_ingestion() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        execution_id, _source_artifact_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            content_text="candidate payload missing case",
        )
        ingestion = KnowledgeIngestionService(db)
        ingest_result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=None,
            raise_on_error=True,
        )

        assert ingest_result["ok"] is True
        assert ingest_result["candidate_extraction_status"] == "no_signal"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == ingest_result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(run.run_metadata or {})
        assert run.status == "succeeded"
        assert run_metadata.get("candidate_extraction_status") == "no_signal"
        assert run_metadata.get("candidate_extraction_reason") == "post_tool_candidate_payload_missing"

        query = KnowledgeQueryService(db)
        summary = query.get_summary(user_id=engagement.user_id)
        assert summary["open_findings_total"] == 0
    finally:
        db.close()
        engine.dispose()


def test_remote_runtime_cve_lookup_candidate_flow_persists_candidate_only_and_hides_from_default_rollups() -> None:
    engine, db = _build_session()
    try:
        engagement, task = _seed_user_engagement_task(db)
        discovery_execution_id, _discovery_artifact_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            content_text=_nmap_service_stdout(),
            tool_name="information_gathering.network_discovery.nmap",
            command="nmap -sV 10.0.0.21",
        )
        ingestion = KnowledgeIngestionService(db)
        discovery_result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=discovery_execution_id,
            raise_on_error=True,
        )
        assert discovery_result["ok"] is True

        projected_service = (
            db.query(KnowledgeService)
            .filter(KnowledgeService.engagement_id == engagement.id)
            .one()
        )
        assert projected_service.service_key == "service.socket:10.0.0.21/tcp/443"

        lookup_execution_id, lookup_source_artifact_id = _seed_execution_with_stdout_artifact(
            db,
            task_id=task.id,
            content_text=(
                '{"tool":"knowledge.cve_lookup","status":"ok",'
                '"coverage":{"is_partial":false,"pending_count":0,'
                '"error_count":0,"projected_count":1,"record_count":1,"warning":""},'
                '"matches":[{"cve_id":"CVE-2024-9999","applicability":"possible"}],'
                '"message":"ok"}'
            ),
            tool_name="knowledge.cve_lookup",
            command="knowledge.cve_lookup product=nginx version=1.14.0",
        )
        lookup_result = ingestion.ingest_execution(
            task_id=task.id,
            source_execution_id=lookup_execution_id,
            post_tool_candidate_payload=_build_post_tool_payload(
                source_artifact_id=lookup_source_artifact_id,
                vulnerability_confidence=0.84,
            ),
            raise_on_error=True,
        )
        assert lookup_result["ok"] is True
        assert lookup_result["candidate_extraction_status"] == "ran"

        lookup_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == lookup_result["ingestion_run_id"])
            .one()
        )
        run_metadata = dict(lookup_run.run_metadata or {})
        adapter_stats = dict(run_metadata.get("adapter_stats") or {})
        assert run_metadata.get("source_tool_name") == "knowledge.cve_lookup"
        assert run_metadata.get("candidate_extraction_status") == "ran"
        assert run_metadata.get("candidate_extraction_reason") == "candidates_extracted"
        assert int(adapter_stats.get("observation_count_finding_authoritative") or 0) == 0

        candidate_observation = (
            db.query(KnowledgeObservation)
            .filter(
                KnowledgeObservation.ingestion_run_id == lookup_run.id,
                KnowledgeObservation.assertion_level == "candidate",
            )
            .one()
        )
        evidence_refs = list((candidate_observation.payload or {}).get("evidence_refs") or [])
        assert len(evidence_refs) == 1
        evidence_archive_id = str(evidence_refs[0].get("evidence_archive_id") or "")
        assert evidence_archive_id
        evidence_archive = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.id == evidence_archive_id)
            .one()
        )
        assert str(evidence_archive.source_execution_id) == lookup_execution_id
        assert str(evidence_archive.source_artifact_id) == lookup_source_artifact_id

        query = KnowledgeQueryService(db)
        summary = query.get_summary(user_id=engagement.user_id)
        assert summary["open_findings_total"] == 0

        default_findings = query.list_findings(user_id=engagement.user_id, filters=FindingsFilters(limit=50, offset=0))
        assert default_findings["total"] == 0

        with_candidates = query.list_findings(
            user_id=engagement.user_id,
            filters=FindingsFilters(limit=50, offset=0, include_candidates=True),
        )
        assert with_candidates["total"] == 1
        row = with_candidates["items"][0]
        assert row["status"] == "candidate"
        assert row["assertion_level"] == "candidate"
        assert row["is_candidate"] is True

        persisted_finding = (
            db.query(KnowledgeFinding)
            .filter(
                KnowledgeFinding.engagement_id == engagement.id,
                KnowledgeFinding.status == "candidate",
            )
            .one()
        )
        assert persisted_finding.status == "candidate"
        assert persisted_finding.assertion_level == "candidate"
        assert persisted_finding.confidence == "medium"
        assert persisted_finding.service_id == projected_service.id
        persisted_metadata = dict(persisted_finding.finding_metadata or {})
        assert dict(persisted_metadata.get("authority") or {}) == {
            "source_kind": "llm_candidate",
            "candidate_only": True,
        }
    finally:
        db.close()
        engine.dispose()
