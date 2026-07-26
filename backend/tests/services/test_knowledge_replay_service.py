"""Tests for replay boundary over durable ingestion runs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid as uuid_lib

import pytest
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant, TenantMembership
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.knowledge.candidate_extraction import (
    CandidateExtractionResult,
    CandidateExtractionUsageSummary,
)
from backend.services.knowledge.contracts import (
    ObservationCreate,
    parse_semantic_inputs_from_execution,
)
from backend.services.knowledge.historical_backfill_service import (
    KnowledgeHistoricalBackfillService,
)
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.pentest_facts import (
    KnowledgeFactContext,
    build_knowledge_observations,
)
from backend.services.knowledge.replay_source_resolver import KnowledgeReplaySourceResolver
from backend.services.knowledge.replay_service import KnowledgeReplayService
from runtime_shared.semantic.canonical_keys import (
    build_host_dns_key,
    build_host_ip_key,
    build_relationship_edge_key,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope
from runtime_shared.semantic.web_common import build_finding_subject_key


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


class _RecordingIngestionService:
    def __init__(self) -> None:
        self.calls = []

    def ingest_execution_payload(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "status": "succeeded",
            "projection_status": "succeeded",
            "ingestion_run_id": None,
        }


class _StaticReplaySourceResolver:
    def __init__(self, *, source_kind: str) -> None:
        self.source_kind = source_kind

    def resolve_source(self, *, source_execution_id: str, task_id: int | None):
        return {
            "source_kind": self.source_kind,
            "engagement_id": 101,
            "task_id": task_id,
            "execution_payload": {
                "execution": {
                    "execution_id": source_execution_id,
                    "tool_name": "historical.strict-admission.fixture",
                    "execution_metadata": {
                        "semantic_observations": [],
                        "semantic_evidence": [],
                    },
                },
                "artifacts": [],
            },
            "compact_output_hint": None,
        }


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


def _semantic_metadata(
    rows: list[dict[str, object]],
    *,
    capability_family: str,
    schema_version: str,
) -> dict[str, object]:
    return {
        "semantic_observations": rows,
        "semantic_evidence": [],
        "semantic_schema_version": schema_version,
        "capability_family": capability_family,
    }


def _host_discovered_row(ip: str) -> dict[str, object]:
    return {
        "observation_type": "network.host_discovered",
        "subject_type": "host.ip",
        "subject_key": f"host.ip:{ip}",
        "payload": {"ip": ip, "source": "replay-test"},
    }


def _open_port_row(ip: str, port: int, *, service_name: str) -> dict[str, object]:
    return {
        "observation_type": "network.open_port",
        "subject_type": "service.socket",
        "subject_key": f"service.socket:{ip}/tcp/{port}",
        "payload": {
            "ip": ip,
            "protocol": "tcp",
            "port": port,
            "service_name": service_name,
            "source": "replay-test",
        },
    }


def _web_path_row(target_url: str, path: str, *, status_code: int) -> dict[str, object]:
    url = f"{target_url.rstrip('/')}/{path.lstrip('/')}"
    return {
        "observation_type": "web.path_discovered",
        "subject_type": "web.path",
        "subject_key": f"web.path:{url}",
        "payload": {
            "url": url,
            "target_url": target_url,
            "path": f"/{path.lstrip('/')}",
            "status_code": status_code,
            "source": "replay-test",
        },
    }


def _supported_historical_semantic_cases() -> tuple[dict[str, object], ...]:
    dns_key = build_host_dns_key("api.example.test")
    ip_key = build_host_ip_key("192.0.2.20")
    relationship_key = build_relationship_edge_key(
        source_subject_key=dns_key,
        relationship_type="resolves_to",
        target_subject_key=ip_key,
    )
    nuclei_url = "https://example.test/admin"
    return (
        {
            "name": "nmap",
            "tool_name": "information_gathering.network_discovery.nmap",
            "tool_arguments": {"target": "192.0.2.20"},
            "capability_family": "network_discovery",
            "semantic_schema_version": "nmap.v1",
            "rows": [
                {
                    "observation_type": "network.host_discovered",
                    "subject_type": "host.ip",
                    "subject_key": ip_key,
                    "payload": {"source": "nmap", "host_status": "up"},
                }
            ],
        },
        {
            "name": "nuclei",
            "tool_name": "web_applications.web_vulnerability_scanners.nuclei",
            "tool_arguments": {"target": nuclei_url},
            "capability_family": "vulnerability_scanning",
            "semantic_schema_version": "nuclei.v1",
            "rows": [
                {
                    "observation_type": "finding.vulnerability_detected",
                    "subject_type": "finding.instance",
                    "subject_key": build_finding_subject_key(
                        detector_id="nuclei/cve-2026-0001",
                        target_url=nuclei_url,
                        variant_id="admin-panel",
                    ),
                    "payload": {
                        "source": "nuclei",
                        "detector_id": "nuclei/cve-2026-0001",
                        "target_url": nuclei_url,
                        "matcher_id": "admin-panel",
                        "severity": "high",
                    },
                },
            ],
        },
        {
            "name": "amass",
            "tool_name": "information_gathering.dns.amass",
            "tool_arguments": {"domain": "example.test"},
            "capability_family": "dns_enumeration",
            "semantic_schema_version": "amass.v1",
            "rows": [
                {
                    "observation_type": "dns.name_discovered",
                    "subject_type": "host.dns",
                    "subject_key": dns_key,
                    "payload": {"dns_name": "api.example.test", "tool_source": "amass"},
                },
                {
                    "observation_type": "dns.address_resolved",
                    "subject_type": "host.ip",
                    "subject_key": ip_key,
                    "payload": {
                        "address": "192.0.2.20",
                        "record_type": "A",
                        "tool_source": "amass",
                    },
                },
                {
                    "observation_type": "relationship.resolves_to",
                    "subject_type": "relationship.edge",
                    "subject_key": relationship_key,
                    "payload": {
                        "source_subject_type": "host.dns",
                        "source_subject_key": dns_key,
                        "relationship_type": "resolves_to",
                        "target_subject_type": "host.ip",
                        "target_subject_key": ip_key,
                        "record_type": "A",
                        "tool_source": "amass",
                    },
                },
            ],
        },
    )


def _canonical_snapshot_from_resolved_source(
    db,
    *,
    resolved_source: dict[str, object],
    tenant_id: int,
    user_id: int,
    source_execution_id: str,
) -> list[dict[str, object]]:
    execution_payload = dict(resolved_source["execution_payload"])
    execution = dict(execution_payload["execution"])
    semantic_inputs = parse_semantic_inputs_from_execution(execution)
    envelope = SemanticFactEnvelope(
        semantic_schema_version=semantic_inputs.get("semantic_schema_version"),
        capability_family=semantic_inputs.get("capability_family"),
        observations=tuple(semantic_inputs.get("semantic_observations") or ()),
        evidence=tuple(semantic_inputs.get("semantic_evidence") or ()),
    )
    archives = (
        db.query(KnowledgeEvidenceArchive)
        .filter(KnowledgeEvidenceArchive.source_execution_id == source_execution_id)
        .order_by(KnowledgeEvidenceArchive.created_at.asc(), KnowledgeEvidenceArchive.id.asc())
        .all()
    )
    result = build_knowledge_observations(
        envelope=envelope,
        context=KnowledgeFactContext(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=int(resolved_source["engagement_id"]),
            task_id=resolved_source.get("task_id"),
            source_execution_id=source_execution_id,
            ingestion_run_id="canonical-reference",
            observed_at=None,
            artifact_summaries=tuple(
                dict(item) for item in execution_payload.get("artifacts", [])
            ),
            evidence_archives=tuple(archives),
        ),
    )
    assert result.compiled.input_count == len(envelope.observations)
    assert result.compiled.accepted_count == len(envelope.observations)
    assert result.compiled.rejected_count == 0
    assert result.compiled.diagnostics == ()
    return _observation_snapshot(result.observations, include_run_id=False)


def _run_observation_snapshot(db, *, ingestion_run_id: object) -> list[dict[str, object]]:
    rows = (
        db.query(KnowledgeObservation)
        .filter(KnowledgeObservation.ingestion_run_id == ingestion_run_id)
        .order_by(KnowledgeObservation.created_at.asc(), KnowledgeObservation.id.asc())
        .all()
    )
    return _observation_snapshot(rows, include_run_id=False)


def _observation_snapshot(
    observations: list[object] | tuple[object, ...],
    *,
    include_run_id: bool,
) -> list[dict[str, object]]:
    snapshot: list[dict[str, object]] = []
    for item in observations:
        entry = {
            "observation_type": str(item.observation_type),
            "subject_type": str(item.subject_type),
            "subject_key": str(item.subject_key),
            "assertion_level": str(item.assertion_level),
            "payload": dict(item.payload or {}),
            "observation_metadata": dict(item.observation_metadata or {}),
            "lineage": {
                "user_id": int(item.user_id),
                "engagement_id": int(item.engagement_id),
                "task_id": item.task_id,
                "source_execution_id": str(item.source_execution_id),
            },
            "dedupe_key": str(item.dedupe_key),
        }
        if include_run_id:
            entry["lineage"]["ingestion_run_id"] = str(item.ingestion_run_id)
        snapshot.append(entry)
    snapshot.sort(
        key=lambda item: (
            str(item["observation_type"]),
            str(item["subject_type"]),
            str(item["subject_key"]),
            json.dumps(item["payload"], sort_keys=True, separators=(",", ":")),
        )
    )
    return snapshot


def _projection_snapshot(db, *, engagement_id: int) -> dict[str, object]:
    return {
        "assets": [
            {
                "asset_key": row.asset_key,
                "asset_type": row.asset_type,
                "ip_address": row.ip_address,
                "hostname": row.hostname,
            }
            for row in db.query(KnowledgeAsset)
            .filter(KnowledgeAsset.engagement_id == engagement_id)
            .order_by(KnowledgeAsset.asset_key.asc())
            .all()
        ],
        "services": [
            {
                "service_key": row.service_key,
                "protocol": row.protocol,
                "port": row.port,
                "service_name": row.service_name,
            }
            for row in db.query(KnowledgeService)
            .filter(KnowledgeService.engagement_id == engagement_id)
            .order_by(KnowledgeService.service_key.asc())
            .all()
        ],
        "findings": [
            {
                "finding_key": row.finding_key,
                "finding_type": row.finding_type,
                "subject_key": row.subject_key,
                "severity": row.severity,
                "assertion_level": row.assertion_level,
            }
            for row in db.query(KnowledgeFinding)
            .filter(KnowledgeFinding.engagement_id == engagement_id)
            .order_by(KnowledgeFinding.finding_key.asc())
            .all()
        ],
        "relationships": [
            {
                "relationship_key": row.relationship_key,
                "source_subject_key": row.source_subject_key,
                "relationship_type": row.relationship_type,
                "target_subject_key": row.target_subject_key,
            }
            for row in db.query(KnowledgeRelationship)
            .filter(KnowledgeRelationship.engagement_id == engagement_id)
            .order_by(KnowledgeRelationship.relationship_key.asc())
            .all()
        ],
        "web_paths": [
            {
                "canonical_url": row.canonical_url,
                "path": row.path,
                "last_status_code": row.last_status_code,
            }
            for row in db.query(KnowledgeWebPath)
            .join(EngagementWebPathLink, EngagementWebPathLink.web_path_id == KnowledgeWebPath.id)
            .filter(EngagementWebPathLink.engagement_id == engagement_id)
            .order_by(KnowledgeWebPath.canonical_url.asc())
            .all()
        ],
    }


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


def test_runtime_and_durable_replay_enter_the_same_ingestion_payload_boundary() -> None:
    source_execution_ids = {
        "runtime": "00000000-0000-4000-8000-000000000201",
        "durable_archive": "00000000-0000-4000-8000-000000000202",
    }
    for source_kind, source_execution_id in source_execution_ids.items():
        engine, db = _build_session()
        try:
            ingestion_service = _RecordingIngestionService()
            replay_service = KnowledgeReplayService(
                db,
                ingestion_service=ingestion_service,
                replay_source_resolver=_StaticReplaySourceResolver(source_kind=source_kind),
            )

            replay = replay_service.replay_execution(
                task_id=202,
                source_execution_id=source_execution_id,
                extractor_family="runtime.ingestion",
                target_extractor_version=f"{source_kind}.target",
            )

            assert replay["ok"] is True
            assert replay["replay_source_type"] == source_kind
            assert len(ingestion_service.calls) == 1
            call = ingestion_service.calls[0]
            assert call["engagement_id"] == 101
            assert call["task_id"] == 202
            assert call["source_execution_id"] == source_execution_id
            assert call["replay_source_type"] == source_kind
            assert call["reuse_existing_archive_rows"] is True
            assert call["raise_on_error"] is True
        finally:
            db.close()
            engine.dispose()


def test_supported_historical_snapshots_replay_with_canonical_parity() -> None:
    for case in _supported_historical_semantic_cases():
        engine, db = _build_session()
        try:
            user, engagement, task = _seed_user_engagement_task(db)
            rows = list(case["rows"])
            execution_metadata = {
                "semantic_observations": rows,
                "semantic_evidence": [],
                "semantic_schema_version": case["semantic_schema_version"],
                "capability_family": case["capability_family"],
            }
            execution_id = _seed_execution_with_artifact_payload(
                db,
                task_id=task.id,
                tenant_id=task.tenant_id,
                tool_name=str(case["tool_name"]),
                tool_arguments=dict(case["tool_arguments"]),
                content_text=f"canonical historical replay fixture: {case['name']}",
                execution_metadata=execution_metadata,
            )
            ingestion_service = KnowledgeIngestionService(db)
            replay_service = KnowledgeReplayService(
                db,
                ingestion_service=ingestion_service,
            )
            initial = ingestion_service.ingest_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                extractor_family="runtime.ingestion",
                extractor_version=f"{case['name']}.baseline",
                delete_survival_required=True,
                raise_on_error=True,
            )
            assert initial["ok"] is True

            before_replay_count = (
                db.query(KnowledgeObservation)
                .filter(KnowledgeObservation.source_execution_id == execution_id)
                .count()
            )
            runtime_resolved = replay_service.replay_source_resolver.resolve_source(
                source_execution_id=execution_id,
                task_id=task.id,
            )
            runtime_canonical = _canonical_snapshot_from_resolved_source(
                db,
                resolved_source=runtime_resolved,
                tenant_id=int(task.tenant_id),
                user_id=int(user.id),
                source_execution_id=execution_id,
            )
            runtime_replay = replay_service.replay_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                extractor_family="runtime.ingestion",
                target_extractor_version=f"{case['name']}.runtime-replay",
            )
            runtime_snapshot = _run_observation_snapshot(
                db,
                ingestion_run_id=runtime_replay["ingestion_run_id"],
            )
            runtime_projection = _projection_snapshot(db, engagement_id=engagement.id)

            durable_resolved = replay_service.replay_source_resolver.resolve_source(
                source_execution_id=execution_id,
                task_id=None,
            )
            durable_canonical = _canonical_snapshot_from_resolved_source(
                db,
                resolved_source=durable_resolved,
                tenant_id=int(task.tenant_id),
                user_id=int(user.id),
                source_execution_id=execution_id,
            )
            durable_replay = replay_service.replay_execution(
                task_id=None,
                source_execution_id=execution_id,
                extractor_family="runtime.ingestion",
                target_extractor_version=f"{case['name']}.durable-replay",
            )
            durable_snapshot = _run_observation_snapshot(
                db,
                ingestion_run_id=durable_replay["ingestion_run_id"],
            )
            durable_projection = _projection_snapshot(db, engagement_id=engagement.id)
            after_replay_count = (
                db.query(KnowledgeObservation)
                .filter(KnowledgeObservation.source_execution_id == execution_id)
                .count()
            )
            backfill_gate = KnowledgeHistoricalBackfillService(
                db
            ).verify_after_replay_backfill(
                target_engagement_ids=[engagement.id],
                verify_idempotent_rerun=True,
                replay_extractor_family="runtime.ingestion",
                replay_extractor_version=f"{case['name']}.durable-replay",
                require_replay_runs=True,
            )

            assert runtime_resolved["source_kind"] == "runtime"
            assert durable_resolved["source_kind"] == "durable_archive"
            assert runtime_canonical == durable_canonical
            assert runtime_snapshot == runtime_canonical
            assert durable_snapshot == durable_canonical
            assert runtime_projection == durable_projection
            assert after_replay_count == before_replay_count + (2 * len(rows))
            assert all(
                item["lineage"]["source_execution_id"] == execution_id
                for item in runtime_snapshot
            )
            assert all(
                item["lineage"]["source_execution_id"] == execution_id
                for item in durable_snapshot
            )
            persisted_replay_tenants = {
                row.tenant_id
                for row in db.query(KnowledgeObservation)
                .filter(
                    KnowledgeObservation.ingestion_run_id.in_(
                        [
                            runtime_replay["ingestion_run_id"],
                            durable_replay["ingestion_run_id"],
                        ]
                    )
                )
                .all()
            }
            assert persisted_replay_tenants == {task.tenant_id}
            assert backfill_gate["completion_gate_passed"] is True
            assert backfill_gate["failed_engagement_count"] == 0
            assert backfill_gate["engagement_statuses"][0]["idempotent_rerun"]["ok"] is True
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


def test_replay_execution_reads_archived_file_snapshot_when_inline_excerpt_missing() -> None:
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
            execution_metadata=_semantic_metadata(
                [
                    _host_discovered_row("10.10.10.9"),
                    _open_port_row("10.10.10.9", 22, service_name="ssh"),
                ],
                capability_family="network_discovery",
                schema_version="nmap.v1",
            ),
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


def test_replay_execution_preserves_canonical_web_path_observations_from_object_ref(
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
            execution_metadata=_semantic_metadata(
                [
                    _web_path_row("http://example.test", "/admin", status_code=403),
                    _web_path_row("http://example.test", "/login", status_code=200),
                ],
                capability_family="web_discovery",
                schema_version="gobuster.v1",
            ),
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
