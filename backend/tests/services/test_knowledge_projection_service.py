"""Tests for projection service and per-model deterministic upserts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import partial
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.services.knowledge.candidate_extraction import (
    CandidateExtractionRequest,
    CandidateExtractionUsageSummary,
)
from backend.services.knowledge.candidate_extraction.mapping import map_structured_payload
from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.tshark_adapter import TsharkKnowledgeAdapter
from backend.services.knowledge.identity.canonical_keys import (
    build_finding_vulnerability_key,
    build_relationship_edge_key,
    build_secret_exposure_finding_key,
)
from backend.services.knowledge.projection.relationship_projector import RelationshipProjector
from backend.services.knowledge.contracts import ObservationCreate as _ObservationCreate
from backend.services.knowledge.projection_service import KnowledgeProjectionService
from backend.services.knowledge.query_service import KnowledgeQueryService

ObservationCreate = partial(_ObservationCreate, user_id=1)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_engagement(db, *, tenant_id: int = 1):
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (:id, :slug, :name, CURRENT_TIMESTAMP)"
        ),
        {"id": int(tenant_id), "slug": f"tenant-{tenant_id}", "name": f"Tenant {tenant_id}"},
    )
    user = User(username=f"execution-plane-projection-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=tenant_id, name="Execution Plane Projection", status="active")
    db.add(engagement)
    db.flush()
    return engagement


def _tshark_semantic_secret_exposure(*, fingerprint: str) -> dict:
    return {
        "observation_type": "finding.vulnerability_detected",
        "subject_type": "finding.vulnerability",
        "subject_key": (
            "finding.vulnerability:service.socket:203.0.113.20/tcp/80:"
            "tshark/credential_exposure_detected/http.authorization"
        ),
        "payload": {
            "detector_id": "tshark/credential_exposure_detected/http.authorization",
            "finding_subtype": "credential_exposure_detected",
            "title": "Credential material exposed in packet capture",
            "severity": "medium",
            "subject_key": "service.socket:203.0.113.20/tcp/80",
            "subject_type": "service.socket",
            "protocol": "http",
            "field": "http.authorization",
            "kind": "authorization_header",
            "frame": "7",
            "stream": "2",
            "src": "192.0.2.10",
            "dst": "203.0.113.20",
            "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
            "extraction_filter": "http.authorization",
            "proof_excerpt": "Authorization: Bearer <DURABLE_SECRET_MASK:token>",
            "fingerprint": fingerprint,
            "pcap_artifact_sha256": "pcap-sha256",
        },
    }


def test_tshark_adapter_masks_bare_ftp_protocol_auth_proof_from_metadata() -> None:
    raw_secret = "synthetic-ftp-password"
    metadata = {
        "schema_version": "tshark.v1",
        "analysis_mode": "secret_exposure",
        "pcap": {"artifact_sha256": "pcap-sha256"},
        "secret_exposure": [
            {
                "frame": "3",
                "stream": "9",
                "protocol": "ftp",
                "src": "192.0.2.20",
                "dst": "203.0.113.21",
                "field": "ftp.request.command_parameter",
                "flow_key": "tcp:192.0.2.20:49154->203.0.113.21:21",
                "extraction_filter": "ftp.request.command == PASS",
                "kind": "protocol_auth_argument",
                "proof_mode": "proof_excerpt",
                "proof_excerpt": raw_secret,
                "pcap_artifact_sha256": "pcap-sha256",
            }
        ],
    }
    context = AdapterContext(
        user_id=1,
        engagement_id=2,
        task_id=None,
        source_execution_id="exec-tshark-ftp-proof",
        ingestion_run_id="run-tshark-ftp-proof",
        execution_payload={
            "execution": {"tool_name": "sniffing_spoofing.network_sniffers.tshark"}
        },
        tool_metadata=metadata,
    )

    observations = TsharkKnowledgeAdapter().extract(context)
    finding = next(
        item
        for item in observations
        if item.observation_type == "finding.vulnerability_detected"
    )

    assert finding.payload["proof_excerpt"] == "<DURABLE_SECRET_MASK:secret>"
    assert finding.payload["exposure_proof_id"].endswith("<DURABLE_SECRET_MASK:secret>")
    assert "ftp.request.command_parameter" in finding.payload["field"]
    assert raw_secret not in str([item.payload for item in observations])
    assert raw_secret not in finding.subject_key


def test_projection_service_upserts_all_execution_plane_read_models() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.vulnerability:service.socket:10.10.10.5/tcp/443:nuclei/cve-2023-1234"
        relationship_key = build_relationship_edge_key(
            source_subject_key="host.ip:10.10.10.5",
            relationship_type="exploits",
            target_subject_key="host.ip:10.10.10.7",
        )
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-execution-plane-1",
                ingestion_run_id="run-execution-plane-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.5",
                assertion_level="observed",
                payload={"host_status": "up", "confidence": "medium"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-execution-plane-1",
                ingestion_run_id="run-execution-plane-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.5/tcp/443",
                assertion_level="observed",
                payload={"confidence": "medium"},
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-execution-plane-1",
                ingestion_run_id="run-execution-plane-1",
                observation_type="network.service_detected",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.5/tcp/443",
                assertion_level="observed",
                payload={"service_name": "nginx", "product": "nginx", "version": "1.24", "confidence": "high"},
                observed_at=now + timedelta(seconds=2),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-execution-plane-1",
                ingestion_run_id="run-execution-plane-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={"detector_id": "nuclei/cve-2023-1234", "severity": "high", "confidence": "medium"},
                observed_at=now + timedelta(seconds=3),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-execution-plane-1",
                ingestion_run_id="run-execution-plane-1",
                observation_type="relationship.exploits",
                subject_type="relationship.edge",
                subject_key=relationship_key,
                assertion_level="observed",
                payload={
                    "source_subject_key": "host.ip:10.10.10.5",
                    "relationship_type": "exploits",
                    "target_subject_key": "host.ip:10.10.10.7",
                    "confidence": "high",
                },
                observed_at=now + timedelta(seconds=4),
            ),
        ]

        result = service.project_observations(engagement_id=engagement.id, observations=observations)

        assert result.asset_upsert_count == 1
        assert result.service_upsert_count == 1
        assert result.finding_upsert_count == 1
        assert result.relationship_upsert_count == 1

        asset = db.query(KnowledgeAsset).filter(KnowledgeAsset.engagement_id == engagement.id).one()
        projected_service = db.query(KnowledgeService).filter(KnowledgeService.engagement_id == engagement.id).one()
        finding = db.query(KnowledgeFinding).filter(KnowledgeFinding.engagement_id == engagement.id).one()
        relationship = db.query(KnowledgeRelationship).filter(KnowledgeRelationship.engagement_id == engagement.id).one()

        assert projected_service.asset_id == asset.id
        assert finding.service_id == projected_service.id
        assert relationship.relationship_key == relationship_key
    finally:
        db.close()
        engine.dispose()


def test_projection_service_links_hydra_confirmed_finding_to_service_asset_and_graph() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        projection_service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        service_key = "service.socket:192.168.1.100/tcp/22"
        finding_key = build_finding_vulnerability_key(
            subject_key=service_key,
            detector_id="hydra/weak-auth",
        )
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-hydra-link-1",
                ingestion_run_id="run-hydra-link-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:192.168.1.100",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-hydra-link-1",
                ingestion_run_id="run-hydra-link-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"port": 22, "protocol": "tcp", "ip": "192.168.1.100"},
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-hydra-link-1",
                ingestion_run_id="run-hydra-link-1",
                observation_type="network.service_detected",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"service_name": "ssh"},
                observed_at=now + timedelta(seconds=2),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-hydra-link-1",
                ingestion_run_id="run-hydra-link-1",
                observation_type="finding.vulnerability_confirmed",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="confirmed",
                payload={
                    "detector_id": "hydra/weak-auth",
                    "title": "Weak authentication confirmed on SSH",
                    "subject_key": service_key,
                    "subject_type": "service.socket",
                    "finding_subtype": "credential_compromise_confirmed",
                    "confidence": "confirmed",
                    "successful_login_count": 1,
                    "account_identifier": "admin",
                    "durable_masking_applied": True,
                },
                observed_at=now + timedelta(seconds=3),
            ),
        ]

        result = projection_service.project_observations(
            engagement_id=engagement.id,
            observations=observations,
        )

        assert result.finding_upsert_count == 1
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        projected_service = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == service_key,
        ).one()
        asset = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.engagement_id == engagement.id,
            KnowledgeAsset.asset_key == "host.ip:192.168.1.100",
        ).one()

        assert finding.service_id == projected_service.id
        assert finding.asset_id == asset.id
        assert finding.status == "confirmed"
        assert finding.severity == "high"
        assert finding.subject_key == service_key
        metadata = dict(finding.finding_metadata or {})
        assert metadata["severity_resolution"] == {
            "policy_version": "knowledge-severity.v1",
            "source": "policy_default",
            "signal": "finding_subtype:credential_compromise_confirmed",
            "severity": "high",
        }

        snapshot = KnowledgeQueryService(db).get_graph_snapshot(user_id=engagement.user_id)
        has_finding_edges = [
            edge for edge in snapshot["edges"]
            if edge["relationship_type"] == "has_finding" and edge["target"] == finding_key
        ]
        assert {edge["source"] for edge in has_finding_edges} == {
            "host.ip:192.168.1.100",
            service_key,
        }
    finally:
        db.close()
        engine.dispose()


def test_projection_service_resolves_successful_exploit_severity_from_policy() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.instance:msfconsole:exploit/unix/webapp/drupal_drupalgeddon2:target-192.168.196.16"

        result = service.project_observations(
            engagement_id=engagement.id,
            observations=[
                ObservationCreate(
                    engagement_id=engagement.id,
                    task_id=None,
                    source_execution_id="exec-msf-exploit-1",
                    ingestion_run_id="run-msf-exploit-1",
                    observation_type="finding.exploit_succeeded",
                    subject_type="finding.instance",
                    subject_key=finding_key,
                    assertion_level="exploited",
                    payload={
                        "source": "msfconsole",
                        "detector_id": "exploit/unix/webapp/drupal_drupalgeddon2",
                        "session_count": 1,
                        "target_ip": "192.168.196.16",
                    },
                    observed_at=now,
                ),
            ],
        )

        assert result.finding_upsert_count == 1
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        assert finding.status == "exploited"
        assert finding.assertion_level == "exploited"
        assert finding.severity == "high"
        metadata = dict(finding.finding_metadata or {})
        assert metadata["severity_resolution"] == {
            "policy_version": "knowledge-severity.v1",
            "source": "policy_default",
            "signal": "observation_type:finding.exploit_succeeded",
            "severity": "high",
        }
    finally:
        db.close()
        engine.dispose()


def test_projection_service_projects_tshark_secret_exposure_without_raw_secret() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        service_key = "service.socket:203.0.113.20/tcp/80"
        proof_id = "hmac-sha256:bearer_token:abc123"
        finding_key = build_secret_exposure_finding_key(
            subject_key=service_key,
            detector_id="tshark/secret_exposure/http.authorization",
            protocol="http",
            exposure_kind="authorization_header",
            flow_key="tcp:192.0.2.10:49152->203.0.113.20:80",
            proof_id=proof_id,
        )
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tshark-exposure-1",
                ingestion_run_id="run-tshark-exposure-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"ip": "203.0.113.20", "protocol": "tcp", "port": 80},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tshark-exposure-1",
                ingestion_run_id="run-tshark-exposure-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "detector_id": "tshark/secret_exposure/http.authorization",
                    "finding_subtype": "secret_exposure_detected",
                    "title": "Secret material exposed in packet capture",
                    "subject_key": service_key,
                    "subject_type": "service.socket",
                    "protocol": "http",
                    "kind": "authorization_header",
                    "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                    "exposure_proof_id": proof_id,
                    "proof_excerpt": "Authorization: Bearer <DURABLE_SECRET_MASK:token>",
                    "evidence_refs": [{"evidence_archive_id": "archive-tshark-1"}],
                },
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tshark-exposure-2",
                ingestion_run_id="run-tshark-exposure-2",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "detector_id": "tshark/secret_exposure/http.authorization",
                    "finding_subtype": "secret_exposure_detected",
                    "subject_key": service_key,
                    "proof_excerpt": "Authorization: Bearer <DURABLE_SECRET_MASK:token>",
                    "evidence_refs": [{"evidence_archive_id": "archive-tshark-1"}],
                },
                observed_at=now + timedelta(seconds=2),
            ),
        ]

        result = service.project_observations(engagement_id=engagement.id, observations=observations)

        assert result.finding_upsert_count == 1
        assert observations[1].payload["proof_excerpt"] == (
            "Authorization: Bearer <DURABLE_SECRET_MASK:token>"
        )
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        assert finding.severity == "medium"
        metadata = dict(finding.finding_metadata or {})
        assert metadata["severity_resolution"] == {
            "policy_version": "knowledge-severity.v1",
            "source": "policy_default",
            "signal": "finding_subtype:secret_exposure_detected",
            "severity": "medium",
        }
        assert db.query(KnowledgeFinding).count() == 1
        assert finding.subject_key == service_key
        assert finding.evidence_summary == {
            "evidence_refs": [{"evidence_archive_id": "archive-tshark-1"}]
        }
        read_model_text = str(
            {
                "finding_key": finding.finding_key,
                "metadata": finding.finding_metadata,
                "evidence": finding.evidence_summary,
            }
        )
        assert "raw-token" not in read_model_text
        assert "Bearer raw-token" not in read_model_text
        assert "bearer_token-abc123" in finding.finding_key
    finally:
        db.close()
        engine.dispose()


def test_projection_service_keeps_semantic_tshark_secret_proofs_distinct_without_raw_secret() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        raw_secret = "Bearer raw-token"
        semantic_rows = [
            _tshark_semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:abc123"),
            _tshark_semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:def456"),
            _tshark_semantic_secret_exposure(fingerprint="hmac-sha256:bearer_token:abc123"),
        ]
        adapter_context = AdapterContext(
            user_id=engagement.user_id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id="exec-tshark-semantic-exposure-1",
            ingestion_run_id="run-tshark-semantic-exposure-1",
            execution_payload={
                "execution": {
                    "tool_name": "sniffing_spoofing.network_sniffers.tshark",
                    "execution_metadata": {"semantic_observations": semantic_rows},
                }
            },
            semantic_observations=semantic_rows,
        )
        observations = TsharkKnowledgeAdapter().extract(adapter_context)
        findings = [
            item for item in observations if item.observation_type == "finding.vulnerability_detected"
        ]

        result = KnowledgeProjectionService(db).project_observations(
            engagement_id=engagement.id,
            observations=observations,
        )

        projected = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
        ).all()
        read_model_text = str(
            [
                {
                    "finding_key": item.finding_key,
                    "metadata": item.finding_metadata,
                    "evidence": item.evidence_summary,
                }
                for item in projected
            ]
        )
        assert len(findings) == 2
        assert "<DURABLE_SECRET_MASK:token>" in str([item.payload for item in findings])
        assert result.finding_upsert_count == 2
        assert len(projected) == 2
        assert {item.finding_key for item in projected} == {item.subject_key for item in findings}
        assert "hmac-sha256-bearer_token-abc123" in read_model_text
        assert "hmac-sha256-bearer_token-def456" in read_model_text
        assert raw_secret not in read_model_text
    finally:
        db.close()
        engine.dispose()


def test_projection_service_is_idempotent_for_same_observation_batch() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-idempotent",
                ingestion_run_id="run-idempotent",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.25",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-idempotent",
                ingestion_run_id="run-idempotent",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.25/tcp/80",
                assertion_level="observed",
                payload={},
                observed_at=now + timedelta(seconds=1),
            ),
        ]

        first = service.project_observations(engagement_id=engagement.id, observations=observations)
        second = service.project_observations(engagement_id=engagement.id, observations=observations)

        assert first.asset_insert_count == 1
        assert first.service_insert_count == 1
        assert second.asset_insert_count == 0
        assert second.service_insert_count == 0
        assert db.query(KnowledgeAsset).filter(KnowledgeAsset.engagement_id == engagement.id).count() == 1
        assert db.query(KnowledgeService).filter(KnowledgeService.engagement_id == engagement.id).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_projection_service_keeps_same_tenant_same_key_separate_by_user() -> None:
    engine, db = _build_session()
    try:
        first_engagement = _seed_engagement(db, tenant_id=1)
        second_engagement = _seed_engagement(db, tenant_id=1)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)

        for engagement in (first_engagement, second_engagement):
            observations = [
                _ObservationCreate(
                    user_id=int(engagement.user_id),
                    engagement_id=engagement.id,
                    task_id=None,
                    source_execution_id=f"exec-shared-key-{engagement.id}",
                    ingestion_run_id=f"run-shared-key-{engagement.id}",
                    observation_type="network.host_discovered",
                    subject_type="host.ip",
                    subject_key="host.ip:10.10.10.88",
                    assertion_level="observed",
                    payload={"host_status": "up"},
                    observed_at=now,
                )
            ]
            result = service.project_observations(
                tenant_id=1,
                user_id=int(engagement.user_id),
                engagement_id=engagement.id,
                observations=observations,
            )
            assert result.asset_insert_count == 1

        rows = (
            db.query(KnowledgeAsset)
            .filter(
                KnowledgeAsset.tenant_id == 1,
                KnowledgeAsset.asset_key == "host.ip:10.10.10.88",
            )
            .all()
        )

        assert len(rows) == 2
        assert {int(row.user_id) for row in rows} == {
            int(first_engagement.user_id),
            int(second_engagement.user_id),
        }
    finally:
        db.close()
        engine.dispose()


def test_projection_service_web_path_projection_reports_counters() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-web-path-1",
                ingestion_run_id="run-web-path-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.50",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-web-path-1",
                ingestion_run_id="run-web-path-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.50/tcp/80",
                assertion_level="observed",
                payload={},
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-web-path-1",
                ingestion_run_id="run-web-path-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:http://10.10.10.50/admin",
                assertion_level="observed",
                payload={"source": "web_applications.web_crawlers.gobuster", "status_code": 200},
                observed_at=now + timedelta(seconds=2),
            ),
        ]

        result = service.project_observations(
            engagement_id=engagement.id,
            observations=observations,
        )
        assert result.web_path_upsert_count == 1
        assert result.web_path_insert_count == 1
        assert db.query(KnowledgeWebPath).count() == 1
        assert db.query(EngagementWebPathLink).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_projection_service_sets_tenant_id_on_canonical_rows_and_links() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, tenant_id=91)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = build_finding_vulnerability_key(
            subject_key="service.socket:10.10.10.11/tcp/443",
            detector_id="nuclei/cve-2024-9999",
        )
        relationship_key = build_relationship_edge_key(
            source_subject_key="host.ip:10.10.10.11",
            relationship_type="exposes",
            target_subject_key="host.ip:10.10.10.21",
        )
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tenant-1",
                ingestion_run_id="run-tenant-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.11",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tenant-1",
                ingestion_run_id="run-tenant-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.11/tcp/443",
                assertion_level="observed",
                payload={},
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tenant-1",
                ingestion_run_id="run-tenant-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={"severity": "high"},
                observed_at=now + timedelta(seconds=2),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tenant-1",
                ingestion_run_id="run-tenant-1",
                observation_type="relationship.exposes",
                subject_type="relationship.edge",
                subject_key=relationship_key,
                assertion_level="observed",
                payload={
                    "source_subject_key": "host.ip:10.10.10.11",
                    "relationship_type": "exposes",
                    "target_subject_key": "host.ip:10.10.10.21",
                },
                observed_at=now + timedelta(seconds=3),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-tenant-1",
                ingestion_run_id="run-tenant-1",
                observation_type="web.path_discovered",
                subject_type="web.path",
                subject_key="web.path:https://10.10.10.11/admin",
                assertion_level="observed",
                payload={"source": "web_applications.web_crawlers.gobuster", "status_code": 200},
                observed_at=now + timedelta(seconds=4),
            ),
        ]

        service.project_observations(engagement_id=engagement.id, observations=observations)

        assert db.query(KnowledgeAsset.tenant_id).one()[0] == 91
        assert db.query(KnowledgeService.tenant_id).one()[0] == 91
        assert db.query(KnowledgeFinding.tenant_id).one()[0] == 91
        assert db.query(KnowledgeRelationship.tenant_id).one()[0] == 91
        assert db.query(KnowledgeWebPath.tenant_id).one()[0] == 91
        assert db.query(EngagementAssetLink.tenant_id).one()[0] == 91
        assert db.query(EngagementServiceLink.tenant_id).one()[0] == 91
        assert db.query(EngagementFindingLink.tenant_id).one()[0] == 91
        assert db.query(EngagementWebPathLink.tenant_id).one()[0] == 91
    finally:
        db.close()
        engine.dispose()


def test_projection_service_separates_identity_across_users_in_same_tenant() -> None:
    engine, db = _build_session()
    try:
        engagement_one = _seed_engagement(db, tenant_id=120)
        engagement_two = _seed_engagement(db, tenant_id=120)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)

        first_observation = _ObservationCreate(
            user_id=int(engagement_one.user_id),
            engagement_id=engagement_one.id,
            task_id=None,
            source_execution_id="exec-tenant-shared-1",
            ingestion_run_id="run-tenant-shared-1",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.20.30.40",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=now,
        )
        second_observation = _ObservationCreate(
            user_id=int(engagement_two.user_id),
            engagement_id=engagement_two.id,
            task_id=None,
            source_execution_id="exec-tenant-shared-2",
            ingestion_run_id="run-tenant-shared-2",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.20.30.40",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=now + timedelta(seconds=5),
        )

        first = service.project_observations(
            engagement_id=engagement_one.id,
            observations=[first_observation],
        )
        second = service.project_observations(
            engagement_id=engagement_two.id,
            observations=[second_observation],
        )

        assert first.asset_insert_count == 1
        assert second.asset_insert_count == 1
        rows = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.tenant_id == 120,
            KnowledgeAsset.asset_key == "host.ip:10.20.30.40",
        ).all()
        assert len(rows) == 2
        assert {int(row.user_id) for row in rows} == {
            int(engagement_one.user_id),
            int(engagement_two.user_id),
        }

        asset_ids = {row.id for row in rows}
        links = db.query(EngagementAssetLink).filter(EngagementAssetLink.tenant_id == 120).all()
        assert {int(link.engagement_id) for link in links} == {engagement_one.id, engagement_two.id}
        assert {link.asset_id for link in links} == asset_ids
    finally:
        db.close()
        engine.dispose()


def test_projection_service_keeps_overlapping_identity_keys_isolated_per_tenant() -> None:
    engine, db = _build_session()
    try:
        engagement_tenant_a = _seed_engagement(db, tenant_id=121)
        engagement_tenant_b = _seed_engagement(db, tenant_id=122)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)

        observation_tenant_a = _ObservationCreate(
            user_id=int(engagement_tenant_a.user_id),
            engagement_id=engagement_tenant_a.id,
            task_id=None,
            source_execution_id="exec-tenant-a",
            ingestion_run_id="run-tenant-a",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.20.30.41",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=now,
        )
        observation_tenant_b = _ObservationCreate(
            user_id=int(engagement_tenant_b.user_id),
            engagement_id=engagement_tenant_b.id,
            task_id=None,
            source_execution_id="exec-tenant-b",
            ingestion_run_id="run-tenant-b",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.20.30.41",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=now + timedelta(seconds=5),
        )

        first = service.project_observations(
            engagement_id=engagement_tenant_a.id,
            observations=[observation_tenant_a],
        )
        second = service.project_observations(
            engagement_id=engagement_tenant_b.id,
            observations=[observation_tenant_b],
        )

        assert first.asset_insert_count == 1
        assert second.asset_insert_count == 1
        rows = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.asset_key == "host.ip:10.20.30.41",
        ).all()
        assert len(rows) == 2
        assert {int(row.tenant_id) for row in rows} == {121, 122}
    finally:
        db.close()
        engine.dispose()


def test_projection_service_rejects_missing_engagement_context() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, tenant_id=55)
        service = KnowledgeProjectionService(db)
        observation = ObservationCreate(
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id="exec-missing-engagement",
            ingestion_run_id="run-missing-engagement",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.55",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=datetime.now(timezone.utc),
        )

        try:
            service.project_observations(observations=[observation])
            raise AssertionError("Expected projection to reject missing engagement context")
        except ValueError as exc:
            assert str(exc) == "engagement_id is required for projection writes"
    finally:
        db.close()
        engine.dispose()


def test_projection_service_rejects_mismatched_observation_engagement_scope() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db, tenant_id=67)
        other_engagement = _seed_engagement(db, tenant_id=67)
        service = KnowledgeProjectionService(db)
        observation = ObservationCreate(
            engagement_id=other_engagement.id,
            task_id=None,
            source_execution_id="exec-engagement-mismatch",
            ingestion_run_id="run-engagement-mismatch",
            observation_type="network.host_discovered",
            subject_type="host.ip",
            subject_key="host.ip:10.10.10.67",
            assertion_level="observed",
            payload={"host_status": "up"},
            observed_at=datetime.now(timezone.utc),
        )

        try:
            service.project_observations(engagement_id=engagement.id, observations=[observation])
            raise AssertionError("Expected projection to reject mismatched observation engagement scope")
        except ValueError as exc:
            assert str(exc).startswith("Observation engagement scope mismatch.")
            assert f"Expected engagement_id={engagement.id}" in str(exc)
            assert f"got={other_engagement.id}" in str(exc)
    finally:
        db.close()
        engine.dispose()


def test_projection_service_updates_finding_timestamps_confidence_and_evidence_deterministically() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.vulnerability:service.socket:10.10.10.5/tcp/443:nuclei/cve-2023-1234"

        batch_one = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-finding-1",
                ingestion_run_id="run-finding-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={"confidence": "medium", "evidence_refs": [{"evidence_archive_id": "ev-a1"}]},
                observed_at=now,
            )
        ]
        batch_two = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-finding-2",
                ingestion_run_id="run-finding-2",
                observation_type="finding.vulnerability_confirmed",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="confirmed",
                payload={"confidence": "high", "evidence_refs": [{"evidence_archive_id": "ev-a2"}]},
                observed_at=now + timedelta(minutes=10),
            )
        ]

        service.project_observations(engagement_id=engagement.id, observations=batch_one)
        service.project_observations(engagement_id=engagement.id, observations=batch_two)

        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        assert finding.first_seen_at.replace(tzinfo=timezone.utc) == now
        assert finding.last_seen_at.replace(tzinfo=timezone.utc) == now + timedelta(minutes=10)
        assert finding.confidence == "high"
        assert finding.status == "confirmed"
        refs = (finding.evidence_summary or {}).get("evidence_refs") or []
        assert len(refs) == 2
    finally:
        db.close()
        engine.dispose()


def test_projection_service_links_nmap_curated_findings_to_service_and_persists_detection_state() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        service_key = "service.socket:10.10.10.5/tcp/21"
        finding_key = build_finding_vulnerability_key(
            subject_key=service_key,
            detector_id="nmap/ftp-anon",
        )
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nmap-link-1",
                ingestion_run_id="run-nmap-link-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.5",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nmap-link-1",
                ingestion_run_id="run-nmap-link-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"port": 21, "protocol": "tcp", "ip": "10.10.10.5"},
                observed_at=now + timedelta(seconds=1),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nmap-link-1",
                ingestion_run_id="run-nmap-link-1",
                observation_type="network.service_detected",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"service_name": "ftp", "product": "vsftpd", "version": "3.0.3"},
                observed_at=now + timedelta(seconds=2),
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nmap-link-1",
                ingestion_run_id="run-nmap-link-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "detector_id": "nmap/ftp-anon",
                    "script_id": "ftp-anon",
                    "summary": "Anonymous FTP login allowed (FTP code 230)",
                    "subject_key": service_key,
                    "severity": "medium",
                    "title": "Anonymous FTP login allowed",
                },
                observed_at=now + timedelta(seconds=3),
            ),
        ]

        result = service.project_observations(engagement_id=engagement.id, observations=observations)

        assert result.finding_upsert_count == 1
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        projected_service = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == service_key,
        ).one()

        assert finding.service_id == projected_service.id
        assert finding.subject_key == service_key
        state = dict((finding.finding_metadata or {}).get("state") or {})
        assert state["finding_presence"] == "present"
        assert state["severity"] == "medium"
        assert state["detector_id"] == "nmap/ftp-anon"
        assert state["script_id"] == "ftp-anon"
        assert state["summary"] == "Anonymous FTP login allowed (FTP code 230)"
    finally:
        db.close()
        engine.dispose()


def test_projection_service_preserves_service_contradictions_across_batches() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        service_key = "service.socket:10.10.10.30/tcp/80"
        batch_one = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-svc-1",
                ingestion_run_id="run-svc-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-svc-1",
                ingestion_run_id="run-svc-1",
                observation_type="network.service_detected",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"service_name": "http"},
                observed_at=now + timedelta(seconds=1),
            ),
        ]
        batch_two = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-svc-2",
                ingestion_run_id="run-svc-2",
                observation_type="network.service_detected",
                subject_type="service.socket",
                subject_key=service_key,
                assertion_level="observed",
                payload={"service_name": "nginx"},
                observed_at=now + timedelta(minutes=1),
            )
        ]

        first = service.project_observations(engagement_id=engagement.id, observations=batch_one)
        second = service.project_observations(engagement_id=engagement.id, observations=batch_two)

        projected_service = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == service_key,
        ).one()
        contradictions = list((projected_service.service_metadata or {}).get("contradictions") or [])
        assert projected_service.service_name == "nginx"
        assert len(contradictions) == 1
        assert contradictions[0]["field"] == "service_name"
        assert contradictions[0]["previous"] == "http"
        assert contradictions[0]["incoming"] == "nginx"
        assert first.contradiction_count == 0
        assert second.contradiction_count == 1
        assert (second.contradiction_count_by_domain or {}).get("service") == 1
    finally:
        db.close()
        engine.dispose()


def test_projection_service_merges_nuclei_rich_details_across_batches() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.instance:cve-2024-0001:https://example.com/login"

        batch_one = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nuclei-rich-1",
                ingestion_run_id="run-nuclei-rich-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.instance",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "source": "nuclei",
                    "detector_id": "cve-2024-0001",
                    "target_url": "https://example.com/login",
                    "severity": "high",
                    "title": "Exposed Default Login Page",
                    "description_summary": "Default admin login page exposed to unauthenticated users.",
                    "classification": {
                        "cve_ids": ["CVE-2024-0001"],
                        "cwe_ids": ["CWE-200"],
                    },
                    "references": ["https://vendor.example/advisory"],
                    "extracted_results": ["admin portal"],
                },
                observed_at=now,
            )
        ]
        batch_two = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-nuclei-rich-2",
                ingestion_run_id="run-nuclei-rich-2",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.instance",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "source": "nuclei",
                    "detector_id": "cve-2024-0001",
                    "target_url": "https://example.com/login",
                    "severity": "high",
                    "tags": ["default-login", "panel"],
                    "matched_at": "https://example.com/login",
                },
                observed_at=now + timedelta(minutes=5),
            )
        ]

        service.project_observations(engagement_id=engagement.id, observations=batch_one)
        service.project_observations(engagement_id=engagement.id, observations=batch_two)

        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()

        metadata = dict(finding.finding_metadata or {})
        state = dict(metadata.get("state") or {})
        rich_details = dict(metadata.get("rich_details") or {})

        assert finding.title == "Exposed Default Login Page"
        assert state["description_summary"] == "Default admin login page exposed to unauthenticated users."
        assert state["matched_at"] == "https://example.com/login"
        assert rich_details["classification"] == {
            "cve_ids": ["CVE-2024-0001"],
            "cwe_ids": ["CWE-200"],
        }
        assert rich_details["references"] == ["https://vendor.example/advisory"]
        assert rich_details["extracted_results"] == ["admin portal"]
        assert rich_details["tags"] == ["default-login", "panel"]
    finally:
        db.close()
        engine.dispose()


def test_projection_service_preserves_non_overlapping_rich_details_and_overwrites_overlapping_keys_across_batches() -> None:
    """Prove that multi-run projection merges rich_details correctly.

    Batch 1 introduces classification (with cve+cwe) and tags.
    Batch 2 introduces a narrower classification (cve only) and extracted_results.

    After merge:
    - classification is overwritten (last-writer-wins), losing cwe_ids from batch 1
    - tags from batch 1 survive because batch 2 has no tags key
    - extracted_results from batch 2 appear
    """
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.instance:cve-2024-0001:https://example.com/login:variant-overwrite"

        batch_one = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-overwrite-1",
                ingestion_run_id="run-overwrite-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.instance",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "source": "nuclei",
                    "detector_id": "cve-2024-0001",
                    "target_url": "https://example.com/login",
                    "severity": "high",
                    "title": "Exposed Default Login Page",
                    "classification": {
                        "cve_ids": ["CVE-2024-0001"],
                        "cwe_ids": ["CWE-200"],
                    },
                    "tags": ["panel", "exposure"],
                    "references": ["https://vendor.example/advisory-v1"],
                },
                observed_at=now,
            )
        ]
        batch_two = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-overwrite-2",
                ingestion_run_id="run-overwrite-2",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.instance",
                subject_key=finding_key,
                assertion_level="observed",
                payload={
                    "source": "nuclei",
                    "detector_id": "cve-2024-0001",
                    "target_url": "https://example.com/login",
                    "severity": "high",
                    "classification": {
                        "cve_ids": ["CVE-2024-0001"],
                    },
                    "references": ["https://vendor.example/advisory-v2"],
                    "extracted_results": ["admin portal"],
                },
                observed_at=now + timedelta(minutes=5),
            )
        ]

        service.project_observations(engagement_id=engagement.id, observations=batch_one)
        service.project_observations(engagement_id=engagement.id, observations=batch_two)

        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()

        rich_details = dict((finding.finding_metadata or {}).get("rich_details") or {})

        # Overlapping keys: last-writer-wins overwrites at the top level
        assert rich_details["classification"] == {"cve_ids": ["CVE-2024-0001"]}, (
            "classification should be overwritten by batch 2 (cwe_ids lost — shallow last-writer-wins)"
        )
        assert rich_details["references"] == ["https://vendor.example/advisory-v2"], (
            "references should be overwritten by batch 2"
        )

        # Non-overlapping keys: batch 1 fields survive when absent from batch 2
        assert rich_details["tags"] == ["panel", "exposure"], (
            "tags from batch 1 must survive when batch 2 has no tags key"
        )

        # New keys from batch 2
        assert rich_details["extracted_results"] == ["admin portal"], (
            "extracted_results from batch 2 should appear"
        )
    finally:
        db.close()
        engine.dispose()


def test_relationship_projector_fallback_parses_colon_delimited_subject_keys() -> None:
    key = "relationship.edge:host.ip:10.0.0.5:exploits:host.ip:10.0.0.10"

    source, relation, target = RelationshipProjector._resolve_triplet(relationship_key=key, payload={})

    assert source == "host.ip:10.0.0.5"
    assert relation == "exploits"
    assert target == "host.ip:10.0.0.10"


def test_relationship_projector_fallback_uses_payload_fields_when_partially_present() -> None:
    key = "relationship.edge:host.ip:10.0.0.5:exploits:host.ip:10.0.0.10"
    payload = {"source_subject_key": "host.ip:10.0.0.5"}

    source, relation, target = RelationshipProjector._resolve_triplet(
        relationship_key=key,
        payload=payload,
    )

    assert source == "host.ip:10.0.0.5"
    assert relation == "exploits"
    assert target == "host.ip:10.0.0.10"


def test_projection_service_keeps_llm_candidate_findings_non_authoritative() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.vulnerability:host.ip:10.10.10.99:candidate-replay"
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-candidate-1",
                ingestion_run_id="run-candidate-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="candidate",
                payload={"title": "Low-authority candidate", "severity": "high"},
                observation_metadata={
                    "source_kind": "llm_candidate",
                    "extractor_family": "llm.candidate_extraction",
                    "extractor_version": "1.0",
                },
                observed_at=now,
            )
        ]

        result = service.project_observations(engagement_id=engagement.id, observations=observations)
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()

        assert result.finding_upsert_count == 1
        assert finding.status == "candidate"
        assert finding.assertion_level == "candidate"
        authority = dict((finding.finding_metadata or {}).get("authority") or {})
        assert authority.get("source_kind") == "llm_candidate"
        assert authority.get("candidate_only") is True
    finally:
        db.close()
        engine.dispose()


def test_projection_service_candidate_projection_uses_normalized_confidence_and_preserves_authority_metadata() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.vulnerability:host.ip:10.10.10.88:candidate-replay"
        db.add(
            KnowledgeFinding(
                tenant_id=engagement.tenant_id,
                user_id=engagement.user_id,
                engagement_id=engagement.id,
                finding_key=finding_key,
                finding_type="finding.vulnerability",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.88",
                first_seen_at=now - timedelta(minutes=10),
                last_seen_at=now - timedelta(minutes=5),
                confidence="low",
                finding_metadata={
                    "authority": {
                        "triage_state": "needs_review",
                    }
                },
            )
        )
        db.flush()
        observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-candidate-confidence-1",
                ingestion_run_id="run-candidate-confidence-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="candidate",
                payload={
                    "title": "Candidate projection with normalized confidence",
                    "severity": "medium",
                    "confidence": "HIGH",
                    "evidence_refs": [{"evidence_archive_id": "ev-cand-1", "excerpt": "candidate proof"}],
                },
                observation_metadata={
                    "source_kind": "llm_candidate",
                    "extractor_family": "llm.candidate_extraction",
                    "extractor_version": "1.0",
                },
                observed_at=now,
            )
        ]

        result = service.project_observations(engagement_id=engagement.id, observations=observations)
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()

        assert result.finding_upsert_count == 1
        assert result.finding_insert_count == 0
        assert finding.status == "candidate"
        assert finding.assertion_level == "candidate"
        assert finding.confidence == "high"
        authority = dict((finding.finding_metadata or {}).get("authority") or {})
        assert authority.get("source_kind") == "llm_candidate"
        assert authority.get("candidate_only") is True
        assert authority.get("triage_state") == "needs_review"
    finally:
        db.close()
        engine.dispose()


def test_projection_service_candidate_projection_maps_float_confidence_from_mapping_path() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        evidence_id = str(uuid_lib.uuid4())
        extraction_result = map_structured_payload(
            request=CandidateExtractionRequest(
                engagement_id=engagement.id,
                source_execution_id="exec-candidate-float-map-1",
                ingestion_run_id="run-candidate-float-map-1",
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0",
                extraction_mode="candidate_fallback",
                tool_name="shell.exec",
                capability_family="web_scan",
            ),
            user_id=engagement.user_id,
            payload={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.vulnerability",
                        "subject_key_hint": "host.ip:10.10.10.77:candidate-replay",
                        "confidence": 0.85,
                        "attributes": {"title": "Float confidence candidate"},
                        "rationale": "Float confidence from mapping path",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": evidence_id,
                                "excerpt": "candidate evidence",
                            }
                        ],
                    }
                ],
                "analyst_notes": [],
                "no_signal": False,
            },
            bounded_evidence=(
                {
                    "evidence_archive_id": evidence_id,
                },
            ),
            durable_masking_applied=False,
            usage_summary=CandidateExtractionUsageSummary(),
        )
        assert extraction_result.status == "succeeded"
        assert extraction_result.observations
        mapped_observation = extraction_result.observations[0]

        result = service.project_observations(
            engagement_id=engagement.id,
            observations=[mapped_observation],
        )
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == mapped_observation.subject_key,
        ).one()

        assert result.finding_upsert_count == 1
        assert finding.status == "candidate"
        assert finding.assertion_level == "candidate"
        assert finding.confidence == "high"
    finally:
        db.close()
        engine.dispose()


def test_candidate_mapping_masks_generic_durable_payloads_and_notes() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        evidence_id = str(uuid_lib.uuid4())
        raw_secret = "generic-candidate-secret-123"
        extraction_result = map_structured_payload(
            request=CandidateExtractionRequest(
                engagement_id=engagement.id,
                source_execution_id="exec-candidate-mask-1",
                ingestion_run_id="run-candidate-mask-1",
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0",
                extraction_mode="candidate_fallback",
                tool_name="shell.exec",
                capability_family="web_scan",
            ),
            user_id=engagement.user_id,
            payload={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.vulnerability",
                        "subject_key_hint": f"token={raw_secret}",
                        "confidence": 0.91,
                        "attributes": {
                            "title": "Credential exposure",
                            "authorization": f"Bearer {raw_secret}",
                        },
                        "vulnerability": {
                            "id": "CVE-2026-0001",
                            "title": f"password={raw_secret}",
                            "severity": "high",
                        },
                        "rationale": f"Observed password={raw_secret} in output",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": evidence_id,
                                "excerpt": f"Authorization: Bearer {raw_secret}",
                            }
                        ],
                    }
                ],
                "analyst_notes": [
                    {"note": f"Analyst note retained token={raw_secret}."}
                ],
                "no_signal": False,
            },
            bounded_evidence=(
                {
                    "evidence_archive_id": evidence_id,
                },
            ),
            durable_masking_applied=False,
            usage_summary=CandidateExtractionUsageSummary(),
        )

        assert extraction_result.status == "succeeded"
        assert extraction_result.observations
        durable_text = json.dumps(
            {
                "subject_key": extraction_result.observations[0].subject_key,
                "payload": extraction_result.observations[0].payload,
                "analyst_notes": extraction_result.analyst_notes,
            },
            sort_keys=True,
        )
        assert raw_secret not in durable_text
        assert "<DURABLE_SECRET_MASK:" in durable_text
        assert "Credential exposure" in durable_text
    finally:
        db.close()
        engine.dispose()


def test_projection_service_links_candidate_vulnerability_to_existing_service_from_canonicalized_key() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        deterministic_observations = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-link-base-1",
                ingestion_run_id="run-link-base-1",
                observation_type="network.host_discovered",
                subject_type="host.ip",
                subject_key="host.ip:10.10.10.5",
                assertion_level="observed",
                payload={"host_status": "up"},
                observed_at=now,
            ),
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-link-base-1",
                ingestion_run_id="run-link-base-1",
                observation_type="network.open_port",
                subject_type="service.socket",
                subject_key="service.socket:10.10.10.5/tcp/443",
                assertion_level="observed",
                payload={},
                observed_at=now + timedelta(seconds=1),
            ),
        ]
        service.project_observations(
            engagement_id=engagement.id,
            observations=deterministic_observations,
        )
        projected_service = db.query(KnowledgeService).filter(
            KnowledgeService.engagement_id == engagement.id,
            KnowledgeService.service_key == "service.socket:10.10.10.5/tcp/443",
        ).one()

        evidence_id = str(uuid_lib.uuid4())
        extraction_result = map_structured_payload(
            request=CandidateExtractionRequest(
                engagement_id=engagement.id,
                source_execution_id="exec-candidate-link-1",
                ingestion_run_id="run-candidate-link-1",
                extractor_family="llm.candidate_extraction",
                extractor_version="1.0",
                extraction_mode="candidate_fallback",
                tool_name="information_gathering.network_discovery.nmap",
                capability_family="network_scan",
            ),
            user_id=engagement.user_id,
            payload={
                "candidate_observations": [
                    {
                        "observation_type": "finding.vulnerability_detected",
                        "subject_type": "finding.instance",
                        "subject_key_hint": "cve-2024-9999:service.socket:10.10.10.5/tcp/443",
                        "vulnerability_confidence": 0.92,
                        "vulnerability": {
                            "id": "CVE-2024-9999",
                            "title": "Service candidate",
                            "severity": "high",
                        },
                        "attributes": {"title": "Service vulnerability candidate"},
                        "rationale": "Service banner suggests vulnerable version",
                        "evidence_refs": [
                            {
                                "evidence_archive_id": evidence_id,
                                "excerpt": "443/tcp open https nginx 1.14.0",
                            }
                        ],
                    }
                ],
                "analyst_notes": [],
                "no_signal": False,
            },
            bounded_evidence=(
                {
                    "evidence_archive_id": evidence_id,
                },
            ),
            durable_masking_applied=False,
            usage_summary=CandidateExtractionUsageSummary(),
        )
        assert extraction_result.status == "succeeded"
        mapped_observation = extraction_result.observations[0]
        assert mapped_observation.subject_type == "finding.vulnerability"
        assert mapped_observation.subject_key.startswith(
            "finding.vulnerability:service.socket:10.10.10.5/tcp/443:"
        )

        service.project_observations(
            engagement_id=engagement.id,
            observations=[mapped_observation],
        )
        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == mapped_observation.subject_key,
        ).one()
        assert finding.status == "candidate"
        assert finding.service_id == projected_service.id
        assert finding.asset_id == projected_service.asset_id
    finally:
        db.close()
        engine.dispose()


def test_projection_service_does_not_downgrade_candidate_confidence_across_runs() -> None:
    engine, db = _build_session()
    try:
        engagement = _seed_engagement(db)
        service = KnowledgeProjectionService(db)
        now = datetime.now(timezone.utc)
        finding_key = "finding.vulnerability:host.ip:10.10.10.66:candidate-replay"

        high_batch = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-candidate-high-1",
                ingestion_run_id="run-candidate-high-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="candidate",
                payload={"title": "High candidate", "confidence": "high"},
                observation_metadata={"source_kind": "llm_candidate"},
                observed_at=now,
            )
        ]
        service.project_observations(engagement_id=engagement.id, observations=high_batch)

        low_batch = [
            ObservationCreate(
                engagement_id=engagement.id,
                task_id=None,
                source_execution_id="exec-candidate-low-1",
                ingestion_run_id="run-candidate-low-1",
                observation_type="finding.vulnerability_detected",
                subject_type="finding.vulnerability",
                subject_key=finding_key,
                assertion_level="candidate",
                payload={"title": "Low candidate later", "confidence": "low"},
                observation_metadata={"source_kind": "llm_candidate"},
                observed_at=now + timedelta(minutes=5),
            )
        ]
        service.project_observations(engagement_id=engagement.id, observations=low_batch)

        finding = db.query(KnowledgeFinding).filter(
            KnowledgeFinding.engagement_id == engagement.id,
            KnowledgeFinding.finding_key == finding_key,
        ).one()
        assert finding.status == "candidate"
        assert finding.assertion_level == "candidate"
        assert finding.confidence == "high"
    finally:
        db.close()
        engine.dispose()
