"""Tests for replay boundary over durable ingestion runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant, TenantMembership
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.candidate_extraction import (
    CandidateExtractionResult,
    CandidateExtractionUsageSummary,
)
from backend.services.knowledge.contracts import ObservationCreate
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.replay_source_resolver import KnowledgeReplaySourceResolver
from backend.services.knowledge.replay_service import KnowledgeReplayService


class _FakeCandidateExtractionService:
    def __init__(self) -> None:
        self.calls = []

    def extract_candidates_sync(self, *, request):
        self.calls.append(request)
        evidence_id = str(request.evidence_archive_ids[0]) if request.evidence_archive_ids else ""
        return CandidateExtractionResult.succeeded(
            observations=[
                ObservationCreate(
                    engagement_id=int(request.engagement_id),
                    task_id=request.task_id,
                    source_execution_id=str(request.source_execution_id),
                    ingestion_run_id=str(request.ingestion_run_id),
                    observation_type="finding.vulnerability_detected",
                    subject_type="finding.instance",
                    subject_key="finding.instance:candidate-replay:test",
                    assertion_level="candidate",
                    payload={
                        "title": "Replay Candidate",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": evidence_id,
                                "excerpt": "replay evidence",
                            }
                        ],
                    },
                    observation_metadata={
                        "source_kind": "llm_candidate",
                        "extractor_family": str(request.extractor_family),
                        "extractor_version": str(request.extractor_version),
                        "extraction_mode": str(request.extraction_mode),
                        "durable_masking_applied": False,
                        "audit_summary": {"llm_status": "succeeded"},
                    },
                )
            ],
            evidence_archive_ids_used=request.evidence_archive_ids,
            usage_summary=CandidateExtractionUsageSummary(
                input_tokens=25,
                output_tokens=10,
                total_tokens=35,
                estimated_cost_usd=0.0025,
            ),
        )


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_engagement_task(db):
    user = User(username="knowledge-replay-user", password="secret")
    db.add(user)
    db.flush()
    tenant = Tenant(slug=f"replay-tenant-{uuid_lib.uuid4()}", name="Replay Tenant")
    db.add(tenant)
    db.flush()
    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
    db.flush()
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Replay Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Replay Task",
    )
    db.add(task)
    db.flush()
    return user, engagement, task


def _seed_execution_with_text_artifact(db, *, task_id: int) -> str:
    tenant_id = db.query(Task.tenant_id).filter(Task.id == int(task_id)).scalar()
    if tenant_id is None:
        raise ValueError(f"Task {task_id} has no tenant_id")
    return _seed_execution_with_artifact_payload(
        db,
        task_id=task_id,
        tenant_id=int(tenant_id),
        tool_name="shell.exec",
        tool_arguments={"command": "echo replay"},
        content_text="replay artifact",
        execution_metadata=None,
    )


def _seed_execution_with_artifact_payload(
    db,
    *,
    task_id: int,
    tenant_id: int,
    tool_name: str,
    tool_arguments: dict,
    content_text: str,
    execution_metadata: dict | None,
) -> str:
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata=execution_metadata,
    )
    db.add(execution)
    db.flush()
    db.add(
        ExecutionArtifact(
            id=uuid_lib.uuid4(),
            execution_id=execution.id,
            tenant_id=tenant_id,
            task_id=task_id,
            artifact_kind="stdout",
            content_text=content_text,
            content_sha256="c" * 64,
            byte_size=len(content_text.encode("utf-8")),
            mime_type="text/plain",
            is_text=True,
        )
    )
    db.flush()
    return str(execution.id)


def test_replay_execution_creates_new_run_with_explicit_target_version() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        ingestion_service = KnowledgeIngestionService(db)

        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            target_extractor_version="1.1",
        )

        assert initial["ok"] is True
        assert replay["ok"] is True
        assert replay["extractor_version"] == "1.1"
        assert replay["replay_source_type"] == "runtime"
        assert replay["ingestion_run_id"] != initial["ingestion_run_id"]
        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        replay_metadata = dict(replay_run.run_metadata or {})
        assert replay_metadata.get("replay_source_type") == "runtime"
        assert replay_metadata.get("replay_usage_summary") == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
        replay_audit = dict(replay_metadata.get("replay_audit_summary") or {})
        assert replay_audit.get("outcome_ok") is True
        assert replay_audit.get("status") == "succeeded"
        assert replay_audit.get("replay_source_type") == "runtime"
        assert float(replay_audit.get("duration_seconds") or 0.0) >= 0.0
        archive_count = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .count()
        )

        runs = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.source_execution_id == execution_id)
            .all()
        )
        versions = sorted(run.extractor_version for run in runs)
        assert "1.0" in versions
        assert "1.1" in versions
        assert archive_count == 1
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_autogenerates_new_replay_version_without_task_rerun() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        ingestion_service = KnowledgeIngestionService(db)
        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)

        first = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )
        second = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )

        assert first["ok"] is True
        assert second["ok"] is True
        assert first["extractor_version"] == "replay.1"
        assert second["extractor_version"] == "replay.2"
        assert first["replay_source_type"] == "runtime"
        assert second["replay_source_type"] == "runtime"

        tool_execution_count = (
            db.query(ToolExecution)
            .filter(ToolExecution.task_id == task.id)
            .count()
        )
        archive_count = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .count()
        )
        assert tool_execution_count == 1
        assert archive_count == 1
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_rejects_existing_target_extractor_version() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        ingestion_service = KnowledgeIngestionService(db)
        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)

        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        assert initial["ok"] is True

        try:
            replay_service.replay_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                extractor_family="runtime.ingestion",
                target_extractor_version="1.0",
            )
            assert False, "Expected replay to reject existing extractor version"
        except ValueError as exc:
            assert "already exists" in str(exc)

        run_count = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.source_execution_id == execution_id)
            .count()
        )
        assert run_count == 1
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_candidate_family_propagates_version_and_records_summary(monkeypatch) -> None:
    engine, db = _build_session()
    try:
        monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        fake_candidate_service = _FakeCandidateExtractionService()
        ingestion_service = KnowledgeIngestionService(
            db,
            candidate_extraction_service=fake_candidate_service,
        )
        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)

        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="llm.candidate_extraction",
            target_extractor_version="2.1",
        )

        assert replay["ok"] is True
        assert replay["extractor_family"] == "llm.candidate_extraction"
        assert replay["extractor_version"] == "2.1"
        assert replay["replay_source_type"] == "runtime"
        assert len(fake_candidate_service.calls) == 0
        summary = dict(replay["candidate_outcome_summary"] or {})
        assert summary.get("status") == "no_signal"
        assert summary.get("reason") == "post_tool_candidate_payload_missing"
        assert summary.get("extractor_family") == "llm.candidate_extraction"
        assert summary.get("extractor_version") == "2.1"
        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        replay_metadata = dict(replay_run.run_metadata or {})
        assert replay_metadata.get("replay_usage_summary") == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_rejects_remote_runtime_candidate_family_when_feature_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "false")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        replay_service = KnowledgeReplayService(db, ingestion_service=KnowledgeIngestionService(db))
        with pytest.raises(ValueError, match="ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION"):
            replay_service.replay_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                extractor_family="llm.candidate_extraction",
                target_extractor_version="2.1",
            )
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_uses_durable_fallback_after_task_deletion() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        ingestion_service = KnowledgeIngestionService(db)
        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )

        assert replay["ok"] is True
        assert replay["replay_source_type"] == "durable_archive"
        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        assert dict(replay_run.run_metadata or {}).get("replay_source_type") == "durable_archive"
        runs = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.source_execution_id == execution_id)
            .all()
        )
        assert len(runs) >= 2
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_reads_archived_file_when_inline_excerpt_missing() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        nmap_output = "\n".join(
            [
                "Nmap scan report for 10.10.10.9",
                "22/tcp open ssh",
            ]
        )
        execution_id = _seed_execution_with_artifact_payload(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            tool_name="information_gathering.network_discovery.nmap",
            tool_arguments={"target": "10.10.10.9"},
            content_text=(nmap_output + "\n" + ("x" * 20000)),
            execution_metadata={"tool_metadata": {}},
        )
        ingestion_service = KnowledgeIngestionService(db)
        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True

        archived = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert archived.storage_mode == "archived_file"
        archived.inline_excerpt = None
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )
        assert replay["ok"] is True

        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        inserted = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == replay_run.id)
            .count()
        )
        assert inserted >= 2
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_uses_object_backed_rows_without_provider_file_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact_payload(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo replay"},
            content_text="x" * 20000,
            execution_metadata={
                "tool_metadata": {"parsed_source": "shell.exec.parse_output"},
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [{"evidence_kind": "port_banner", "port": 443}],
                "semantic_schema_version": "network.v2",
                "capability_family": "network_discovery",
            },
        )
        ingestion_service = KnowledgeIngestionService(db)
        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True

        archived = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        archived.storage_mode = "object_ref"
        archived.inline_excerpt = None
        archived.archived_file_ref = None
        archived.object_key = (
            f"tenants/{task.tenant_id}/engagements/{task.engagement_id}/knowledge/evidence/{archived.id}.txt"
        )
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        def _unexpected_provider_read(*_args, **_kwargs):
            raise AssertionError("replay unexpectedly attempted runtime provider file read")

        monkeypatch.setattr(
            "backend.services.runtime_provider.runtime_artifact_access.run_provider_operation_sync",
            _unexpected_provider_read,
        )

        replay_service = KnowledgeReplayService(db, ingestion_service=ingestion_service)
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )

        assert replay["ok"] is True
        assert replay["replay_source_type"] == "durable_archive"
        replay_run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == replay["ingestion_run_id"])
            .one()
        )
        replay_snapshot = dict((replay_run.run_metadata or {}).get("semantic_input_snapshot") or {})
        assert replay_snapshot.get("capability_family") == "network_discovery"
        assert replay_snapshot.get("semantic_schema_version") == "network.v2"
        assert replay_snapshot.get("semantic_observations") == [
            {"observation_type": "network.open_port"}
        ]
        assert replay_snapshot.get("semantic_evidence") == [
            {"evidence_kind": "port_banner", "port": 443}
        ]
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_preserves_artifact_text_adapter_observations_from_object_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        gobuster_output = "\n".join(
            [
                "/admin (Status: 403) [Size: 12]",
                "/login (Status: 200) [Size: 45]",
            ]
        )
        execution_id = _seed_execution_with_artifact_payload(
            db,
            task_id=task.id,
            tenant_id=task.tenant_id,
            tool_name="web_applications.web_crawlers.gobuster",
            tool_arguments={"target": "http://example.test"},
            content_text=gobuster_output + ("\n" + ("x" * 20000)),
            execution_metadata={"tool_metadata": {}},
        )
        ingestion_service = KnowledgeIngestionService(db)
        initial = ingestion_service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            delete_survival_required=True,
            raise_on_error=True,
        )
        assert initial["ok"] is True
        initial_count = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == initial["ingestion_run_id"])
            .filter(KnowledgeObservation.observation_type == "web.path_discovered")
            .count()
        )
        assert initial_count == 2

        archived = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        archived.storage_mode = "object_ref"
        archived.inline_excerpt = None
        archived.archived_file_ref = None
        archived.object_key = (
            f"tenants/{task.tenant_id}/engagements/{task.engagement_id}/knowledge/evidence/{archived.id}.txt"
        )
        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        object_store.put_bytes(
            str(archived.object_key),
            gobuster_output.encode("utf-8"),
            content_type="text/plain",
        )
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        def _unexpected_provider_read(*_args, **_kwargs):
            raise AssertionError("replay unexpectedly attempted runtime provider file read")

        monkeypatch.setattr(
            "backend.services.runtime_provider.runtime_artifact_access.run_provider_operation_sync",
            _unexpected_provider_read,
        )

        replay_service = KnowledgeReplayService(
            db,
            ingestion_service=ingestion_service,
            replay_source_resolver=KnowledgeReplaySourceResolver(
                db,
                query_service=ingestion_service.query_service,
                object_store=object_store,
            ),
        )
        replay = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )

        assert replay["ok"] is True
        assert replay["replay_source_type"] == "durable_archive"
        replay_count = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == replay["ingestion_run_id"])
            .filter(KnowledgeObservation.observation_type == "web.path_discovered")
            .count()
        )
        assert replay_count == initial_count
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_fails_when_runtime_and_durable_sources_are_missing() -> None:
    engine, db = _build_session()
    try:
        replay_service = KnowledgeReplayService(db)
        with pytest.raises(ValueError) as exc:
            replay_service.replay_execution(
                task_id=None,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion",
            )
        assert "Replay source not found" in str(exc.value)
    finally:
        db.close()
        engine.dispose()


def test_replay_execution_emits_metrics_for_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inc_calls: list[tuple[str, int]] = []
    gauge_calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        "backend.services.knowledge.replay_service.safe_inc",
        lambda name, value=1: inc_calls.append((str(name), int(value))),
    )
    monkeypatch.setattr(
        "backend.services.knowledge.replay_service.safe_gauge",
        lambda name, value: gauge_calls.append((str(name), float(value))),
    )

    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_text_artifact(db, task_id=task.id)
        replay_service = KnowledgeReplayService(db, ingestion_service=KnowledgeIngestionService(db))
        ok_result = replay_service.replay_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
        )
        assert ok_result["ok"] is True

        with pytest.raises(ValueError):
            replay_service.replay_execution(
                task_id=None,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion",
            )
    finally:
        db.close()
        engine.dispose()

    counter_totals: dict[str, int] = {}
    for name, value in inc_calls:
        counter_totals[name] = counter_totals.get(name, 0) + value
    assert counter_totals.get("knowledge_replay_total", 0) >= 1
    assert counter_totals.get("knowledge_replay_failed_total", 0) >= 1
    assert any(name == "knowledge_replay_duration_seconds" for name, _ in gauge_calls)
