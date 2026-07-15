"""Tests for ingestion write-boundary behavior and lineage safety."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any
import uuid as uuid_lib

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.delete_guard_service import KnowledgeDeleteGuardService
from backend.services.knowledge.contracts import (
    IngestionRunCreate as _IngestionRunCreate,
    IngestionRunStatus,
    ObservationCreate as _ObservationCreate,
)
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService

IngestionRunCreate = partial(_IngestionRunCreate, user_id=1)
ObservationCreate = partial(_ObservationCreate, user_id=1)


class _FailingAdapter:
    tool_names = ("shell.exec",)
    capability_families = ()

    def extract(self, context):
        raise RuntimeError("adapter extraction failed")


class _LeakyFailingAdapter:
    tool_names = ("shell.exec",)
    capability_families = ()

    def extract(self, context):
        raise RuntimeError(
            "adapter extraction failed token=sk-live-123456789 bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x.y"  # gitleaks:allow
        )


class _FailingAdapterRegistry:
    def build_context(
        self,
        *,
        user_id,
        engagement_id,
        task_id,
        source_execution_id,
        ingestion_run_id,
        execution_payload,
        tenant_id=None,
        compact_output_hint=None,
        artifact_reader=None,
    ):
        class _Context:
            def source_tool_name(self_inner):
                execution = execution_payload.get("execution") or {}
                return str(execution.get("tool_name") or "")

            def select_authoritative_input_source(self_inner):
                return "tool_metadata"

        return _Context()

    def resolve_adapters(self, context):
        return [_FailingAdapter()]


class _LeakyFailingAdapterRegistry(_FailingAdapterRegistry):
    def resolve_adapters(self, context):
        return [_LeakyFailingAdapter()]


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db, *, tenant_id: int = 1):
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (:id, :slug, :name, CURRENT_TIMESTAMP)"
        ),
        {"id": int(tenant_id), "slug": f"tenant-{tenant_id}", "name": f"Tenant {tenant_id}"},
    )
    user = User(username="knowledge-ingestion-user", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=tenant_id, name="Runtime Ingestion Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, tenant_id=tenant_id, name="Runtime Ingestion Task")
    db.add(task)
    db.flush()
    return user, engagement, task


def _seed_execution_with_artifact(
    db,
    *,
    task_id: int,
    tool_name: str = "shell.exec",
    artifact_kind: str = "stdout",
    content_text: str | None = "tool output",
    is_text: bool = True,
    byte_size: int = 32,
    execution_metadata: dict[str, Any] | None = None,
) -> str:
    task_tenant_id = db.execute(
        select(Task.tenant_id).where(Task.id == int(task_id))
    ).scalar_one()
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=int(task_tenant_id),
        task_id=task_id,
        tool_name=tool_name,
        tool_arguments={"command": "echo test"},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata=execution_metadata or {},
    )
    db.add(execution)
    db.flush()

    artifact = ExecutionArtifact(
        id=uuid_lib.uuid4(),
        execution_id=execution.id,
        tenant_id=int(task_tenant_id),
        task_id=task_id,
        artifact_kind=artifact_kind,
        content_text=content_text,
        content_sha256="a" * 64,
        byte_size=byte_size,
        mime_type="text/plain" if is_text else "application/octet-stream",
        is_text=is_text,
    )
    db.add(artifact)
    db.flush()
    return str(execution.id)


def _tshark_secret_exposure_metadata() -> dict[str, Any]:
    return {
        "schema_version": "tshark.v1",
        "analysis_mode": "secret_exposure",
        "pcap": {
            "input_file": "captures/secret-exposure-example.pcap",
            "artifact_sha256": "pcap-secret-exposure-sha256",
            "packet_count": 3,
            "duration_seconds": 1.5,
        },
        "hosts": ["192.0.2.10"],
        "conversations": [
            {
                "protocol": "tcp",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "dst_port": 80,
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "packet_count": 2,
            }
        ],
        "secret_exposure": [
            {
                "protocol": "http",
                "field": "http.authorization",
                "kind": "authorization_header",
                "frame": "7",
                "stream": "2",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "extraction_filter": "http.authorization",
                "proof_excerpt": "Authorization: Bearer raw-token",
                "fingerprint": "hmac-sha256:bearer_token-abc123",
                "pcap_artifact_sha256": "pcap-secret-exposure-sha256",
            }
        ],
    }


def _seed_succeeded_ingestion_run(
    db,
    *,
    engagement_id: int,
    tenant_id: int,
    user_id: int,
    task_id: int,
    source_execution_id: str,
) -> None:
    db.add(
        KnowledgeIngestionRun(
            id=uuid_lib.uuid4(),
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            engagement_id=int(engagement_id),
            task_id=int(task_id),
            source_execution_id=source_execution_id,
            extractor_family="knowledge.delete_guard",
            extractor_version="1.0",
            status=IngestionRunStatus.SUCCEEDED.value,
        )
    )
    db.flush()


def test_create_or_get_ingestion_run_is_idempotent() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        execution_id = str(uuid_lib.uuid4())
        run_dto = IngestionRunCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion.det",
            extractor_version="1.0.0",
        )
        first = service.create_or_get_ingestion_run(run_dto)
        second = service.create_or_get_ingestion_run(run_dto)

        assert first.id == second.id
        assert first.engagement_id == engagement.id
        assert first.task_id == task.id
        assert first.source_execution_id == execution_id
    finally:
        db.close()
        engine.dispose()


def test_ingestion_run_and_observation_set_tenant_id_from_engagement() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db, tenant_id=66)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion.det",
                extractor_version="1.0.0",
            )
        )
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="network.open_port",
            subject_type="host.ip",
            subject_key="host.ip:10.0.0.9",
            assertion_level="observed",
            payload={},
            observed_at=datetime.now(timezone.utc),
        )
        service.insert_observations(ingestion_run_id=str(run.id), observations=[observation])

        persisted_run = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == run.id).one()
        persisted_obs = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == run.id)
            .one()
        )
        assert persisted_run.tenant_id == 66
        assert persisted_obs.tenant_id == 66
    finally:
        db.close()
        engine.dispose()


def test_insert_observations_dedupes_within_run() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion.det",
                extractor_version="1.0.0",
            )
        )
        payload = {"port": 80, "proto": "tcp"}
        obs = ObservationCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="network.open_port",
            subject_type="host.ip",
            subject_key="host.ip:10.0.0.1",
            assertion_level="observed",
            payload=payload,
            observed_at=datetime.now(timezone.utc),
        )
        inserted, duplicates = service.insert_observations(
            ingestion_run_id=str(run.id),
            observations=[obs, obs],
        )

        rows = db.query(KnowledgeObservation).filter(KnowledgeObservation.ingestion_run_id == run.id).all()
        assert inserted == 1
        assert duplicates == 1
        assert len(rows) == 1
    finally:
        db.close()
        engine.dispose()


def test_insert_observations_persists_observation_metadata() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0.0",
            )
        )
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="finding.vulnerability_detected",
            subject_type="finding.instance",
            subject_key="finding.instance:cve-2021-44228:http://10.0.0.7/",
            assertion_level="candidate",
            payload={
                "title": "Possible vulnerability exposure",
                "evidence_refs": [
                    {
                        "evidence_archive_id": "archive-1",
                        "excerpt": "candidate evidence excerpt",
                    }
                ],
            },
            observation_metadata={
                "source_kind": "llm_candidate",
                "extractor_family": "llm.candidate_extraction",
                "extractor_version": "1.0.0",
                "extraction_mode": "candidate_fallback",
                "durable_masking_applied": True,
                "audit_summary": {"llm_model": "gpt-5-mini"},
            },
            observed_at=datetime.now(timezone.utc),
        )
        inserted, duplicates = service.insert_observations(
            ingestion_run_id=str(run.id),
            observations=[observation],
        )

        persisted = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == run.id)
            .one()
        )
        assert inserted == 1
        assert duplicates == 0
        assert persisted.assertion_level == "candidate"
        assert persisted.observation_metadata == {
            "source_kind": "llm_candidate",
            "extractor_family": "llm.candidate_extraction",
            "extractor_version": "1.0.0",
            "extraction_mode": "candidate_fallback",
            "durable_masking_applied": True,
            "audit_summary": {"llm_model": "gpt-5-mini"},
        }
    finally:
        db.close()
        engine.dispose()


def test_insert_observations_rejects_candidate_without_evidence_refs() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0.0",
            )
        )
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="finding.vulnerability_detected",
            subject_type="finding.instance",
            subject_key="finding.instance:candidate:http://10.0.0.8/",
            assertion_level="candidate",
            payload={"title": "Missing evidence refs", "evidence_refs": []},
            observation_metadata={
                "source_kind": "llm_candidate",
                "extractor_family": "llm.candidate_extraction",
                "extractor_version": "1.0.0",
                "extraction_mode": "candidate_fallback",
            },
            observed_at=datetime.now(timezone.utc),
        )

        try:
            service.insert_observations(
                ingestion_run_id=str(run.id),
                observations=[observation],
            )
            assert False, "Expected ValueError when candidate evidence_refs is empty"
        except ValueError as exc:
            assert "payload.evidence_refs" in str(exc)
    finally:
        db.close()
        engine.dispose()


def test_zero_observation_run_is_tracked_cleanly() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion.det",
                extractor_version="1.0.0",
            )
        )
        inserted, duplicates = service.insert_observations(
            ingestion_run_id=str(run.id),
            observations=[],
        )
        completed = service.set_ingestion_run_status(
            ingestion_run_id=str(run.id),
            status=IngestionRunStatus.SUCCEEDED,
        )

        assert inserted == 0
        assert duplicates == 0
        assert completed.status == "succeeded"
    finally:
        db.close()
        engine.dispose()


def test_task_delete_does_not_delete_knowledge_rows() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion.det",
                extractor_version="1.0.0",
            )
        )
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="network.open_port",
            subject_type="host.ip",
            subject_key="host.ip:10.0.0.3",
            assertion_level="observed",
            payload={"port": 22},
            observed_at=datetime.now(timezone.utc),
        )
        service.insert_observations(ingestion_run_id=str(run.id), observations=[observation])
        db.flush()

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        remaining_runs = db.query(KnowledgeIngestionRun).filter(KnowledgeIngestionRun.id == run.id).count()
        remaining_obs = db.query(KnowledgeObservation).filter(KnowledgeObservation.ingestion_run_id == run.id).count()
        assert remaining_runs == 1
        assert remaining_obs == 1
    finally:
        db.close()
        engine.dispose()


def test_insert_observations_rejects_lineage_mismatch() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        run = service.create_or_get_ingestion_run(
            IngestionRunCreate(
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=str(uuid_lib.uuid4()),
                extractor_family="runtime.ingestion.det",
                extractor_version="1.0.0",
            )
        )
        mismatched_observation = ObservationCreate(
            engagement_id=engagement.id + 1,
            task_id=task.id,
            source_execution_id=str(run.source_execution_id),
            ingestion_run_id=str(run.id),
            observation_type="network.open_port",
            subject_type="host.ip",
            subject_key="host.ip:10.0.0.4",
            assertion_level="observed",
            payload={"port": 8080},
            observed_at=datetime.now(timezone.utc),
        )

        try:
            service.insert_observations(
                ingestion_run_id=str(run.id),
                observations=[mismatched_observation],
            )
            assert False, "Expected ValueError for observation lineage mismatch"
        except ValueError as exc:
            assert "engagement_id does not match" in str(exc)
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_orchestrates_run_archive_and_zero_observation_success() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="command output",
            is_text=True,
            byte_size=128,
        )
        service = KnowledgeIngestionService(db)

        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            compact_output_hint={"summary": "compact summary"},
        )

        assert result["ok"] is True
        assert result["status"] == "succeeded"
        assert result["archive_count"] == 1
        assert result["observation_inserted_count"] == 0
        assert result["web_path_upsert_count"] == 0
        assert result["web_path_insert_count"] == 0

        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        assert run.status == "succeeded"
        assert run.run_metadata["artifact_count"] == 1
        assert run.run_metadata["archive_count"] == 1
        assert run.run_metadata["observation_inserted_count"] == 0
        assert int(run.run_metadata.get("web_path_upsert_count") or 0) == 0
        assert int(run.run_metadata.get("web_path_insert_count") or 0) == 0
        semantic_metrics = dict(run.run_metadata.get("semantic_metrics") or {})
        by_model = dict(semantic_metrics.get("projection_upsert_count_by_model") or {})
        assert int(by_model.get("web_path") or 0) == 0
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_archives_and_projects_tshark_masked_secret_exposure() -> None:
    engine, db = _build_session()
    raw_secret = "Bearer raw-token"
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        tshark_metadata = _tshark_secret_exposure_metadata()
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="sniffing_spoofing.network_sniffers.tshark",
            artifact_kind="json",
            content_text=json.dumps(tshark_metadata),
            is_text=True,
            byte_size=512,
            execution_metadata={"tool_metadata": tshark_metadata},
        )
        service = KnowledgeIngestionService(db)

        first = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )
        second = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert first["ok"] is True
        assert first["archive_count"] == 1
        assert first["observation_inserted_count"] == 3
        assert first["finding_upsert_count"] == 1
        assert second["ingestion_run_id"] == first["ingestion_run_id"]
        assert second["archive_count"] == 1
        assert second["observation_inserted_count"] == 0
        assert second["observation_duplicate_count"] == 3

        archive = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        observations = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == first["ingestion_run_id"])
            .all()
        )
        assert {item.observation_type for item in observations} == {
            "network.host_discovered",
            "network.service_observed",
            "finding.vulnerability_detected",
        }

        persisted_finding_observation = next(
            item
            for item in observations
            if item.observation_type == "finding.vulnerability_detected"
        )
        evidence_refs = list((persisted_finding_observation.payload or {}).get("evidence_refs") or [])
        assert evidence_refs == [{"evidence_archive_id": str(archive.id)}]
        assert persisted_finding_observation.payload["proof_excerpt"] == (
            "Authorization: Bearer <DURABLE_SECRET_MASK:token>"
        )

        projected_finding = (
            db.query(KnowledgeFinding)
            .filter(KnowledgeFinding.engagement_id == engagement.id)
            .one()
        )
        assert projected_finding.finding_key == persisted_finding_observation.subject_key
        assert projected_finding.evidence_summary == {
            "evidence_refs": [{"evidence_archive_id": str(archive.id)}]
        }

        durable_text = json.dumps(
            {
                "observations": [
                    {
                        "subject_key": item.subject_key,
                        "payload": item.payload,
                        "metadata": item.observation_metadata,
                    }
                    for item in observations
                ],
                "finding_key": projected_finding.finding_key,
                "finding_metadata": projected_finding.finding_metadata,
                "evidence_summary": projected_finding.evidence_summary,
            },
            default=str,
            sort_keys=True,
        )
        assert "raw-token" not in durable_text
        assert raw_secret not in durable_text
        assert "<DURABLE_SECRET_MASK:token>" in durable_text
        assert "bearer_token-abc123" in projected_finding.finding_key
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_marks_run_failed_when_archive_step_raises(monkeypatch) -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="command output",
            is_text=True,
            byte_size=128,
        )
        service = KnowledgeIngestionService(db)

        def _raise_archive(**kwargs):
            raise RuntimeError("archive unavailable")

        monkeypatch.setattr(service.archive_service, "archive_execution_artifacts", _raise_archive)

        result = service.ingest_execution(
            task_id=task.id,
            engagement_id=engagement.id,
            source_execution_id=execution_id,
        )

        assert result["ok"] is False
        assert result["status"] == "failed"
        assert "archive unavailable" in result["error"]

        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        assert run.status == "failed"
        assert "archive unavailable" in str(run.error_message or "")
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_persists_observations_when_extractor_emits() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="http service on 10.0.0.5:80",
            is_text=True,
            byte_size=256,
        )

        def _extractor(
            execution_payload,
            ingestion_run_id,
            engagement_id,
            task_id,
            compact_output_hint,
        ):
            return [
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=str(execution_payload["execution"]["execution_id"]),
                    ingestion_run_id=ingestion_run_id,
                    observation_type="network.open_port",
                    subject_type="host.ip",
                    subject_key="host.ip:10.0.0.5",
                    assertion_level="observed",
                    payload={"port": 80, "protocol": "tcp"},
                    observed_at=datetime.now(timezone.utc),
                )
            ]

        service = KnowledgeIngestionService(db, extractors=[_extractor])
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["observation_inserted_count"] == 1
        persisted = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == result["ingestion_run_id"])
            .all()
        )
        assert len(persisted) == 1
        assert persisted[0].observation_type == "network.open_port"
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_run_metadata_includes_finding_level_extraction_counters() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="finding-level extraction counters",
            is_text=True,
            byte_size=256,
        )

        def _extractor(
            execution_payload,
            ingestion_run_id,
            engagement_id,
            task_id,
            compact_output_hint,
        ):
            source_execution_id = str(execution_payload["execution"]["execution_id"])
            observed_at = datetime.now(timezone.utc)
            return [
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=source_execution_id,
                    ingestion_run_id=ingestion_run_id,
                    observation_type="finding.vulnerability_detected",
                    subject_type="finding.instance",
                    subject_key="finding.instance:cve-2023-0001:host-1",
                    assertion_level="observed",
                    payload={"title": "Authoritative finding"},
                    observed_at=observed_at,
                ),
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=source_execution_id,
                    ingestion_run_id=ingestion_run_id,
                    observation_type="finding.vulnerability_detected",
                    subject_type="finding.instance",
                    subject_key="finding.instance:candidate:host-2",
                    assertion_level="candidate",
                    payload={
                        "title": "Candidate finding",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": "archive-candidate-1",
                                "excerpt": "candidate evidence excerpt",
                            }
                        ],
                    },
                    observed_at=observed_at,
                ),
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=source_execution_id,
                    ingestion_run_id=ingestion_run_id,
                    observation_type="network.service_detected",
                    subject_type="service.socket",
                    subject_key="service.socket:10.0.0.10/tcp/443",
                    assertion_level="observed",
                    payload={"service_name": "https"},
                    observed_at=observed_at,
                ),
            ]

        service = KnowledgeIngestionService(db, extractors=[_extractor])
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        adapter_stats = dict((run.run_metadata or {}).get("adapter_stats") or {})
        assert int(adapter_stats.get("observation_count_total") or 0) == 3
        assert int(adapter_stats.get("observation_count_finding_total") or 0) == 2
        assert int(adapter_stats.get("observation_count_finding_authoritative") or 0) == 1
        assert int(adapter_stats.get("observation_count_non_finding_total") or 0) == 1
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_deterministic_only_flow_still_succeeds_with_no_candidate_payload(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="deterministic only path",
            is_text=True,
            byte_size=128,
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=None,
            raise_on_error=True,
        )
        assert result["ok"] is True
        assert result["candidate_extraction_status"] == "no_signal"
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_projects_from_persisted_deduped_observations() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="duplicate host observation input",
            is_text=True,
            byte_size=128,
        )

        def _extractor(
            execution_payload,
            ingestion_run_id,
            engagement_id,
            task_id,
            compact_output_hint,
        ):
            observation = ObservationCreate(
                engagement_id=engagement_id,
                task_id=task_id,
                source_execution_id=str(execution_payload["execution"]["execution_id"]),
                ingestion_run_id=ingestion_run_id,
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.0.0.55",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=datetime.now(timezone.utc),
            )
            return [observation, observation]

        service = KnowledgeIngestionService(db, extractors=[_extractor])
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["observation_inserted_count"] == 1
        assert result["observation_duplicate_count"] == 1

        asset = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == task.engagement_id,
            KnowledgeAsset.asset_key == "host.ip:10.0.0.55",
        ).one()
        assert int((asset.asset_metadata or {}).get("observation_count") or 0) == 1
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_is_archive_idempotent_for_same_run_identity() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="idempotent archive",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeIngestionService(db)

        first = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )
        second = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            raise_on_error=True,
        )

        assert first["ingestion_run_id"] == second["ingestion_run_id"]
        archive_count = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .count()
        )
        assert archive_count == 1
    finally:
        db.close()
        engine.dispose()


def test_delete_guard_upgrades_inline_excerpt_rows_to_materialized_archived_file() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="delete-safe durable text",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeIngestionService(db)
        service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=False,
            raise_on_error=True,
        )

        before = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert before.storage_mode == "inline_excerpt"

        result = service.ensure_task_delete_safe(
            task_id=task.id,
            engagement_id=engagement.id,
        )
        assert result["safe"] is True
        assert result["catchup_attempted"] is False

        after = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert after.storage_mode == "inline_excerpt"
        assert after.inline_excerpt == "delete-safe durable text"
    finally:
        db.close()
        engine.dispose()


def test_delete_guard_does_not_mark_metadata_only_rows_safe_without_materialization() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=4096,
        )
        service = KnowledgeIngestionService(db)
        service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            delete_survival_required=False,
            raise_on_error=True,
        )

        before = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert before.storage_mode == "metadata_only"

        result = service.ensure_task_delete_safe(
            task_id=task.id,
            engagement_id=engagement.id,
        )
        assert result["safe"] is False
        assert result["catchup_attempted"] is True
        assert execution_id in result["unsafe_execution_ids"]

        after = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_execution_id == execution_id)
            .one()
        )
        assert after.storage_mode == "archived_file"
        assert str(after.archived_file_ref or "").startswith("pending://")
    finally:
        db.close()
        engine.dispose()


def test_delete_guard_accepts_object_ref_evidence_when_object_is_ready(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=4,
        )
        artifact = (
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.task_id == task.id, ExecutionArtifact.execution_id == execution_id)
            .one()
        )
        payload = b"\x01\x02\x03\x04"
        archive_object_key = "tenants/1/engagements/1/evidence/object-ready.bin"
        artifact.upload_status = "ready"
        artifact.object_key = "tenants/1/tasks/1/executions/1/artifacts/file.bin"
        artifact.content_sha256 = sha256(payload).hexdigest()
        artifact.byte_size = len(payload)
        _seed_succeeded_ingestion_run(
            db,
            engagement_id=engagement.id,
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            task_id=task.id,
            source_execution_id=execution_id,
        )
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution_id,
                source_artifact_id=artifact.id,
                storage_mode="object_ref",
                object_key=archive_object_key,
                content_sha256=sha256(payload).hexdigest(),
                byte_size=len(payload),
                mime_type="application/octet-stream",
                lineage_snapshot={"artifact_id": str(artifact.id)},
            )
        )
        db.commit()

        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        object_store.put_bytes(archive_object_key, payload, content_type="application/octet-stream")
        service = KnowledgeDeleteGuardService(
            db,
            ingest_execution=lambda **_kwargs: {"ok": False},
            object_store=object_store,
        )
        result = service.ensure_task_delete_safe(
            task_id=task.id,
            engagement_id=engagement.id,
        )

        assert result["safe"] is True
        assert result["catchup_attempted"] is False
    finally:
        db.close()
        engine.dispose()


def test_delete_guard_blocks_object_ref_when_runner_upload_pending(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=4,
        )
        artifact = (
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.task_id == task.id, ExecutionArtifact.execution_id == execution_id)
            .one()
        )
        payload = b"\x01\x02\x03\x04"
        archive_object_key = "tenants/1/engagements/1/evidence/upload-pending.bin"
        artifact.upload_status = "upload_pending"
        artifact.object_key = "tenants/1/tasks/1/executions/1/artifacts/file.bin"
        artifact.content_sha256 = sha256(payload).hexdigest()
        artifact.byte_size = len(payload)
        _seed_succeeded_ingestion_run(
            db,
            engagement_id=engagement.id,
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            task_id=task.id,
            source_execution_id=execution_id,
        )
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution_id,
                source_artifact_id=artifact.id,
                storage_mode="object_ref",
                object_key=archive_object_key,
                content_sha256=sha256(payload).hexdigest(),
                byte_size=len(payload),
                mime_type="application/octet-stream",
                lineage_snapshot={"artifact_id": str(artifact.id)},
            )
        )
        db.commit()

        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        object_store.put_bytes(archive_object_key, payload, content_type="application/octet-stream")
        service = KnowledgeDeleteGuardService(
            db,
            ingest_execution=lambda **_kwargs: {"ok": False},
            object_store=object_store,
        )
        result = service.ensure_task_delete_safe(
            task_id=task.id,
            engagement_id=engagement.id,
        )

        assert result["safe"] is False
        assert result["catchup_attempted"] is True
        assert execution_id in result["unsafe_execution_ids"]
    finally:
        db.close()
        engine.dispose()


def test_delete_guard_blocks_object_ref_when_hash_mismatch(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=4,
        )
        artifact = (
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.task_id == task.id, ExecutionArtifact.execution_id == execution_id)
            .one()
        )
        payload = b"\x01\x02\x03\x04"
        archive_object_key = "tenants/1/engagements/1/evidence/hash-mismatch.bin"
        artifact.upload_status = "ready"
        artifact.object_key = "tenants/1/tasks/1/executions/1/artifacts/file.bin"
        artifact.content_sha256 = sha256(payload).hexdigest()
        artifact.byte_size = len(payload)
        _seed_succeeded_ingestion_run(
            db,
            engagement_id=engagement.id,
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            task_id=task.id,
            source_execution_id=execution_id,
        )
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=engagement.tenant_id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution_id,
                source_artifact_id=artifact.id,
                storage_mode="object_ref",
                object_key=archive_object_key,
                content_sha256="0" * 64,
                byte_size=len(payload),
                mime_type="application/octet-stream",
                lineage_snapshot={"artifact_id": str(artifact.id)},
            )
        )
        db.commit()

        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        object_store.put_bytes(archive_object_key, payload, content_type="application/octet-stream")
        service = KnowledgeDeleteGuardService(
            db,
            ingest_execution=lambda **_kwargs: {"ok": False},
            object_store=object_store,
        )
        result = service.ensure_task_delete_safe(
            task_id=task.id,
            engagement_id=engagement.id,
        )

        assert result["safe"] is False
        assert result["catchup_attempted"] is True
        assert execution_id in result["unsafe_execution_ids"]
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_writes_semantic_failure_metadata_for_adapter_errors() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="adapter failure case",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeIngestionService(db, adapter_registry=_FailingAdapterRegistry())
        result = service.ingest_execution(
            task_id=task.id,
            engagement_id=engagement.id,
            source_execution_id=execution_id,
            raise_on_error=False,
        )

        assert result["ok"] is False
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        assert metadata.get("semantic_status") == "failed"
        assert metadata.get("semantic_failure_stage") == "adapter_extraction"
        assert "adapter extraction failed" in str(metadata.get("semantic_failure_reason") or "")
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_redacts_sensitive_values_in_failure_metadata_and_error_response() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="adapter failure secret redaction case",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeIngestionService(db, adapter_registry=_LeakyFailingAdapterRegistry())
        result = service.ingest_execution(
            task_id=task.id,
            engagement_id=engagement.id,
            source_execution_id=execution_id,
            raise_on_error=False,
        )

        assert result["ok"] is False
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        failure_reason = str(metadata.get("semantic_failure_reason") or "")
        error_value = str(result.get("error") or "")
        for candidate in [failure_reason, error_value, str(run.error_message or "")]:
            assert "sk-live-123456789" not in candidate
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x.y" not in candidate
            assert "<REDACTED>" in candidate or "<REDACTED_JWT>" in candidate
        assert metadata.get("semantic_failure_redacted") is True
        assert metadata.get("semantic_failure_error_class") == "RuntimeError"
        assert isinstance(metadata.get("semantic_failure_fingerprint"), str)
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_redacts_sensitive_values_in_projection_failure_metadata() -> None:
    class _LeakyProjectionFailureService:
        def project_observations(self, *, engagement_id: int, user_id: int, observations):
            raise RuntimeError("projection exploded api_key=topsecret-999999 bearer eyJfoo.bar.baz")

    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="projection secret redaction case",
            is_text=True,
            byte_size=64,
        )

        def _extractor(
            execution_payload,
            ingestion_run_id,
            engagement_id,
            task_id,
            compact_output_hint,
        ):
            return [
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=str(execution_payload["execution"]["execution_id"]),
                    ingestion_run_id=ingestion_run_id,
                    observation_type="network.host_discovered",
                    subject_type="host.ip",
                    subject_key="host.ip:10.0.0.99",
                    assertion_level="observed",
                    payload={"host_status": "up"},
                    observed_at=datetime.now(timezone.utc),
                )
            ]

        service = KnowledgeIngestionService(
            db,
            projection_service=_LeakyProjectionFailureService(),
            extractors=[_extractor],
        )
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=False,
        )

        assert result["ok"] is False
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        projection_error = str(metadata.get("projection_error") or "")
        assert "topsecret-999999" not in projection_error
        assert "eyJfoo.bar.baz" not in projection_error
        assert "<REDACTED>" in projection_error or "<REDACTED_JWT>" in projection_error
        assert metadata.get("projection_error_class") == "RuntimeError"
        assert metadata.get("projection_error_redacted") is True
        assert isinstance(metadata.get("projection_error_fingerprint"), str)
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_zero_observation_run_is_success_not_failure() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="no semantic facts",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        semantic_metrics = dict(metadata.get("semantic_metrics") or {})
        assert metadata.get("semantic_status") == "succeeded"
        assert int(semantic_metrics.get("zero_observation_run_count") or 0) == 1
        assert int(semantic_metrics.get("adapter_dispatch_count_total") or 0) == 0
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_records_projection_contradiction_metrics() -> None:
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="contradiction metric case",
            is_text=True,
            byte_size=256,
        )

        def _extractor(
            execution_payload,
            ingestion_run_id,
            engagement_id,
            task_id,
            compact_output_hint,
        ):
            observed_at = datetime.now(timezone.utc)
            return [
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=str(execution_payload["execution"]["execution_id"]),
                    ingestion_run_id=ingestion_run_id,
                    observation_type="network.service_detected",
                    subject_type="service.socket",
                    subject_key="service.socket:10.20.30.40/tcp/80",
                    assertion_level="observed",
                    payload={"service_name": "http"},
                    observed_at=observed_at,
                ),
                ObservationCreate(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    source_execution_id=str(execution_payload["execution"]["execution_id"]),
                    ingestion_run_id=ingestion_run_id,
                    observation_type="network.service_detected",
                    subject_type="service.socket",
                    subject_key="service.socket:10.20.30.40/tcp/80",
                    assertion_level="observed",
                    payload={"service_name": "nginx"},
                    observed_at=observed_at + timezone.utc.utcoffset(observed_at),
                ),
            ]

        service = KnowledgeIngestionService(db, extractors=[_extractor])
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        semantic_metrics = dict(metadata.get("semantic_metrics") or {})
        assert int(semantic_metrics.get("projection_contradiction_count") or 0) >= 1
        by_domain = dict(semantic_metrics.get("projection_contradiction_count_by_domain") or {})
        assert int(by_domain.get("service") or 0) >= 1
    finally:
        db.close()
        engine.dispose()


def _build_post_tool_candidate_payload(
    *,
    source_artifact_id: str,
    vulnerability_confidence: float,
) -> dict[str, Any]:
    return {
        "candidate_observations": [
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.instance",
                "subject_key_hint": "cve-2026-1000:service.socket:10.0.0.50/tcp/5432",
                "assertion_level": "candidate",
                "confidence": 0.92,
                "attributes": [{"key": "version", "value": "11.5"}],
                "rationale": "Version banner indicates likely vulnerable release.",
                "evidence_refs": [
                    {
                        "source_artifact_id": source_artifact_id,
                        "excerpt": "PostgreSQL 11.5",
                    }
                ],
                "vulnerability": {
                    "id": "CVE-2026-1000",
                    "title": "PostgreSQL vulnerable version candidate",
                    "severity": "high",
                },
                "vulnerability_confidence": float(vulnerability_confidence),
            }
        ],
        "analyst_notes": [],
        "no_signal": False,
    }


def test_candidate_extraction_is_disabled_by_feature_flag(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "false")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="feature-flag disabled path",
            is_text=True,
            byte_size=64,
        )
        artifact_id = str(
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.execution_id == execution_id)
            .one()
            .id
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=_build_post_tool_candidate_payload(
                source_artifact_id=artifact_id,
                vulnerability_confidence=0.95,
            ),
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["candidate_extraction_status"] == "skipped"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        assert metadata.get("candidate_extraction_status") == "skipped"
        assert metadata.get("candidate_extraction_reason") == "candidate_feature_disabled"
    finally:
        db.close()
        engine.dispose()


def test_candidate_extraction_missing_post_tool_payload_returns_no_signal(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="missing payload case",
            is_text=True,
            byte_size=96,
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=None,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["candidate_extraction_status"] == "no_signal"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        assert metadata.get("candidate_extraction_reason") == "post_tool_candidate_payload_missing"
    finally:
        db.close()
        engine.dispose()


def test_candidate_extraction_maps_source_artifact_refs_and_persists_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "0.90")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="postgresql 11.5 banner",
            is_text=True,
            byte_size=128,
        )
        artifact_id = str(
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.execution_id == execution_id)
            .one()
            .id
        )

        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=_build_post_tool_candidate_payload(
                source_artifact_id=artifact_id,
                vulnerability_confidence=0.94,
            ),
            post_tool_candidate_usage={
                "input_tokens": 42,
                "output_tokens": 18,
                "total_tokens": 60,
                "estimated_cost_usd": 0.0,
            },
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["candidate_extraction_status"] == "ran"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        assert metadata.get("candidate_extraction_reason") == "candidates_extracted"
        assert int(metadata.get("candidate_observation_count") or 0) == 1
        assert metadata.get("candidate_usage_summary") == {
            "input_tokens": 42,
            "output_tokens": 18,
            "total_tokens": 60,
            "estimated_cost_usd": 0.0,
        }
        persisted = (
            db.query(KnowledgeObservation)
            .filter(KnowledgeObservation.ingestion_run_id == run.id)
            .one()
        )
        assert persisted.assertion_level == "candidate"
        evidence_refs = list((persisted.payload or {}).get("evidence_refs") or [])
        assert len(evidence_refs) == 1
        assert set(evidence_refs[0].keys()) == {"evidence_archive_id", "excerpt"}
        assert "source_artifact_id" not in evidence_refs[0]
        assert evidence_refs[0]["excerpt"] == "PostgreSQL 11.5"
        archive_id = str(evidence_refs[0].get("evidence_archive_id") or "")
        assert archive_id
        archive_row = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.id == archive_id)
            .one()
        )
        assert str(archive_row.source_artifact_id) == artifact_id
    finally:
        db.close()
        engine.dispose()


def test_candidate_extraction_below_threshold_records_drop_reason(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    monkeypatch.setenv("KNOWLEDGE_VULNERABILITY_MIN_CONFIDENCE", "0.90")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="postgresql 11.5 banner",
            is_text=True,
            byte_size=128,
        )
        artifact_id = str(
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.execution_id == execution_id)
            .one()
            .id
        )

        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            post_tool_candidate_payload=_build_post_tool_candidate_payload(
                source_artifact_id=artifact_id,
                vulnerability_confidence=0.82,
            ),
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["candidate_extraction_status"] == "no_signal"
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        drop_reasons = dict(metadata.get("candidate_vulnerability_drop_reasons") or {})
        assert int(drop_reasons.get("below_vulnerability_confidence_threshold") or 0) >= 1
        assert int(metadata.get("candidate_vulnerability_accepted_count") or 0) == 0
    finally:
        db.close()
        engine.dispose()


def test_candidate_extraction_is_idempotent_for_same_run_identity(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    engine, db = _build_session()
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="idempotent candidate case",
            is_text=True,
            byte_size=64,
        )
        artifact_id = str(
            db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.execution_id == execution_id)
            .one()
            .id
        )
        service = KnowledgeIngestionService(db)

        payload = _build_post_tool_candidate_payload(
            source_artifact_id=artifact_id,
            vulnerability_confidence=0.95,
        )
        first = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            post_tool_candidate_payload=payload,
            raise_on_error=True,
        )
        second = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="runtime.ingestion",
            extractor_version="1.0",
            post_tool_candidate_payload=payload,
            raise_on_error=True,
        )

        assert first["ingestion_run_id"] == second["ingestion_run_id"]
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == first["ingestion_run_id"])
            .one()
        )
        metadata = dict(run.run_metadata or {})
        assert metadata.get("candidate_extraction_status") == "ran"
        assert metadata.get("candidate_extraction_reason") == "candidates_extracted"
    finally:
        db.close()
        engine.dispose()
