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
from backend.services.knowledge.candidate_extraction import (
    CandidateExtractionPolicyDecision,
    CandidateExtractionPolicyRequest,
)
from backend.services.knowledge.contracts import (
    IngestionRunCreate as _IngestionRunCreate,
    IngestionRunStatus,
    ObservationCreate as _ObservationCreate,
)
from backend.services.knowledge import ingestion_service as ingestion_module
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from agent.tools.sniffing_spoofing.network_sniffers.tshark_semantics import (
    build_tshark_semantic_observations,
)

IngestionRunCreate = partial(_IngestionRunCreate, user_id=1)
ObservationCreate = partial(_ObservationCreate, user_id=1)
REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_SCAN_ROOTS = (
    REPO_ROOT / "backend",
    REPO_ROOT / "agent",
    REPO_ROOT / "client",
    REPO_ROOT / "core",
)

STATISTICS_DISPOSITION_INVENTORY = {
    "preserve_run_result": (
        "observation_inserted_count",
        "observation_duplicate_count",
        "projection_status",
        "asset_upsert_count",
        "service_upsert_count",
        "finding_upsert_count",
        "relationship_upsert_count",
        "web_path_upsert_count",
    ),
    "preserve_run_metadata": (
        "source_tool_name",
        "artifact_count",
        "archive_count",
        "semantic_status",
        "semantic_metrics",
        "projection_upsert_count_by_model",
        "projection_contradiction_count",
        "projection_contradiction_count_by_domain",
    ),
    "preserve_candidate_policy": (
        "deterministic_observation_count",
        "observation_count_finding_total",
        "observation_count_finding_authoritative",
        "observation_count_non_finding_total",
    ),
    "retire_dispatch_only": (
        "resolved_adapter_count",
        "resolved_adapters",
        "adapter_dispatch_count_by_tool",
        "adapter_dispatch_count_by_family",
        "adapter_observation_count",
        "legacy_extractor_count",
        "legacy_observation_count",
    ),
    "safe_failure_metadata": (
        "semantic_failure_stage",
        "semantic_failure_reason",
        "semantic_failure_error_class",
        "semantic_failure_fingerprint",
        "semantic_failure_redacted",
        "projection_error",
        "projection_error_class",
        "projection_error_fingerprint",
        "projection_error_redacted",
    ),
}

EXPECTED_NON_TEST_STATISTICS_CONSUMERS = {
    "fact_stats": (
        "backend/services/knowledge/candidate_extraction/service.py",
        "backend/services/knowledge/ingestion_service.py",
    ),
    "adapter_stats": (),
    "semantic_metrics": ("backend/services/knowledge/ingestion_service.py",),
    "deterministic_observation_count": (
        "backend/services/knowledge/candidate_extraction/contracts.py",
        "backend/services/knowledge/candidate_extraction/policy.py",
        "backend/services/knowledge/candidate_extraction/service.py",
    ),
    "semantic_failure_reason": ("backend/services/knowledge/ingestion_service.py",),
}

EXPECTED_EXTRACTOR_AND_REGISTRY_CONSUMERS = {
    "Execution" + "Extractor": (),
    "register" + "_extractor(": (),
    "extractors: Iterable[" + "Execution" + "Extractor] | None = None": (),
    "adapter_registry:": (),
    "Knowledge" + "Adapter" + "Registry" + "Service(": (),
    "from .adapter_registry import " + "Knowledge" + "Adapter" + "Registry" + "Service": (),
    '"' + "Knowledge" + "Adapter" + "Registry" + "Service" + '",': (),
}

EXPECTED_DIRECT_ADAPTER_IMPORT_CONSUMERS = ()


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for root in PRODUCTION_SCAN_ROOTS:
        files.extend(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
            and "tests" not in path.relative_to(REPO_ROOT).parts
        )
    return sorted(files)


def _production_paths_containing(pattern: str) -> tuple[str, ...]:
    matches: list[str] = []
    for path in _production_python_files():
        if pattern in path.read_text(encoding="utf-8"):
            matches.append(path.relative_to(REPO_ROOT).as_posix())
    return tuple(sorted(matches))


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


def _semantic_metadata(
    rows: list[dict[str, Any]],
    *,
    capability_family: str = "network_discovery",
    schema_version: str = "ingestion-test.v1",
) -> dict[str, Any]:
    return {
        "semantic_observations": rows,
        "semantic_evidence": [],
        "semantic_schema_version": schema_version,
        "capability_family": capability_family,
    }


def _host_discovered_row(ip: str) -> dict[str, Any]:
    return {
        "observation_type": "network.host_discovered",
        "subject_type": "host.ip",
        "subject_key": f"host.ip:{ip}",
        "payload": {"ip": ip, "source": "ingestion-test"},
    }


