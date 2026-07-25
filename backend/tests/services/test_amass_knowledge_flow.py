"""Persisted Amass knowledge-flow tests for ingestion and projector seams."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from unittest.mock import patch
import uuid as uuid_lib

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    EngagementAssetLink,
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.adapters.amass_adapter import AMASS_TOOL_ID
from backend.services.knowledge.identity.canonical_keys import build_finding_vulnerability_key
from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
from backend.services.knowledge.query_service import KnowledgeQueryService
from runtime_shared.semantic.canonical_keys import build_relationship_edge_key


NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"
DNS_KEY = "host.dns:api.example.com"
IP_KEY = "host.ip:192.0.2.20"
SERVICE_KEY = "service.socket:192.0.2.20/tcp/443"
FINDING_DETECTOR_ID = "nmap/ssl-cert-weak-signature"
FINDING_KEY = build_finding_vulnerability_key(
    subject_key=SERVICE_KEY,
    detector_id=FINDING_DETECTOR_ID,
)
RELATIONSHIP_TYPE = "resolves_to"
RELATIONSHIP_KEY = build_relationship_edge_key(
    source_subject_key=DNS_KEY,
    relationship_type=RELATIONSHIP_TYPE,
    target_subject_key=IP_KEY,
)


def _ensure_tenant(db, *, tenant_id: int) -> None:
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (:id, :slug, :name, CURRENT_TIMESTAMP)"
        ),
        {
            "id": int(tenant_id),
            "slug": f"tenant-{tenant_id}",
            "name": f"Tenant {tenant_id}",
        },
    )


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_user_engagement_task(db, *, tenant_id: int = 221):
    _ensure_tenant(db, tenant_id=tenant_id)
    user = User(username=f"amass-flow-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement, task = _seed_engagement_task_for_user(
        db,
        user=user,
        tenant_id=tenant_id,
        name="Amass Knowledge Flow",
    )
    return user, engagement, task


def _seed_engagement_task_for_user(db, *, user: User, tenant_id: int, name: str):
    _ensure_tenant(db, tenant_id=tenant_id)
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant_id,
        name=name,
        status="active",
    )
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        engagement_id=engagement.id,
        tenant_id=tenant_id,
        name=f"{name} Task",
    )
    db.add(task)
    db.flush()
    return engagement, task


def _semantic_observations() -> list[dict]:
    return [
        {
            "observation_type": "dns.name_discovered",
            "subject_type": "host.dns",
            "subject_key": DNS_KEY,
            "payload": {
                "tool_source": "amass",
                "dns_name": "api.example.com",
                "confidence": "medium",
            },
        },
        {
            "observation_type": "dns.address_resolved",
            "subject_type": "host.ip",
            "subject_key": IP_KEY,
            "payload": {
                "tool_source": "amass",
                "address": "192.0.2.20",
                "record_type": "A",
                "confidence": "medium",
            },
        },
        {
            "observation_type": "relationship.resolves_to",
            "subject_type": "relationship.edge",
            "subject_key": RELATIONSHIP_KEY,
            "payload": {
                "source_subject_type": "host.dns",
                "source_subject_key": DNS_KEY,
                "relationship_type": RELATIONSHIP_TYPE,
                "target_subject_type": "host.ip",
                "target_subject_key": IP_KEY,
                "record_type": "A",
                "tool_source": "amass",
                "confidence": "medium",
            },
        },
    ]


def _nmap_semantic_observations() -> list[dict]:
    return [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": IP_KEY,
            "payload": {
                "source": "nmap",
                "host_status": "up",
                "confidence": "medium",
            },
        }
    ]


def _nmap_service_finding_semantic_observations() -> list[dict]:
    rows = _nmap_semantic_observations()
    rows.extend(
        [
            {
                "observation_type": "network.open_port",
                "subject_type": "service.socket",
                "subject_key": SERVICE_KEY,
                "payload": {
                    "source": "nmap",
                    "ip": "192.0.2.20",
                    "protocol": "tcp",
                    "port": 443,
                    "confidence": "medium",
                },
            },
            {
                "observation_type": "network.service_detected",
                "subject_type": "service.socket",
                "subject_key": SERVICE_KEY,
                "payload": {
                    "source": "nmap",
                    "service_name": "https",
                    "product": "nginx",
                    "version": "1.24.0",
                    "confidence": "medium",
                },
            },
            {
                "observation_type": "finding.vulnerability_detected",
                "subject_type": "finding.vulnerability",
                "subject_key": FINDING_KEY,
                "payload": {
                    "detector_id": FINDING_DETECTOR_ID,
                    "script_id": "ssl-cert",
                    "summary": "TLS certificate uses a weak signature algorithm",
                    "subject_key": SERVICE_KEY,
                    "severity": "medium",
                    "title": "Weak TLS certificate signature",
                    "confidence": "medium",
                },
            },
        ]
    )
    return rows


def _seed_semantic_execution(
    db,
    *,
    task: Task,
    tool_name: str,
    capability_family: str,
    semantic_observations: list[dict],
    artifact_text: str,
    relative_path: str,
) -> str:
    execution_id = uuid_lib.uuid4()
    execution = ToolExecution(
        id=execution_id,
        tenant_id=int(task.tenant_id),
        task_id=int(task.id),
        tool_name=tool_name,
        tool_arguments={"target": "example.com"},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        execution_metadata={
            "capability_family": capability_family,
            "tool_metadata": {},
            "semantic_observations": semantic_observations,
        },
    )
    db.add(execution)
    db.flush()

    encoded = artifact_text.encode("utf-8")
    db.add(
        ExecutionArtifact(
            id=uuid_lib.uuid4(),
            execution_id=execution_id,
            tenant_id=int(task.tenant_id),
            task_id=int(task.id),
            artifact_kind="stdout",
            relative_path=relative_path,
            content_text=artifact_text,
            content_sha256=sha256(encoded).hexdigest(),
            byte_size=len(encoded),
            mime_type="application/json",
            is_text=True,
        )
    )
    db.flush()
    return str(execution_id)


def _seed_amass_execution(db, *, task: Task, artifact_text: str) -> str:
    return _seed_semantic_execution(
        db,
        task=task,
        tool_name=AMASS_TOOL_ID,
        capability_family="dns_enumeration",
        semantic_observations=_semantic_observations(),
        artifact_text=artifact_text,
        relative_path="amass.json",
    )


def _seed_nmap_execution(db, *, task: Task, artifact_text: str) -> str:
    return _seed_semantic_execution(
        db,
        task=task,
        tool_name=NMAP_TOOL_ID,
        capability_family="network_discovery",
        semantic_observations=_nmap_semantic_observations(),
        artifact_text=artifact_text,
        relative_path="nmap.json",
    )


def _seed_nmap_service_finding_execution(db, *, task: Task, artifact_text: str) -> str:
    return _seed_semantic_execution(
        db,
        task=task,
        tool_name=NMAP_TOOL_ID,
        capability_family="network_discovery",
        semantic_observations=_nmap_service_finding_semantic_observations(),
        artifact_text=artifact_text,
        relative_path="nmap-service-finding.json",
    )


@contextmanager
def _observation_clock(at: datetime):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return at.replace(tzinfo=None)
            return at.astimezone(tz)

    with patch("backend.services.knowledge.contracts.datetime", FixedDatetime):
        yield


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _evidence_ids(refs: list[dict]) -> set[str]:
    return {str(item["evidence_archive_id"]) for item in refs}


def _archive_ids_for_executions(db, execution_ids: set[str]) -> set[str]:
    return {
        str(row.id)
        for row in db.query(KnowledgeEvidenceArchive)
        .filter(KnowledgeEvidenceArchive.source_execution_id.in_(sorted(execution_ids)))
        .all()
    }


def _asset_by_key(db, *, tenant_id: int, user_id: int, asset_key: str) -> KnowledgeAsset:
    return (
        db.query(KnowledgeAsset)
        .filter(
            KnowledgeAsset.tenant_id == int(tenant_id),
            KnowledgeAsset.user_id == int(user_id),
            KnowledgeAsset.asset_key == asset_key,
        )
        .one()
    )


def _relationship_by_key(
    db,
    *,
    tenant_id: int,
    user_id: int,
    relationship_key: str,
) -> KnowledgeRelationship:
    return (
        db.query(KnowledgeRelationship)
        .filter(
            KnowledgeRelationship.tenant_id == int(tenant_id),
            KnowledgeRelationship.user_id == int(user_id),
            KnowledgeRelationship.relationship_key == relationship_key,
        )
        .one()
    )


def _service_by_key(db, *, tenant_id: int, user_id: int, service_key: str) -> KnowledgeService:
    return (
        db.query(KnowledgeService)
        .filter(
            KnowledgeService.tenant_id == int(tenant_id),
            KnowledgeService.user_id == int(user_id),
            KnowledgeService.service_key == service_key,
        )
        .one()
    )


def _finding_by_key(db, *, tenant_id: int, user_id: int, finding_key: str) -> KnowledgeFinding:
    return (
        db.query(KnowledgeFinding)
        .filter(
            KnowledgeFinding.tenant_id == int(tenant_id),
            KnowledgeFinding.user_id == int(user_id),
            KnowledgeFinding.finding_key == finding_key,
        )
        .one()
    )


def test_amass_persists_multi_mapping_ipv6_and_unresolved_dns_projection() -> None:
    engine, db = _build_session()
    seen_at = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    shared_ip_key = "host.ip:192.0.2.20"
    ipv6_key = "host.ip:2001:db8::5"
    api_dns_key = "host.dns:api.example.com"
    www_dns_key = "host.dns:www.example.com"
    unresolved_dns_key = "host.dns:unresolved.example.com"
    api_ipv4_relationship_key = build_relationship_edge_key(
        source_subject_key=api_dns_key,
        relationship_type=RELATIONSHIP_TYPE,
        target_subject_key=shared_ip_key,
    )
    api_ipv6_relationship_key = build_relationship_edge_key(
        source_subject_key=api_dns_key,
        relationship_type=RELATIONSHIP_TYPE,
        target_subject_key=ipv6_key,
    )
    www_ipv4_relationship_key = build_relationship_edge_key(
        source_subject_key=www_dns_key,
        relationship_type=RELATIONSHIP_TYPE,
        target_subject_key=shared_ip_key,
    )
    try:
        user, engagement, task = _seed_user_engagement_task(db, tenant_id=225)
        semantic_observations = [
            {
                "observation_type": "dns.name_discovered",
                "subject_type": "host.dns",
                "subject_key": api_dns_key,
                "payload": {
                    "tool_source": "amass",
                    "dns_name": "api.example.com",
                    "resolved_address_count": 2,
                },
            },
            {
                "observation_type": "dns.name_discovered",
                "subject_type": "host.dns",
                "subject_key": www_dns_key,
                "payload": {
                    "tool_source": "amass",
                    "dns_name": "www.example.com",
                    "resolved_address_count": 1,
                },
            },
            {
                "observation_type": "dns.name_discovered",
                "subject_type": "host.dns",
                "subject_key": unresolved_dns_key,
                "payload": {
                    "tool_source": "amass",
                    "dns_name": "unresolved.example.com",
                    "resolved_address_count": 0,
                },
            },
            {
                "observation_type": "dns.address_resolved",
                "subject_type": "host.ip",
                "subject_key": shared_ip_key,
                "payload": {
                    "tool_source": "amass",
                    "address": "192.0.2.20",
                    "record_type": "A",
                },
            },
            {
                "observation_type": "dns.address_resolved",
                "subject_type": "host.ip",
                "subject_key": ipv6_key,
                "payload": {
                    "tool_source": "amass",
                    "address": "2001:db8::5",
                    "record_type": "AAAA",
                },
            },
            {
                "observation_type": "relationship.resolves_to",
                "subject_type": "relationship.edge",
                "subject_key": api_ipv4_relationship_key,
                "payload": {
                    "source_subject_type": "host.dns",
                    "source_subject_key": api_dns_key,
                    "relationship_type": RELATIONSHIP_TYPE,
                    "target_subject_type": "host.ip",
                    "target_subject_key": shared_ip_key,
                    "record_type": "A",
                    "tool_source": "amass",
                },
            },
            {
                "observation_type": "relationship.resolves_to",
                "subject_type": "relationship.edge",
                "subject_key": api_ipv6_relationship_key,
                "payload": {
                    "source_subject_type": "host.dns",
                    "source_subject_key": api_dns_key,
                    "relationship_type": RELATIONSHIP_TYPE,
                    "target_subject_type": "host.ip",
                    "target_subject_key": ipv6_key,
                    "record_type": "AAAA",
                    "tool_source": "amass",
                },
            },
            {
                "observation_type": "relationship.resolves_to",
                "subject_type": "relationship.edge",
                "subject_key": www_ipv4_relationship_key,
                "payload": {
                    "source_subject_type": "host.dns",
                    "source_subject_key": www_dns_key,
                    "relationship_type": RELATIONSHIP_TYPE,
                    "target_subject_type": "host.ip",
                    "target_subject_key": shared_ip_key,
                    "record_type": "A",
                    "tool_source": "amass",
                },
            },
        ]
        execution_id = _seed_semantic_execution(
            db,
            task=task,
            tool_name=AMASS_TOOL_ID,
            capability_family="dns_enumeration",
            semantic_observations=semantic_observations,
            artifact_text=(
                '{"tool":"amass","subdomains":["api.example.com",'
                '"www.example.com","unresolved.example.com"]}'
            ),
            relative_path="amass-mapping.json",
        )
        service = KnowledgeIngestionService(db)

        with _observation_clock(seen_at):
            result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=execution_id,
                raise_on_error=True,
            )

        assert result["ok"] is True
        assert result["observation_inserted_count"] == len(semantic_observations)
        assert result["asset_insert_count"] == 5
        assert result["relationship_insert_count"] == 3

        archive_ids = _archive_ids_for_executions(db, {execution_id})
        assert len(archive_ids) == 1
        expected_asset_keys = {
            api_dns_key,
            www_dns_key,
            unresolved_dns_key,
            shared_ip_key,
            ipv6_key,
        }
        expected_relationship_keys = {
            api_ipv4_relationship_key,
            api_ipv6_relationship_key,
            www_ipv4_relationship_key,
        }

        query = KnowledgeQueryService(db)
        asset_page = query.list_assets(
            user_id=int(user.id),
            tenant_id=int(engagement.tenant_id),
            engagement_id=int(engagement.id),
        )
        assert asset_page["total"] == len(expected_asset_keys)
        assert {str(item["asset_key"]) for item in asset_page["items"]} == expected_asset_keys

        persisted_assets = (
            db.query(KnowledgeAsset)
            .filter(
                KnowledgeAsset.tenant_id == engagement.tenant_id,
                KnowledgeAsset.user_id == user.id,
                KnowledgeAsset.asset_key.in_(sorted(expected_asset_keys)),
            )
            .all()
        )
        assert len(persisted_assets) == len(expected_asset_keys)
        assert {asset.asset_key for asset in persisted_assets} == expected_asset_keys
        for asset in persisted_assets:
            assert _evidence_ids(asset.asset_metadata["evidence_refs"]) == archive_ids
            assert _as_utc_naive(asset.first_seen_at) == seen_at.replace(tzinfo=None)
            assert _as_utc_naive(asset.last_seen_at) == seen_at.replace(tzinfo=None)
        assert _asset_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            asset_key=unresolved_dns_key,
        ).hostname == "unresolved.example.com"

        persisted_relationships = (
            db.query(KnowledgeRelationship)
            .filter(
                KnowledgeRelationship.tenant_id == engagement.tenant_id,
                KnowledgeRelationship.user_id == user.id,
            )
            .all()
        )
        assert {relationship.relationship_key for relationship in persisted_relationships} == (
            expected_relationship_keys
        )
        for relationship in persisted_relationships:
            assert relationship.relationship_type == RELATIONSHIP_TYPE
            assert _evidence_ids(relationship.relationship_metadata["evidence_refs"]) == archive_ids
            assert (
                relationship.relationship_metadata["state"]["relationship_type"]
                == RELATIONSHIP_TYPE
            )

        assert db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.tenant_id == engagement.tenant_id,
            KnowledgeRelationship.user_id == user.id,
            KnowledgeRelationship.source_subject_key == unresolved_dns_key,
        ).count() == 0
        assert db.query(EngagementAssetLink).filter(
            EngagementAssetLink.engagement_id == engagement.id
        ).count() == len(expected_asset_keys)

        graph = query.get_graph_snapshot(
            user_id=int(user.id),
            tenant_id=int(engagement.tenant_id),
            engagement_id=int(engagement.id),
        )
        edge_triplets = {
            (str(edge["source"]), str(edge["relationship_type"]), str(edge["target"]))
            for edge in graph["edges"]
        }
        assert edge_triplets == {
            (api_dns_key, RELATIONSHIP_TYPE, shared_ip_key),
            (api_dns_key, RELATIONSHIP_TYPE, ipv6_key),
            (www_dns_key, RELATIONSHIP_TYPE, shared_ip_key),
        }
        assert unresolved_dns_key in {str(node["id"]) for node in graph["nodes"]}
    finally:
        db.close()
        engine.dispose()


def test_amass_ingestion_projects_dns_ip_resolves_to_evidence_and_engagement_links() -> None:
    engine, db = _build_session()
    first_seen = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    second_seen = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
    try:
        user, engagement, task = _seed_user_engagement_task(db)
        first_execution_id = _seed_amass_execution(
            db,
            task=task,
            artifact_text='{"subdomain":"api.example.com","ip":"192.0.2.20","run":1}',
        )
        second_execution_id = _seed_amass_execution(
            db,
            task=task,
            artifact_text='{"subdomain":"api.example.com","ip":"192.0.2.20","run":2}',
        )
        service = KnowledgeIngestionService(db)

        with _observation_clock(first_seen):
            first_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=first_execution_id,
                raise_on_error=True,
            )
        with _observation_clock(second_seen):
            second_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=second_execution_id,
                raise_on_error=True,
            )

        assert first_result["ok"] is True
        assert first_result["observation_inserted_count"] == 3
        assert first_result["asset_insert_count"] == 2
        assert first_result["relationship_insert_count"] == 1
        assert second_result["ok"] is True
        assert second_result["observation_inserted_count"] == 3
        assert second_result["asset_insert_count"] == 0
        assert second_result["relationship_insert_count"] == 0

        archive_ids = {
            str(row.id)
            for row in db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.engagement_id == engagement.id)
            .all()
        }
        assert len(archive_ids) == 2

        dns_asset = (
            db.query(KnowledgeAsset)
            .filter(
                KnowledgeAsset.tenant_id == engagement.tenant_id,
                KnowledgeAsset.user_id == user.id,
                KnowledgeAsset.asset_key == DNS_KEY,
            )
            .one()
        )
        ip_asset = (
            db.query(KnowledgeAsset)
            .filter(
                KnowledgeAsset.tenant_id == engagement.tenant_id,
                KnowledgeAsset.user_id == user.id,
                KnowledgeAsset.asset_key == IP_KEY,
            )
            .one()
        )
        relationship = (
            db.query(KnowledgeRelationship)
            .filter(
                KnowledgeRelationship.tenant_id == engagement.tenant_id,
                KnowledgeRelationship.user_id == user.id,
                KnowledgeRelationship.relationship_key == RELATIONSHIP_KEY,
            )
            .one()
        )

        asset_count = (
            db.query(KnowledgeAsset)
            .filter(KnowledgeAsset.engagement_id == engagement.id)
            .count()
        )
        assert asset_count == 2
        assert db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.engagement_id == engagement.id
        ).count() == 1

        assert dns_asset.asset_type == "host.dns"
        assert dns_asset.hostname == "api.example.com"
        assert ip_asset.asset_type == "host.ip"
        assert ip_asset.ip_address == "192.0.2.20"
        assert _as_utc_naive(dns_asset.first_seen_at) == first_seen.replace(tzinfo=None)
        assert _as_utc_naive(dns_asset.last_seen_at) == second_seen.replace(tzinfo=None)
        assert _as_utc_naive(ip_asset.first_seen_at) == first_seen.replace(tzinfo=None)
        assert _as_utc_naive(ip_asset.last_seen_at) == second_seen.replace(tzinfo=None)
        assert _evidence_ids(dns_asset.asset_metadata["evidence_refs"]) == archive_ids
        assert _evidence_ids(ip_asset.asset_metadata["evidence_refs"]) == archive_ids

        assert relationship.source_subject_key == DNS_KEY
        assert relationship.relationship_type == RELATIONSHIP_TYPE
        assert relationship.target_subject_key == IP_KEY
        assert _as_utc_naive(relationship.first_seen_at) == first_seen.replace(tzinfo=None)
        assert _as_utc_naive(relationship.last_seen_at) == second_seen.replace(tzinfo=None)
        assert _evidence_ids(relationship.relationship_metadata["evidence_refs"]) == archive_ids
        assert relationship.relationship_metadata["state"]["relationship_type"] == RELATIONSHIP_TYPE

        relationship_observations = (
            db.query(KnowledgeObservation)
            .filter(
                KnowledgeObservation.engagement_id == engagement.id,
                KnowledgeObservation.subject_key == RELATIONSHIP_KEY,
            )
            .order_by(KnowledgeObservation.observed_at.asc())
            .all()
        )
        assert len(relationship_observations) == 2
        for observation in relationship_observations:
            assert observation.observation_type == "relationship.resolves_to"
            assert observation.payload["source_subject_key"] == DNS_KEY
            assert observation.payload["relationship_type"] == RELATIONSHIP_TYPE
            assert observation.payload["target_subject_key"] == IP_KEY
            assert _evidence_ids(observation.payload["evidence_refs"]) <= archive_ids

        links = (
            db.query(EngagementAssetLink)
            .filter(EngagementAssetLink.engagement_id == engagement.id)
            .all()
        )
        assert len(links) == 2
        assert {str(link.asset_id) for link in links} == {str(dns_asset.id), str(ip_asset.id)}
        for link in links:
            assert link.tenant_id == engagement.tenant_id
            assert _as_utc_naive(link.first_seen_in_engagement) == first_seen.replace(tzinfo=None)
            assert _as_utc_naive(link.last_seen_in_engagement) == second_seen.replace(tzinfo=None)
    finally:
        db.close()
        engine.dispose()


def test_amass_and_nmap_share_canonical_ip_with_evidence_and_scope_isolation() -> None:
    engine, db = _build_session()
    nmap_seen = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
    amass_seen = datetime(2026, 1, 2, 9, 5, tzinfo=timezone.utc)
    repeated_amass_seen = datetime(2026, 1, 2, 9, 10, tzinfo=timezone.utc)
    second_engagement_seen = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)
    tenant_b_seen = datetime(2026, 1, 2, 9, 20, tzinfo=timezone.utc)
    try:
        user, engagement, task = _seed_user_engagement_task(db, tenant_id=231)
        nmap_execution_id = _seed_nmap_execution(
            db,
            task=task,
            artifact_text='{"tool":"nmap","ip":"192.0.2.20"}',
        )
        amass_execution_id = _seed_amass_execution(
            db,
            task=task,
            artifact_text='{"tool":"amass","subdomain":"api.example.com","ip":"192.0.2.20"}',
        )
        repeated_amass_execution_id = _seed_amass_execution(
            db,
            task=task,
            artifact_text='{"tool":"amass","subdomain":"api.example.com","ip":"192.0.2.20","run":2}',
        )
        service = KnowledgeIngestionService(db)

        with _observation_clock(nmap_seen):
            nmap_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=nmap_execution_id,
                raise_on_error=True,
            )
        with _observation_clock(amass_seen):
            amass_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=amass_execution_id,
                raise_on_error=True,
            )
        with _observation_clock(repeated_amass_seen):
            repeated_amass_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=repeated_amass_execution_id,
                raise_on_error=True,
            )
            repeated_same_run = service.ingest_execution(
                task_id=task.id,
                source_execution_id=repeated_amass_execution_id,
                raise_on_error=True,
            )

        assert nmap_result["ok"] is True
        assert nmap_result["asset_insert_count"] == 1
        assert amass_result["ok"] is True
        assert amass_result["asset_insert_count"] == 1
        assert amass_result["relationship_insert_count"] == 1
        assert repeated_amass_result["ok"] is True
        assert repeated_amass_result["asset_insert_count"] == 0
        assert repeated_amass_result["relationship_insert_count"] == 0
        assert repeated_same_run["ingestion_run_id"] == repeated_amass_result["ingestion_run_id"]
        assert repeated_same_run["observation_inserted_count"] == 0

        tenant_user_assets = db.query(KnowledgeAsset).filter(
            KnowledgeAsset.tenant_id == engagement.tenant_id,
            KnowledgeAsset.user_id == user.id,
        )
        assert tenant_user_assets.filter(KnowledgeAsset.asset_key == IP_KEY).count() == 1
        assert tenant_user_assets.filter(KnowledgeAsset.asset_key == DNS_KEY).count() == 1
        assert db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.tenant_id == engagement.tenant_id,
            KnowledgeRelationship.user_id == user.id,
            KnowledgeRelationship.relationship_key == RELATIONSHIP_KEY,
        ).count() == 1

        first_scope_execution_ids = {
            nmap_execution_id,
            amass_execution_id,
            repeated_amass_execution_id,
        }
        first_scope_archive_ids = _archive_ids_for_executions(db, first_scope_execution_ids)
        assert len(first_scope_archive_ids) == 3

        ip_asset = _asset_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            asset_key=IP_KEY,
        )
        dns_asset = _asset_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            asset_key=DNS_KEY,
        )
        relationship = _relationship_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            relationship_key=RELATIONSHIP_KEY,
        )

        assert _evidence_ids(ip_asset.asset_metadata["evidence_refs"]) == first_scope_archive_ids
        assert _evidence_ids(dns_asset.asset_metadata["evidence_refs"]) == (
            first_scope_archive_ids - _archive_ids_for_executions(db, {nmap_execution_id})
        )
        assert _evidence_ids(relationship.relationship_metadata["evidence_refs"]) == (
            first_scope_archive_ids - _archive_ids_for_executions(db, {nmap_execution_id})
        )

        ip_observations = (
            db.query(KnowledgeObservation)
            .filter(
                KnowledgeObservation.tenant_id == engagement.tenant_id,
                KnowledgeObservation.user_id == user.id,
                KnowledgeObservation.subject_key == IP_KEY,
            )
            .all()
        )
        assert {row.observation_type for row in ip_observations} == {
            "network.host_discovered",
            "dns.address_resolved",
        }
        assert any(row.payload.get("source") == "nmap" for row in ip_observations)
        assert any(row.payload.get("tool_source") == "amass" for row in ip_observations)

        second_engagement, second_task = _seed_engagement_task_for_user(
            db,
            user=user,
            tenant_id=int(engagement.tenant_id),
            name="Amass Knowledge Flow Second Engagement",
        )
        second_engagement_amass_execution_id = _seed_amass_execution(
            db,
            task=second_task,
            artifact_text='{"tool":"amass","subdomain":"api.example.com","ip":"192.0.2.20","engagement":2}',
        )
        with _observation_clock(second_engagement_seen):
            second_engagement_result = service.ingest_execution(
                task_id=second_task.id,
                source_execution_id=second_engagement_amass_execution_id,
                raise_on_error=True,
            )

        assert second_engagement_result["ok"] is True
        assert second_engagement_result["asset_insert_count"] == 0
        assert second_engagement_result["relationship_insert_count"] == 0
        assert tenant_user_assets.filter(KnowledgeAsset.asset_key == IP_KEY).count() == 1
        assert tenant_user_assets.filter(KnowledgeAsset.asset_key == DNS_KEY).count() == 1

        links = db.query(EngagementAssetLink).filter(
            EngagementAssetLink.tenant_id == engagement.tenant_id,
            EngagementAssetLink.asset_id.in_([ip_asset.id, dns_asset.id]),
        )
        assert {
            (int(link.engagement_id), str(link.asset_id))
            for link in links
        } == {
            (engagement.id, str(ip_asset.id)),
            (engagement.id, str(dns_asset.id)),
            (second_engagement.id, str(ip_asset.id)),
            (second_engagement.id, str(dns_asset.id)),
        }

        other_user, other_engagement, other_task = _seed_user_engagement_task(db, tenant_id=232)
        tenant_b_nmap_execution_id = _seed_nmap_execution(
            db,
            task=other_task,
            artifact_text='{"tool":"nmap","ip":"192.0.2.20","tenant":"b"}',
        )
        tenant_b_amass_execution_id = _seed_amass_execution(
            db,
            task=other_task,
            artifact_text='{"tool":"amass","subdomain":"api.example.com","ip":"192.0.2.20","tenant":"b"}',
        )
        with _observation_clock(tenant_b_seen):
            tenant_b_nmap_result = service.ingest_execution(
                task_id=other_task.id,
                source_execution_id=tenant_b_nmap_execution_id,
                raise_on_error=True,
            )
            tenant_b_amass_result = service.ingest_execution(
                task_id=other_task.id,
                source_execution_id=tenant_b_amass_execution_id,
                raise_on_error=True,
            )

        assert tenant_b_nmap_result["asset_insert_count"] == 1
        assert tenant_b_amass_result["asset_insert_count"] == 1
        assert tenant_b_amass_result["relationship_insert_count"] == 1

        same_ip_rows = db.query(KnowledgeAsset).filter(KnowledgeAsset.asset_key == IP_KEY).all()
        assert len(same_ip_rows) == 2
        assert {(int(row.tenant_id), int(row.user_id)) for row in same_ip_rows} == {
            (int(engagement.tenant_id), int(user.id)),
            (int(other_engagement.tenant_id), int(other_user.id)),
        }
        same_relationship_rows = db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.relationship_key == RELATIONSHIP_KEY
        ).all()
        assert len(same_relationship_rows) == 2
        assert {
            (int(row.tenant_id), int(row.user_id))
            for row in same_relationship_rows
        } == {
            (int(engagement.tenant_id), int(user.id)),
            (int(other_engagement.tenant_id), int(other_user.id)),
        }
    finally:
        db.close()
        engine.dispose()


def test_amass_relationships_compose_through_engagement_graph_service() -> None:
    engine, db = _build_session()
    nmap_seen = datetime(2026, 1, 3, 10, 0, tzinfo=timezone.utc)
    amass_seen = datetime(2026, 1, 3, 10, 5, tzinfo=timezone.utc)
    unrelated_seen = datetime(2026, 1, 3, 10, 10, tzinfo=timezone.utc)
    unrelated_dns_key = "host.dns:unrelated.example.com"
    unrelated_ip_key = "host.ip:198.51.100.44"
    unrelated_relationship_key = build_relationship_edge_key(
        source_subject_key=unrelated_dns_key,
        relationship_type=RELATIONSHIP_TYPE,
        target_subject_key=unrelated_ip_key,
    )
    try:
        user, engagement, task = _seed_user_engagement_task(db, tenant_id=241)
        nmap_execution_id = _seed_nmap_service_finding_execution(
            db,
            task=task,
            artifact_text='{"tool":"nmap","ip":"192.0.2.20","port":443,"finding":"weak-cert"}',
        )
        amass_execution_id = _seed_amass_execution(
            db,
            task=task,
            artifact_text='{"tool":"amass","subdomain":"api.example.com","ip":"192.0.2.20"}',
        )
        service = KnowledgeIngestionService(db)

        with _observation_clock(nmap_seen):
            nmap_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=nmap_execution_id,
                raise_on_error=True,
            )
        with _observation_clock(amass_seen):
            amass_result = service.ingest_execution(
                task_id=task.id,
                source_execution_id=amass_execution_id,
                raise_on_error=True,
            )

        assert nmap_result["ok"] is True
        assert nmap_result["asset_insert_count"] == 1
        assert nmap_result["service_insert_count"] == 1
        assert nmap_result["finding_insert_count"] == 1
        assert amass_result["ok"] is True
        assert amass_result["asset_insert_count"] == 1
        assert amass_result["relationship_insert_count"] == 1

        unrelated_engagement, unrelated_task = _seed_engagement_task_for_user(
            db,
            user=user,
            tenant_id=int(engagement.tenant_id),
            name="Unrelated Relationship Graph Engagement",
        )
        unrelated_execution_id = _seed_semantic_execution(
            db,
            task=unrelated_task,
            tool_name=AMASS_TOOL_ID,
            capability_family="dns_enumeration",
            semantic_observations=[
                {
                    "observation_type": "dns.name_discovered",
                    "subject_type": "host.dns",
                    "subject_key": unrelated_dns_key,
                    "payload": {
                        "tool_source": "amass",
                        "dns_name": "unrelated.example.com",
                    },
                },
                {
                    "observation_type": "dns.address_resolved",
                    "subject_type": "host.ip",
                    "subject_key": unrelated_ip_key,
                    "payload": {
                        "tool_source": "amass",
                        "address": "198.51.100.44",
                    },
                },
                {
                    "observation_type": "relationship.resolves_to",
                    "subject_type": "relationship.edge",
                    "subject_key": unrelated_relationship_key,
                    "payload": {
                        "source_subject_type": "host.dns",
                        "source_subject_key": unrelated_dns_key,
                        "relationship_type": RELATIONSHIP_TYPE,
                        "target_subject_type": "host.ip",
                        "target_subject_key": unrelated_ip_key,
                        "tool_source": "amass",
                    },
                },
            ],
            artifact_text='{"tool":"amass","subdomain":"unrelated.example.com","ip":"198.51.100.44"}',
            relative_path="amass-unrelated.json",
        )
        with _observation_clock(unrelated_seen):
            unrelated_result = service.ingest_execution(
                task_id=unrelated_task.id,
                source_execution_id=unrelated_execution_id,
                raise_on_error=True,
            )
        assert unrelated_result["ok"] is True
        assert unrelated_engagement.id != engagement.id

        ip_asset = _asset_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            asset_key=IP_KEY,
        )
        service_row = _service_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            service_key=SERVICE_KEY,
        )
        finding = _finding_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            finding_key=FINDING_KEY,
        )
        relationship = _relationship_by_key(
            db,
            tenant_id=int(engagement.tenant_id),
            user_id=int(user.id),
            relationship_key=RELATIONSHIP_KEY,
        )

        assert service_row.asset_id == ip_asset.id
        assert finding.service_id == service_row.id
        assert relationship.source_subject_key == DNS_KEY
        assert relationship.target_subject_key == IP_KEY

        graph = KnowledgeQueryService(db).get_graph_snapshot(
            user_id=int(user.id),
            tenant_id=int(engagement.tenant_id),
            engagement_id=int(engagement.id),
        )
        nodes = {str(node["id"]): node for node in graph["nodes"]}
        edges = list(graph["edges"])
        edge_triplets = [
            (
                str(edge["source"]),
                str(edge["relationship_type"]),
                str(edge["target"]),
            )
            for edge in edges
        ]

        assert set(nodes) == {DNS_KEY, IP_KEY, SERVICE_KEY, FINDING_KEY}
        assert len(edge_triplets) == len(set(edge_triplets))
        assert (DNS_KEY, RELATIONSHIP_TYPE, IP_KEY) in edge_triplets
        assert (IP_KEY, "exposes", SERVICE_KEY) in edge_triplets
        assert (SERVICE_KEY, "has_finding", FINDING_KEY) in edge_triplets
        assert all(str(node["label"]).strip() for node in nodes.values())
        assert nodes[DNS_KEY]["label"] == "api.example.com"
        assert nodes[IP_KEY]["label"] == "192.0.2.20"
        assert nodes[SERVICE_KEY]["label"] == "https"
        assert nodes[FINDING_KEY]["label"] == "Weak TLS certificate signature"
        assert all(str(node["node_type"]) != "unknown" for node in nodes.values())
        assert unrelated_dns_key not in nodes
        assert unrelated_ip_key not in nodes
        assert unrelated_relationship_key not in {str(edge["id"]) for edge in edges}

        resolves_to_edge = next(
            edge
            for edge in edges
            if (
                edge["source"],
                edge["relationship_type"],
                edge["target"],
            )
            == (DNS_KEY, RELATIONSHIP_TYPE, IP_KEY)
        )
        assert resolves_to_edge["relationship_type"] == RELATIONSHIP_TYPE
        assert resolves_to_edge["metadata"]["state"]["relationship_type"] == RELATIONSHIP_TYPE
        assert resolves_to_edge["metadata"]["source_observation_types"] == [
            "relationship.resolves_to"
        ]
        assert resolves_to_edge["metadata"]["evidence_refs"]
        synthetic_edges = [edge for edge in edges if str(edge["id"]).startswith("synth:")]
        assert all(edge["metadata"] == {"synthetic": True, "source": "fk_projection"} for edge in synthetic_edges)
    finally:
        db.close()
        engine.dispose()