def _open_port_row(ip: str, port: int, *, service_name: str = "http") -> dict[str, Any]:
    return {
        "observation_type": "network.open_port",
        "subject_type": "service.socket",
        "subject_key": f"service.socket:{ip}/tcp/{port}",
        "payload": {
            "ip": ip,
            "protocol": "tcp",
            "port": port,
            "service_name": service_name,
            "source": "ingestion-test",
        },
    }


def _service_detected_row(
    ip: str,
    port: int,
    *,
    service_name: str,
) -> dict[str, Any]:
    return {
        "observation_type": "network.service_detected",
        "subject_type": "service.socket",
        "subject_key": f"service.socket:{ip}/tcp/{port}",
        "payload": {
            "ip": ip,
            "protocol": "tcp",
            "port": port,
            "service_name": service_name,
            "source": "ingestion-test",
        },
    }


def _finding_row(
    *,
    detector_id: str,
    service_key: str,
    severity: str = "medium",
) -> dict[str, Any]:
    return {
        "observation_type": "finding.vulnerability_detected",
        "subject_type": "finding.vulnerability",
        "subject_key": f"finding.vulnerability:{service_key}:{detector_id}",
        "payload": {
            "detector_id": detector_id,
            "subject_key": service_key,
            "severity": severity,
            "source": "ingestion-test",
        },
    }


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


def test_statistics_extractor_and_adapter_consumer_inventory_is_locked() -> None:
    for field_name, expected_paths in EXPECTED_NON_TEST_STATISTICS_CONSUMERS.items():
        assert _production_paths_containing(field_name) == expected_paths

    for pattern, expected_paths in EXPECTED_EXTRACTOR_AND_REGISTRY_CONSUMERS.items():
        assert _production_paths_containing(pattern) == expected_paths

    direct_adapter_import_paths = sorted(
        set(_production_paths_containing("from .adapters import ("))
        | {
            path
            for path in _production_paths_containing("from .amass_adapter import")
            if path == "backend/services/knowledge/adapters/__init__.py"
        }
    )
    assert tuple(direct_adapter_import_paths) == EXPECTED_DIRECT_ADAPTER_IMPORT_CONSUMERS


def test_statistics_field_disposition_preserves_product_metrics_only() -> None:
    fact_stats = {
        "source_tool_name": "shell.exec",
        "zero_observation_run_count": 0,
        "zero_observation_by_tool": {"shell.exec": 0},
        "observation_count_total": 2,
        "observation_count_finding_total": 1,
        "observation_count_finding_authoritative": 1,
        "observation_count_non_finding_total": 1,
    }
    projection_metadata = {
        "asset_upsert_count": 1,
        "service_upsert_count": 1,
        "finding_upsert_count": 1,
        "relationship_upsert_count": 0,
        "web_path_upsert_count": 0,
        "projection_contradiction_count": 1,
        "projection_contradiction_count_by_domain": {"service": 1},
    }

    metrics = KnowledgeIngestionService._build_semantic_metrics(
        fact_stats=fact_stats,
        projection_metadata=projection_metadata,
    )

    assert set(STATISTICS_DISPOSITION_INVENTORY) == {
        "preserve_run_result",
        "preserve_run_metadata",
        "preserve_candidate_policy",
        "retire_dispatch_only",
        "safe_failure_metadata",
    }
    assert metrics == {
        "zero_observation_run_count": 0,
        "zero_observation_by_tool": {"shell.exec": 0},
        "projection_upsert_count_by_model": {
            "asset": 1,
            "service": 1,
            "finding": 1,
            "relationship": 0,
            "web_path": 0,
        },
        "projection_contradiction_count": 1,
        "projection_contradiction_count_by_domain": {"service": 1},
    }
    assert "adapter_dispatch_count_total" not in metrics
    assert "adapter_dispatch_count_by_tool" not in metrics
    assert "adapter_dispatch_count_by_family" not in metrics
    assert "resolved_adapters" not in metrics
    assert "legacy_extractor_count" not in metrics


def test_candidate_extraction_boundary_uses_tool_name_and_deterministic_counts() -> None:
    candidate_service = (
        REPO_ROOT / "backend/services/knowledge/candidate_extraction/service.py"
    ).read_text(encoding="utf-8")
    policy_service = (
        REPO_ROOT / "backend/services/knowledge/candidate_extraction/policy.py"
    ).read_text(encoding="utf-8")

    assert "or fact_stats.get(\"source_tool_name\")" in candidate_service
    assert "extraction_stats" not in candidate_service
    assert "deterministic_observation_count=len(deterministic_observations)" in candidate_service
    assert (
        "\"deterministic_observation_count\": request.deterministic_observation_count"
        in policy_service
    )
    assert "Knowledge" + "Adapter" + "Registry" + "Service" not in candidate_service
    assert "backend.services.knowledge.adapters" not in candidate_service


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
            execution_metadata=_semantic_metadata(
                build_tshark_semantic_observations(tshark_metadata, args=None),
                capability_family="packet_analysis",
                schema_version="tshark.v1",
            ),
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
        assert first["observation_inserted_count"] == 4
        assert first["finding_upsert_count"] == 1
        assert second["ingestion_run_id"] == first["ingestion_run_id"]
        assert second["archive_count"] == 1
        assert second["observation_inserted_count"] == 0
        assert second["observation_duplicate_count"] == 4

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
            "network.service_detected",
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


def test_ingest_execution_persists_observations_from_canonical_facts() -> None:
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
            execution_metadata=_semantic_metadata(
                [_open_port_row("10.0.0.5", 80)],
            ),
        )

        service = KnowledgeIngestionService(db)
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
        assert persisted[0].subject_key == "service.socket:10.0.0.5/tcp/80"
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_payload_uses_backend_source_execution_id_when_payload_omits_or_disagrees() -> None:
    engine, db = _build_session()
    try:
        user, engagement, _task = _seed_user_engagement_task(db)
        service = KnowledgeIngestionService(db)
        cases = (
            ("omitted", {}, "10.0.0.91"),
            ("mismatched", {"execution_id": str(uuid_lib.uuid4())}, "10.0.0.92"),
        )

        for label, payload_lineage, ip_address in cases:
            backend_source_execution_id = str(uuid_lib.uuid4())
            execution_payload = {
                "execution": {
                    **payload_lineage,
                    "tool_name": f"shell.exec.{label}",
                    "execution_metadata": _semantic_metadata([_host_discovered_row(ip_address)]),
                },
                "artifacts": [],
            }

            result = service.ingest_execution_payload(
                user_id=user.id,
                engagement_id=engagement.id,
                tenant_id=engagement.tenant_id,
                task_id=None,
                source_execution_id=backend_source_execution_id,
                execution_payload=execution_payload,
                reuse_existing_archive_rows=True,
                raise_on_error=True,
            )

            assert result["ok"] is True
            run = (
                db.query(KnowledgeIngestionRun)
                .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
                .one()
            )
            persisted = (
                db.query(KnowledgeObservation)
                .filter(KnowledgeObservation.ingestion_run_id == run.id)
                .one()
            )
            assert str(run.source_execution_id) == backend_source_execution_id
            assert str(persisted.source_execution_id) == backend_source_execution_id
            assert str(payload_lineage.get("execution_id") or "") != str(
                persisted.source_execution_id
            )
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_calls_canonical_bridge_once(monkeypatch) -> None:
    engine, db = _build_session()
    bridge_calls = 0
    original_bridge = ingestion_module.build_knowledge_observations
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="canonical bridge call count",
            is_text=True,
            byte_size=128,
            execution_metadata=_semantic_metadata([_host_discovered_row("10.0.0.8")]),
        )

        def _counting_bridge(*, envelope, context):
            nonlocal bridge_calls
            bridge_calls += 1
            return original_bridge(envelope=envelope, context=context)

        monkeypatch.setattr(
            ingestion_module,
            "build_knowledge_observations",
            _counting_bridge,
        )
        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert bridge_calls == 1
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
            execution_metadata=_semantic_metadata(
                [
                    _finding_row(
                        detector_id="cve-2023-0001",
                        service_key="service.socket:10.0.0.10/tcp/443",
                    ),
                    _finding_row(
                        detector_id="cve-2023-0002",
                        service_key="service.socket:10.0.0.10/tcp/443",
                    ),
                    _service_detected_row("10.0.0.10", 443, service_name="https"),
                ],
                capability_family="vulnerability_scanning",
            ),
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
        fact_stats = dict((run.run_metadata or {}).get("fact_stats") or {})
        assert "adapter_stats" not in dict(run.run_metadata or {})
        assert int(fact_stats.get("observation_count_total") or 0) == 3
        assert int(fact_stats.get("observation_count_finding_total") or 0) == 2
        assert int(fact_stats.get("observation_count_finding_authoritative") or 0) == 2
        assert int(fact_stats.get("observation_count_non_finding_total") or 0) == 1
        assert int(fact_stats.get("fact_accepted_count") or 0) == 3
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_fact_diagnostics_are_bounded_and_secret_safe() -> None:
    engine, db = _build_session()
    raw_secret = "sk-live-fact-secret-123"
    try:
        _user, _engagement, task = _seed_user_engagement_task(db)
        execution_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            tool_name="shell.exec",
            artifact_kind="stdout",
            content_text="invalid canonical row",
            is_text=True,
            byte_size=128,
            execution_metadata=_semantic_metadata(
                [
                    {
                        "observation_type": "network.open_port",
                        "subject_type": "service.socket",
                        "subject_key": "service.socket:10.0.0.1/tcp/80",
                        "payload": {
                            "ip": "10.0.0.2",
                            "protocol": "tcp",
                            "port": 80,
                            "token": raw_secret,
                        },
                    }
                ],
            ),
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
        fact_stats = dict((run.run_metadata or {}).get("fact_stats") or {})
        assert fact_stats["fact_input_count"] == 1
        assert fact_stats["fact_accepted_count"] == 0
        assert fact_stats["fact_rejected_count"] == 1
        assert fact_stats["fact_diagnostic_count_by_code"] == {"invalid_fact_row": 1}
        assert raw_secret not in json.dumps(fact_stats, sort_keys=True)
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
            execution_metadata=_semantic_metadata(
                [
                    _host_discovered_row("10.0.0.55"),
                    _host_discovered_row("10.0.0.55"),
                ],
            ),
        )

        service = KnowledgeIngestionService(db)
        result = service.ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            raise_on_error=True,
        )

        assert result["ok"] is True
        assert result["observation_inserted_count"] == 1
        assert result["observation_duplicate_count"] == 0
        run = (
            db.query(KnowledgeIngestionRun)
            .filter(KnowledgeIngestionRun.id == result["ingestion_run_id"])
            .one()
        )
        stats = dict((run.run_metadata or {}).get("fact_stats") or {})
        assert int(stats.get("fact_duplicate_count") or 0) == 1

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


def test_ingest_execution_writes_semantic_failure_metadata_for_canonical_errors(
    monkeypatch,
) -> None:
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

        def _raise_bridge_error(*_args, **_kwargs):
            raise RuntimeError("canonical fact extraction failed")

        monkeypatch.setattr(
            ingestion_module,
            "build_knowledge_observations",
            _raise_bridge_error,
        )
        service = KnowledgeIngestionService(db)
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
        assert metadata.get("semantic_failure_stage") == "canonical_fact_extraction"
        assert "canonical fact extraction failed" in str(
            metadata.get("semantic_failure_reason") or ""
        )
    finally:
        db.close()
        engine.dispose()


def test_ingest_execution_redacts_sensitive_values_in_failure_metadata_and_error_response(
    monkeypatch,
) -> None:
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

        def _raise_leaky_bridge_error(*_args, **_kwargs):
            raise RuntimeError(
                "canonical extraction failed token=sk-live-123456789 "
                "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.x.y"  # gitleaks:allow
            )

        monkeypatch.setattr(
            ingestion_module,
            "build_knowledge_observations",
            _raise_leaky_bridge_error,
        )
        service = KnowledgeIngestionService(db)
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
            execution_metadata=_semantic_metadata([_host_discovered_row("10.0.0.99")]),
        )

        service = KnowledgeIngestionService(
            db,
            projection_service=_LeakyProjectionFailureService(),
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
        assert "adapter_dispatch_count_total" not in semantic_metrics
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
            execution_metadata=_semantic_metadata(
                [
                    _service_detected_row("10.20.30.40", 80, service_name="http"),
                    _service_detected_row("10.20.30.40", 80, service_name="nginx"),
                ],
            ),
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


def test_candidate_extraction_policy_receives_primary_compact_hint(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION", "true")
    captured_requests: list[CandidateExtractionPolicyRequest] = []

    def _capture_policy(
        request: CandidateExtractionPolicyRequest,
    ) -> CandidateExtractionPolicyDecision:
        captured_requests.append(request)
        return CandidateExtractionPolicyDecision(
            action="run",
            reason="eligible_for_candidate_extraction",
            policy_metadata={"compact_hint_present": bool(request.compact_output_hint)},
        )

    monkeypatch.setattr(
        "backend.services.knowledge.candidate_extraction.service."
        "KnowledgeCandidateExtractionPolicy.evaluate",
        _capture_policy,
    )
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
        compact_output_hint = {
            "summary": "primary compact summary",
            "highlights": ["existing primary compact hint"],
        }

        result = KnowledgeIngestionService(db).ingest_execution(
            task_id=task.id,
            source_execution_id=execution_id,
            compact_output_hint=compact_output_hint,
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
        assert captured_requests
        assert captured_requests[0].compact_output_hint == compact_output_hint
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
