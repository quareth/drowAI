"""Tests for knowledge query filter and pagination normalization contracts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import (
    EngagementAssetLink,
    KnowledgeAsset,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeRelationship,
    KnowledgeService,
)
from backend.services.knowledge.query.contracts import WebSurfacePathsFilters
from backend.services.knowledge.query_service import (
    AssetsFilters,
    EngagementListFilters,
    EvidenceFilters,
    FindingsFilters,
    KnowledgeQueryService,
    PaginatedResult,
    PaginationParams,
)


def test_pagination_clamps_limit_and_offset_deterministically() -> None:
    normalized = PaginationParams(limit=9999, offset=-15).normalized()
    assert normalized.limit == 100
    assert normalized.offset == 0

    normalized_low = PaginationParams(limit=0, offset="-8").normalized()
    assert normalized_low.limit == 1
    assert normalized_low.offset == 0


def test_empty_filter_behavior_is_stable() -> None:
    engagement_filters = EngagementListFilters(query="   ", limit=None, offset=None).normalized()
    assert engagement_filters.query is None
    assert engagement_filters.limit == 20
    assert engagement_filters.offset == 0

    findings = FindingsFilters().normalized()
    assert findings.severity is None
    assert findings.status is None
    assert findings.exploited is None
    assert findings.asset is None
    assert findings.source is None
    assert findings.query is None
    assert findings.include_candidates is False
    assert findings.sort == "last_seen_desc"
    assert findings.limit == 20
    assert findings.offset == 0


def test_query_string_normalization_strips_and_preserves_signal() -> None:
    findings = FindingsFilters(query="   Apache httpd   ", asset=" host.ip:10.0.0.8 ").normalized()
    assets = AssetsFilters(query="  OpenSSH  ", type=" host.ip ").normalized()
    evidence = EvidenceFilters(query="  cve-2021-44228  ", source_tool="  nmap ").normalized()

    assert findings.query == "Apache httpd"
    assert findings.asset == "host.ip:10.0.0.8"
    assert assets.query == "OpenSSH"
    assert assets.type == "host.ip"
    assert evidence.query == "cve-2021-44228"
    assert evidence.source_tool == "nmap"


def test_invalid_boolean_and_unsupported_filters_are_handled_deterministically() -> None:
    findings = FindingsFilters(
        severity="unexpected",
        status="unknown_status",
        exploited="not-a-bool",
        sort="invalid_sort",
    ).normalized()
    assets = AssetsFilters(vulnerable="2", exploited="maybe", sort="wrong").normalized()
    evidence = EvidenceFilters(sort="not_supported").normalized()

    assert findings.severity is None
    assert findings.status is None
    assert findings.exploited is None
    assert findings.sort == "last_seen_desc"

    assert assets.vulnerable is None
    assert assets.exploited is None
    assert assets.sort == "last_seen_desc"

    assert evidence.sort == "observed_desc"


def test_web_surface_path_filters_normalize_service_and_origin_keys_deterministically() -> None:
    normalized = WebSurfacePathsFilters(
        service_key=" service.socket:10.0.0.8/tcp/443 ",
        origin_key=" https://Example.com:443 ",
        include_noisy="TrUe",
        limit=None,
        offset="-9",
    ).normalized()
    assert normalized.service_key == "service.socket:10.0.0.8/tcp/443"
    assert normalized.origin_key == "https://Example.com:443"
    assert normalized.include_noisy is True
    assert normalized.limit == 100
    assert normalized.offset == 0


def test_web_surface_path_filters_limit_defaults_to_max_and_clamps() -> None:
    defaulted = WebSurfacePathsFilters().normalized()
    clamped = WebSurfacePathsFilters(limit=9999, include_noisy="not-a-bool").normalized()

    assert defaulted.limit == 100
    assert clamped.limit == 100
    assert clamped.include_noisy is False


def test_paginated_result_uses_shared_items_total_limit_offset_shape() -> None:
    page = PaginatedResult.from_items(
        items=[{"id": "a"}, {"id": "b"}],
        total=2,
        limit=5000,
        offset=-9,
    )
    payload = page.to_dict()

    assert tuple(payload.keys()) == ("items", "total", "limit", "offset")
    assert payload["items"] == [{"id": "a"}, {"id": "b"}]
    assert payload["total"] == 2
    assert payload["limit"] == 100
    assert payload["offset"] == 0


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, username: str) -> User:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    return user


def _stable_uuid(token: str) -> uuid_lib.UUID:
    return uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"drowai-knowledge-query-{token}")


def _seed_query_plane_sample(db):
    now = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc)
    tenant_id = 201
    foreign_tenant_id = 202
    owner = _seed_user(db, "runner-control-owner")
    owner_second = _seed_user(db, "runner-control-owner-2")
    foreign = _seed_user(db, "runner-control-foreign")

    owned_a = Engagement(
        tenant_id=tenant_id,
        user_id=owner.id,
        name="Alpha",
        status="active",
        created_at=now,
        updated_at=now + timedelta(minutes=2),
    )
    owned_b = Engagement(
        tenant_id=tenant_id,
        user_id=owner.id,
        name="Bravo",
        status="active",
        created_at=now,
        updated_at=now + timedelta(minutes=1),
    )
    owned_archived = Engagement(
        tenant_id=tenant_id,
        user_id=owner.id,
        name="Charlie Archived",
        status="archived",
        created_at=now,
        updated_at=now - timedelta(minutes=1),
    )
    foreign_engagement = Engagement(
        tenant_id=foreign_tenant_id,
        user_id=foreign.id,
        name="Foreign",
        status="active",
        created_at=now,
        updated_at=now + timedelta(minutes=3),
    )
    other_owned = Engagement(
        tenant_id=tenant_id,
        user_id=owner_second.id,
        name="Owner 2",
        status="active",
        created_at=now,
        updated_at=now + timedelta(minutes=4),
    )
    db.add_all([owned_a, owned_b, owned_archived, foreign_engagement, other_owned])
    db.flush()

    asset_a = KnowledgeAsset(
        id=_stable_uuid("asset-a"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        asset_key="host.ip:10.0.0.10",
        asset_type="host.ip",
        display_name="10.0.0.10",
        ip_address="10.0.0.10",
        hostname=None,
        status="up",
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(hours=1),
        max_confidence="high",
        asset_metadata={"state": {"host_status": "up"}},
    )
    asset_b = KnowledgeAsset(
        id=_stable_uuid("asset-b"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        asset_key="host.ip:10.0.0.11",
        asset_type="host.ip",
        display_name="10.0.0.11",
        ip_address="10.0.0.11",
        hostname=None,
        status="up",
        first_seen_at=now - timedelta(days=3),
        last_seen_at=now - timedelta(minutes=30),
        max_confidence="high",
        asset_metadata={"state": {"host_status": "up"}},
    )
    db.add_all([asset_a, asset_b])
    db.flush()

    service_a = KnowledgeService(
        id=_stable_uuid("service-a"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        service_key="service.socket:10.0.0.10/tcp/443",
        asset_id=asset_a.id,
        protocol="tcp",
        port=443,
        service_name="https",
        product="nginx",
        version="1.25",
        status="open",
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now - timedelta(minutes=20),
        service_metadata={"state": {"service_name": "https"}},
    )
    db.add(service_a)
    db.flush()

    finding_a = KnowledgeFinding(
        id=_stable_uuid("finding-a"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        finding_key="finding.vulnerability:host.ip:10.0.0.10:openssl-cve",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.10",
        asset_id=asset_a.id,
        service_id=service_a.id,
        title="OpenSSL vulnerability",
        severity="critical",
        status="open",
        assertion_level="observed",
        confidence="high",
        first_seen_at=now - timedelta(hours=12),
        last_seen_at=now - timedelta(minutes=15),
        evidence_summary={"evidence_refs": [{"evidence_archive_id": "ev-1"}, {"evidence_archive_id": "ev-2"}]},
        finding_metadata={
            "source_tool": "nmap",
            "state": {
                "severity": "critical",
                "detector_id": "nmap/ssl-cert-expired",
                "script_id": "ssl-cert",
                "summary": "Subject: CN=example.com; Not valid after: 2025-01-01T00:00:00 - expired",
            },
            "evidence_refs": [{"evidence_archive_id": "ev-2"}, {"evidence_archive_id": "ev-4"}],
        },
    )
    finding_b = KnowledgeFinding(
        id=_stable_uuid("finding-b"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        finding_key="finding.vulnerability:host.ip:10.0.0.11:exploit-demo",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.11",
        asset_id=asset_b.id,
        service_id=None,
        title="Exploited service",
        severity="medium",
        status="exploited",
        assertion_level="exploited",
        confidence="high",
        first_seen_at=now - timedelta(hours=10),
        last_seen_at=now - timedelta(minutes=10),
        evidence_summary={"evidence_refs": [{"evidence_archive_id": "ev-3"}]},
        finding_metadata={"source_tool": "metasploit", "state": {"severity": "medium", "exploited": True}},
    )
    finding_candidate = KnowledgeFinding(
        id=_stable_uuid("finding-candidate"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        finding_key="finding.vulnerability:host.ip:10.0.0.10:candidate-replay",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.10",
        asset_id=asset_a.id,
        service_id=None,
        title="Candidate-only signal",
        severity="critical",
        status="candidate",
        assertion_level="candidate",
        confidence="low",
        first_seen_at=now - timedelta(hours=9),
        last_seen_at=now - timedelta(minutes=5),
        evidence_summary={"evidence_refs": [{"evidence_archive_id": "ev-candidate"}]},
        finding_metadata={
            "source_tool": "llm.candidate_extraction",
            "authority": {"source_kind": "llm_candidate", "candidate_only": True},
            "state": {"severity": "critical"},
        },
    )
    db.add_all([finding_a, finding_b, finding_candidate])

    rel_b = KnowledgeRelationship(
        id=_stable_uuid("relationship-b"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        relationship_key="relationship.edge:service.socket:10.0.0.10/tcp/443:hosts:host.ip:10.0.0.10",
        source_subject_key="service.socket:10.0.0.10/tcp/443",
        relationship_type="hosts",
        target_subject_key="host.ip:10.0.0.10",
        confidence="high",
        first_seen_at=now - timedelta(hours=6),
        last_seen_at=now - timedelta(hours=1),
        relationship_metadata={"source": "projection"},
    )
    rel_a = KnowledgeRelationship(
        id=_stable_uuid("relationship-a"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        relationship_key="relationship.edge:host.ip:10.0.0.10:exposes:service.socket:10.0.0.10/tcp/443",
        source_subject_key="host.ip:10.0.0.10",
        relationship_type="exposes",
        target_subject_key="service.socket:10.0.0.10/tcp/443",
        confidence="high",
        first_seen_at=now - timedelta(hours=7),
        last_seen_at=now - timedelta(hours=2),
        relationship_metadata={"source": "projection"},
    )
    db.add_all([rel_b, rel_a])

    evidence_a = KnowledgeEvidenceArchive(
        id=_stable_uuid("evidence-a"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        task_id=None,
        source_execution_id=_stable_uuid("execution-a"),
        source_artifact_id=_stable_uuid("artifact-a"),
        storage_mode="inline_excerpt",
        inline_excerpt="critical output",
        archived_file_ref=None,
        created_at=now - timedelta(minutes=6),
        lineage_snapshot={"source_tool": "nmap"},
        archive_metadata={"type": "terminal"},
    )
    evidence_b = KnowledgeEvidenceArchive(
        id=_stable_uuid("evidence-b"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=owned_a.id,
        task_id=None,
        source_execution_id=_stable_uuid("execution-b"),
        source_artifact_id=_stable_uuid("artifact-b"),
        storage_mode="metadata_only",
        inline_excerpt=None,
        archived_file_ref=None,
        created_at=now - timedelta(minutes=5),
        lineage_snapshot={"source_tool": "metasploit"},
        archive_metadata={"type": "log"},
    )
    db.add_all([evidence_a, evidence_b])
    db.commit()

    return {
        "owner": owner,
        "owned_a": owned_a,
        "owned_b": owned_b,
        "owned_archived": owned_archived,
        "foreign": foreign,
        "asset_a": asset_a,
        "service_a": service_a,
        "finding_a": finding_a,
    }


def _insert_evidence_row(
    db,
    *,
    token: str,
    tenant_id: int = 201,
    user_id: int = 1,
    engagement_id: int,
    source_execution_token: str,
    source_tool: str,
    artifact_kind: str,
    metadata_type: str,
    relative_path: str | None = None,
    inline_excerpt: str | None = None,
    created_at: datetime | None = None,
) -> KnowledgeEvidenceArchive:
    row = KnowledgeEvidenceArchive(
        id=_stable_uuid(f"evidence-{token}"),
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=None,
        source_execution_id=_stable_uuid(source_execution_token),
        source_artifact_id=_stable_uuid(f"artifact-{token}"),
        storage_mode="inline_excerpt",
        inline_excerpt=inline_excerpt,
        archived_file_ref=None,
        created_at=created_at,
        lineage_snapshot={
            "source_tool": source_tool,
            "artifact_kind": artifact_kind,
            "relative_path": relative_path,
        },
        archive_metadata={"type": metadata_type},
    )
    db.add(row)
    db.flush()
    return row


def test_list_engagements_returns_only_owned_engagements() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        result = service.list_engagements(
            user_id=seeded["owner"].id,
            filters=EngagementListFilters(limit=10, offset=0),
        )

        names = [row["name"] for row in result["items"]]
        assert names == ["Alpha", "Bravo"]
        assert result["total"] == 2
    finally:
        db.close()
        engine.dispose()


def test_list_engagements_status_all_includes_archived() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        result = service.list_engagements(
            user_id=seeded["owner"].id,
            filters=EngagementListFilters(status="all", limit=10, offset=0),
        )

        names = [row["name"] for row in result["items"]]
        assert names == ["Alpha", "Bravo", "Charlie Archived"]
        assert result["total"] == 3
    finally:
        db.close()
        engine.dispose()


def test_summary_aggregates_severity_and_asset_counts() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        summary = service.get_summary(user_id=seeded["owner"].id)

        assert summary["open_findings_total"] == 2
        assert summary["open_findings_by_severity"]["critical"] == 1
        assert summary["open_findings_by_severity"]["medium"] == 1
        assert summary["asset_counts"]["total"] == 2
        assert summary["asset_counts"]["vulnerable"] == 2
        assert summary["asset_counts"]["exploited"] == 1
        assert summary["service_count"] == 1
        assert summary["evidence_count"] == 2
    finally:
        db.close()
        engine.dispose()


def test_tenant_scoped_knowledge_queries_exclude_same_tenant_non_owner_rows() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        now = datetime(2026, 3, 8, 8, 0, 0, tzinfo=timezone.utc)
        teammate = _seed_user(db, "runner-control-same-tenant-teammate")
        teammate_engagement = Engagement(
            tenant_id=201,
            user_id=teammate.id,
            name="Teammate Engagement",
            status="active",
            created_at=now,
            updated_at=now,
        )
        db.add(teammate_engagement)
        db.flush()
        teammate_asset = KnowledgeAsset(
            id=_stable_uuid("asset-same-tenant-teammate"),
            tenant_id=201,
            user_id=teammate.id,
            engagement_id=teammate_engagement.id,
            asset_key="host.ip:10.0.0.99",
            asset_type="host.ip",
            display_name="10.0.0.99",
            status="up",
            first_seen_at=now,
            last_seen_at=now,
            asset_metadata={},
        )
        teammate_evidence = KnowledgeEvidenceArchive(
            id=_stable_uuid("evidence-same-tenant-teammate"),
            tenant_id=201,
            user_id=teammate.id,
            engagement_id=teammate_engagement.id,
            task_id=None,
            source_execution_id=_stable_uuid("execution-same-tenant-teammate"),
            source_artifact_id=_stable_uuid("artifact-same-tenant-teammate"),
            storage_mode="inline_excerpt",
            inline_excerpt="teammate output",
            archived_file_ref=None,
            created_at=now,
            lineage_snapshot={"source_tool": "nmap"},
            archive_metadata={"type": "terminal"},
        )
        db.add_all([teammate_asset, teammate_evidence])
        db.commit()

        service = KnowledgeQueryService(db)
        summary = service.get_summary(user_id=seeded["owner"].id, tenant_id=201)
        assets = service.list_assets(
            user_id=seeded["owner"].id,
            tenant_id=201,
            filters=AssetsFilters(limit=50, offset=0),
        )
        evidence = service.list_evidence(
            user_id=seeded["owner"].id,
            tenant_id=201,
            filters=EvidenceFilters(limit=50, offset=0),
        )
        hidden_asset = service.get_asset(
            user_id=seeded["owner"].id,
            tenant_id=201,
            asset_id=str(teammate_asset.id),
        )
        hidden_engagement = service.get_engagement(
            user_id=seeded["owner"].id,
            tenant_id=201,
            engagement_id=teammate_engagement.id,
        )

        assert summary["asset_counts"]["total"] == 2
        assert summary["evidence_count"] == 2
        assert assets["total"] == 2
        assert evidence["total"] == 2
        assert hidden_asset is None
        assert hidden_engagement is None
    finally:
        db.close()
        engine.dispose()


def test_get_finding_resolves_linked_asset_service_and_evidence_refs() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        finding = service.get_finding(
            user_id=seeded["owner"].id,
            finding_id=str(seeded["finding_a"].id),
        )

        assert finding is not None
        assert finding["asset"] is not None
        assert finding["asset"]["id"] == str(seeded["asset_a"].id)
        assert finding["service"] is not None
        assert finding["service"]["id"] == str(seeded["service_a"].id)
        assert len(finding["evidence_refs"]) == 3
        assert finding["evidence_refs"][0]["evidence_archive_id"] == "ev-1"
        assert finding["evidence_refs"][1]["evidence_archive_id"] == "ev-2"
        assert finding["evidence_refs"][2]["evidence_archive_id"] == "ev-4"
        assert finding["affected_asset_count"] == 1
        assert finding["evidence_count"] == 3
    finally:
        db.close()
        engine.dispose()


def test_get_finding_preserves_curated_nmap_detection_metadata() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        finding = service.get_finding(
            user_id=seeded["owner"].id,
            finding_id=str(seeded["finding_a"].id),
        )

        assert finding is not None
        state = dict((finding.get("metadata") or {}).get("state") or {})
        assert state["detector_id"] == "nmap/ssl-cert-expired"
        assert state["script_id"] == "ssl-cert"
        assert "expired" in state["summary"]
    finally:
        db.close()
        engine.dispose()


def test_get_asset_resolves_linked_services_and_findings() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        asset = service.get_asset(
            user_id=seeded["owner"].id,
            asset_id=str(seeded["asset_a"].id),
        )

        assert asset is not None
        assert asset["service_count"] == 1
        assert asset["finding_count"] == 1
        assert asset["services"][0]["id"] == str(seeded["service_a"].id)
        assert asset["findings"][0]["id"] == str(seeded["finding_a"].id)
    finally:
        db.close()
        engine.dispose()


def test_get_engagement_returns_expected_payload() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        engagement = service.get_engagement(engagement_id=seeded["owned_a"].id)

        assert engagement is not None
        assert engagement["id"] == seeded["owned_a"].id
        assert engagement["name"] == "Alpha"
    finally:
        db.close()
        engine.dispose()


def test_list_findings_returns_paginated_items() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        page = service.list_findings(
            user_id=seeded["owner"].id,
            filters=FindingsFilters(limit=50, offset=0),
        )

        assert page["total"] == 2
        assert len(page["items"]) == 2
        assert all(row["is_candidate"] is False for row in page["items"])
        assert all("evidence_count" in row for row in page["items"])
        assert all("affected_asset_count" in row for row in page["items"])
        assert all(isinstance(row["affected_asset_count"], int) for row in page["items"])
        openssl = next(row for row in page["items"] if row["title"] == "OpenSSL vulnerability")
        assert openssl["source_tool"] == "nmap"
        assert openssl["asset"]["display_name"] == "10.0.0.10"
        assert openssl["service"]["service_name"] == "https"
    finally:
        db.close()
        engine.dispose()


def test_list_findings_promotes_source_tool_from_linked_evidence() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        finding = seeded["finding_a"]
        finding.finding_metadata = {"state": {"severity": "critical"}}
        finding.evidence_summary = {
            "evidence_refs": [{"evidence_archive_id": str(_stable_uuid("evidence-a"))}]
        }
        evidence = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.id == _stable_uuid("evidence-a"))
            .one()
        )
        evidence.lineage_snapshot = {"tool_name": "sniffing_spoofing.network_sniffers.tshark"}
        db.commit()
        service = KnowledgeQueryService(db)

        page = service.list_findings(
            user_id=seeded["owner"].id,
            filters=FindingsFilters(limit=50, offset=0),
        )

        openssl = next(row for row in page["items"] if row["title"] == "OpenSSL vulnerability")
        assert openssl["source_tool"] == "sniffing_spoofing.network_sniffers.tshark"
    finally:
        db.close()
        engine.dispose()


def test_list_findings_can_include_candidate_rows_when_explicitly_requested() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        page = service.list_findings(
            user_id=seeded["owner"].id,
            filters=FindingsFilters(limit=50, offset=0, include_candidates=True),
        )

        assert page["total"] == 3
        assert len(page["items"]) == 3
        candidate_rows = [row for row in page["items"] if row.get("is_candidate") is True]
        assert len(candidate_rows) == 1
        assert candidate_rows[0]["status"] == "candidate"
    finally:
        db.close()
        engine.dispose()


def test_list_assets_returns_paginated_items() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        page = service.list_assets(
            user_id=seeded["owner"].id,
            filters=AssetsFilters(limit=50, offset=0),
        )

        assert page["total"] == 2
        assert len(page["items"]) == 2
        assert all("is_vulnerable" in row for row in page["items"])
    finally:
        db.close()
        engine.dispose()


def test_list_services_returns_paginated_items() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        page = service.list_services(
            user_id=seeded["owner"].id,
            limit=50,
            offset=0,
        )

        assert page["total"] == 1
        assert len(page["items"]) == 1
        assert page["items"][0]["service_key"] == "service.socket:10.0.0.10/tcp/443"
    finally:
        db.close()
        engine.dispose()


def test_list_evidence_returns_paginated_items() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(limit=50, offset=0),
        )

        assert page["total"] == 2
        assert len(page["items"]) == 2
        assert {row["source_tool"] for row in page["items"]} == {"nmap", "metasploit"}
    finally:
        db.close()
        engine.dispose()


def test_list_evidence_canonical_prefers_stdout_over_langgraph_tool_txt() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        engagement_id = seeded["owned_a"].id
        now = datetime(2026, 3, 14, 9, 0, 0, tzinfo=timezone.utc)
        _insert_evidence_row(
            db,
            token="canon-txt-xml",
            engagement_id=engagement_id,
            source_execution_token="canon-txt-exec",
            source_tool="canon-tool-txt",
            artifact_kind="tool_file",
            metadata_type="tool_file",
            relative_path="artifacts/nmap_1.xml",
            created_at=now - timedelta(minutes=4),
        )
        _insert_evidence_row(
            db,
            token="canon-txt-command",
            engagement_id=engagement_id,
            source_execution_token="canon-txt-exec",
            source_tool="canon-tool-txt",
            artifact_kind="command",
            metadata_type="command",
            inline_excerpt="nmap 10.0.0.1",
            created_at=now - timedelta(minutes=3),
        )
        expected_stdout = _insert_evidence_row(
            db,
            token="canon-txt-stdout",
            engagement_id=engagement_id,
            source_execution_token="canon-txt-exec",
            source_tool="canon-tool-txt",
            artifact_kind="stdout",
            metadata_type="stdout",
            inline_excerpt="scan output",
            created_at=now - timedelta(minutes=2),
        )
        _insert_evidence_row(
            db,
            token="canon-txt-preferred",
            engagement_id=engagement_id,
            source_execution_token="canon-txt-exec",
            source_tool="canon-tool-txt",
            artifact_kind="tool_file",
            metadata_type="tool_file",
            relative_path="artifacts/20260314090000000000_tool.txt",
            created_at=now - timedelta(minutes=1),
        )
        db.commit()

        service = KnowledgeQueryService(db)
        page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-tool-txt", limit=50, offset=0),
        )

        assert page["total"] == 1
        assert len(page["items"]) == 1
        assert page["items"][0]["id"] == str(expected_stdout.id)
        assert page["items"][0]["evidence_type"] == "stdout"
        group = page["items"][0]["metadata"]["execution_group"]
        assert group["member_count"] == 3
        member_types = {member["evidence_type"] for member in group["members"]}
        assert member_types == {"command", "stdout", "tool_file"}
        assert any(
            member["id"] != str(expected_stdout.id) and member["evidence_type"] == "command"
            for member in group["members"]
        )
    finally:
        db.close()
        engine.dispose()


def test_list_evidence_canonical_falls_back_to_stdout_then_command() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        engagement_id = seeded["owned_a"].id
        now = datetime(2026, 3, 14, 10, 0, 0, tzinfo=timezone.utc)

        _insert_evidence_row(
            db,
            token="canon-stdout-command",
            engagement_id=engagement_id,
            source_execution_token="canon-stdout-exec",
            source_tool="canon-tool-stdout",
            artifact_kind="command",
            metadata_type="command",
            inline_excerpt="run nmap",
            created_at=now - timedelta(minutes=3),
        )
        expected_stdout = _insert_evidence_row(
            db,
            token="canon-stdout-value",
            engagement_id=engagement_id,
            source_execution_token="canon-stdout-exec",
            source_tool="canon-tool-stdout",
            artifact_kind="stdout",
            metadata_type="stdout",
            inline_excerpt="stdout content",
            created_at=now - timedelta(minutes=2),
        )
        _insert_evidence_row(
            db,
            token="canon-stdout-xml",
            engagement_id=engagement_id,
            source_execution_token="canon-stdout-exec",
            source_tool="canon-tool-stdout",
            artifact_kind="tool_file",
            metadata_type="tool_file",
            relative_path="artifacts/nmap_2.xml",
            created_at=now - timedelta(minutes=1),
        )

        expected_command = _insert_evidence_row(
            db,
            token="canon-command-value",
            engagement_id=engagement_id,
            source_execution_token="canon-command-exec",
            source_tool="canon-tool-command",
            artifact_kind="command",
            metadata_type="command",
            inline_excerpt="command only",
            created_at=now - timedelta(minutes=2),
        )
        _insert_evidence_row(
            db,
            token="canon-command-xml",
            engagement_id=engagement_id,
            source_execution_token="canon-command-exec",
            source_tool="canon-tool-command",
            artifact_kind="tool_file",
            metadata_type="tool_file",
            relative_path="artifacts/tool.xml",
            created_at=now - timedelta(minutes=1),
        )
        db.commit()

        service = KnowledgeQueryService(db)
        stdout_page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-tool-stdout", limit=50, offset=0),
        )
        command_page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-tool-command", limit=50, offset=0),
        )

        assert stdout_page["total"] == 1
        assert stdout_page["items"][0]["id"] == str(expected_stdout.id)
        assert stdout_page["items"][0]["evidence_type"] == "stdout"

        assert command_page["total"] == 1
        assert command_page["items"][0]["id"] == str(expected_command.id)
        assert command_page["items"][0]["evidence_type"] == "command"
    finally:
        db.close()
        engine.dispose()


def test_list_evidence_filters_and_sort_apply_to_canonical_rows() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        engagement_id = seeded["owned_a"].id
        now = datetime(2026, 3, 14, 11, 0, 0, tzinfo=timezone.utc)

        older = _insert_evidence_row(
            db,
            token="canon-sort-old-stdout",
            engagement_id=engagement_id,
            source_execution_token="canon-sort-old-exec",
            source_tool="canon-sort-tool",
            artifact_kind="stdout",
            metadata_type="stdout",
            inline_excerpt="alpha signal",
            created_at=now - timedelta(minutes=3),
        )
        _insert_evidence_row(
            db,
            token="canon-sort-old-command",
            engagement_id=engagement_id,
            source_execution_token="canon-sort-old-exec",
            source_tool="canon-sort-tool",
            artifact_kind="command",
            metadata_type="command",
            inline_excerpt="alpha command",
            created_at=now - timedelta(minutes=4),
        )
        newer = _insert_evidence_row(
            db,
            token="canon-sort-new-command",
            engagement_id=engagement_id,
            source_execution_token="canon-sort-new-exec",
            source_tool="canon-sort-tool",
            artifact_kind="command",
            metadata_type="command",
            inline_excerpt="beta command",
            created_at=now - timedelta(minutes=1),
        )
        db.commit()

        service = KnowledgeQueryService(db)
        ordered_page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-sort-tool", sort="observed_asc", limit=50, offset=0),
        )
        stdout_only_page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-sort-tool", type="stdout", limit=50, offset=0),
        )
        query_page = service.list_evidence(
            user_id=seeded["owner"].id,
            filters=EvidenceFilters(source_tool="canon-sort-tool", query="alpha signal", limit=50, offset=0),
        )

        assert ordered_page["total"] == 2
        assert [item["id"] for item in ordered_page["items"]] == [str(older.id), str(newer.id)]

        assert stdout_only_page["total"] == 1
        assert stdout_only_page["items"][0]["id"] == str(older.id)
        assert stdout_only_page["items"][0]["evidence_type"] == "stdout"

        assert query_page["total"] == 1
        assert query_page["items"][0]["id"] == str(older.id)
    finally:
        db.close()
        engine.dispose()


def test_characterization_all_public_methods_are_stable_across_runs() -> None:
    def _capture_output() -> dict[str, object]:
        inner_engine, inner_db = _build_session()
        try:
            seeded = _seed_query_plane_sample(inner_db)
            service = KnowledgeQueryService(inner_db)
            return {
                "list_engagements": service.list_engagements(
                    user_id=seeded["owner"].id,
                    filters=EngagementListFilters(limit=50, offset=0),
                ),
                "get_engagement": service.get_engagement(engagement_id=seeded["owned_a"].id),
                "get_summary": service.get_summary(user_id=seeded["owner"].id),
                "list_findings": service.list_findings(
                    user_id=seeded["owner"].id,
                    filters=FindingsFilters(limit=50, offset=0),
                ),
                "get_finding": service.get_finding(
                    user_id=seeded["owner"].id,
                    finding_id=str(seeded["finding_a"].id),
                ),
                "list_assets": service.list_assets(
                    user_id=seeded["owner"].id,
                    filters=AssetsFilters(limit=50, offset=0),
                ),
                "get_asset": service.get_asset(
                    user_id=seeded["owner"].id,
                    asset_id=str(seeded["asset_a"].id),
                ),
                "list_services": service.list_services(
                    user_id=seeded["owner"].id,
                    limit=50,
                    offset=0,
                ),
                "list_evidence": service.list_evidence(
                    user_id=seeded["owner"].id,
                    filters=EvidenceFilters(limit=50, offset=0),
                ),
                "get_graph_snapshot": service.get_graph_snapshot(user_id=seeded["owner"].id),
            }
        finally:
            inner_db.close()
            inner_engine.dispose()

    first = _capture_output()
    second = _capture_output()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_legacy_import_surface_still_exposes_contracts_and_service() -> None:
    from backend.services.knowledge import query_service as legacy_module

    assert hasattr(legacy_module, "KnowledgeQueryService")
    assert hasattr(legacy_module, "PaginationParams")
    assert hasattr(legacy_module, "PaginatedResult")
    assert hasattr(legacy_module, "EngagementListFilters")
    assert hasattr(legacy_module, "FindingsFilters")
    assert hasattr(legacy_module, "AssetsFilters")
    assert hasattr(legacy_module, "EvidenceFilters")


def test_graph_snapshot_is_deterministic() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        assert len(snapshot["nodes"]) >= 3
        edge_triplets = [
            (row["source"], row["relationship_type"], row["target"])
            for row in snapshot["edges"]
        ]
        assert edge_triplets == sorted(edge_triplets)
        assert (
            "host.ip:10.0.0.10",
            "exposes",
            "service.socket:10.0.0.10/tcp/443",
        ) in edge_triplets
    finally:
        db.close()
        engine.dispose()


def test_methods_support_optional_owner_guard_for_foreign_engagement() -> None:
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)

        foreign_id = seeded["owned_a"].id
        caller_user_id = seeded["foreign"].id

        assert service.get_engagement(engagement_id=foreign_id, user_id=caller_user_id) is None
        assert service.get_finding(
            user_id=caller_user_id,
            finding_id=str(seeded["finding_a"].id),
        ) is None
        assert service.get_asset(
            user_id=caller_user_id,
            asset_id=str(seeded["asset_a"].id),
        ) is None

        findings_page = service.list_findings(
            user_id=caller_user_id,
            filters=FindingsFilters(limit=20, offset=0),
        )
        assert findings_page["items"] == []
        assert findings_page["total"] == 0

        summary = service.get_summary(user_id=caller_user_id)
        assert summary["open_findings_total"] == 0
        assert summary["asset_counts"]["total"] == 0

        graph = service.get_graph_snapshot(user_id=caller_user_id)
        assert graph["nodes"] == []
        assert graph["edges"] == []
    finally:
        db.close()
        engine.dispose()


def test_engagement_scoped_reads_isolate_rows_between_same_user_engagements() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "phase5-same-user")
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        engagement_one = Engagement(user_id=user.id, tenant_id=301, name="Eng One", status="active", updated_at=now)
        engagement_two = Engagement(user_id=user.id, tenant_id=301, name="Eng Two", status="active", updated_at=now)
        db.add_all([engagement_one, engagement_two])
        db.flush()

        asset_one = KnowledgeAsset(
            id=_stable_uuid("phase5-same-user-asset-one"),
            tenant_id=301,
            user_id=user.id,
            engagement_id=engagement_one.id,
            asset_key="host.ip:10.30.0.1",
            asset_type="host.ip",
            first_seen_at=now,
            last_seen_at=now,
        )
        asset_two = KnowledgeAsset(
            id=_stable_uuid("phase5-same-user-asset-two"),
            tenant_id=301,
            user_id=user.id,
            engagement_id=engagement_two.id,
            asset_key="host.ip:10.30.0.2",
            asset_type="host.ip",
            first_seen_at=now,
            last_seen_at=now,
        )
        evidence_one = KnowledgeEvidenceArchive(
            id=_stable_uuid("phase5-same-user-evidence-one"),
            tenant_id=301,
            user_id=user.id,
            engagement_id=engagement_one.id,
            task_id=None,
            source_execution_id=_stable_uuid("phase5-same-user-exec-one"),
            source_artifact_id=_stable_uuid("phase5-same-user-artifact-one"),
            storage_mode="inline_excerpt",
            inline_excerpt="engagement one",
            archived_file_ref=None,
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
            created_at=now,
        )
        evidence_two = KnowledgeEvidenceArchive(
            id=_stable_uuid("phase5-same-user-evidence-two"),
            tenant_id=301,
            user_id=user.id,
            engagement_id=engagement_two.id,
            task_id=None,
            source_execution_id=_stable_uuid("phase5-same-user-exec-two"),
            source_artifact_id=_stable_uuid("phase5-same-user-artifact-two"),
            storage_mode="inline_excerpt",
            inline_excerpt="engagement two",
            archived_file_ref=None,
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
            created_at=now + timedelta(seconds=5),
        )
        db.add_all([asset_one, asset_two, evidence_one, evidence_two])
        db.commit()

        service = KnowledgeQueryService(db)
        assets = service.list_assets(
            user_id=user.id,
            tenant_id=301,
            engagement_id=engagement_one.id,
            filters=AssetsFilters(limit=20, offset=0),
        )
        evidence = service.list_evidence(
            user_id=user.id,
            tenant_id=301,
            engagement_id=engagement_one.id,
            filters=EvidenceFilters(limit=20, offset=0),
        )
        summary = service.get_summary(user_id=user.id, tenant_id=301, engagement_id=engagement_one.id)

        assert assets["total"] == 1
        assert assets["items"][0]["asset_key"] == "host.ip:10.30.0.1"
        assert evidence["total"] == 1
        assert evidence["items"][0]["id"] == str(evidence_one.id)
        assert summary["asset_counts"]["total"] == 1
        assert summary["evidence_count"] == 1
    finally:
        db.close()
        engine.dispose()


def test_engagement_scoped_reads_hide_same_tenant_rows_owned_by_other_users() -> None:
    engine, db = _build_session()
    try:
        now = datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)
        owner = _seed_user(db, "phase5-tenant-owner")
        second_user = _seed_user(db, "phase5-tenant-second-user")
        engagement_one = Engagement(user_id=owner.id, tenant_id=302, name="Tenant A1", status="active", updated_at=now)
        engagement_two = Engagement(
            user_id=second_user.id,
            tenant_id=302,
            name="Tenant A2",
            status="active",
            updated_at=now,
        )
        db.add_all([engagement_one, engagement_two])
        db.flush()

        shared_asset = KnowledgeAsset(
            id=_stable_uuid("phase5-tenant-shared-asset"),
            tenant_id=302,
            user_id=owner.id,
            engagement_id=engagement_one.id,
            asset_key="host.ip:10.30.2.10",
            asset_type="host.ip",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(shared_asset)
        db.flush()

        db.add_all(
            [
                EngagementAssetLink(
                    tenant_id=302,
                    engagement_id=engagement_one.id,
                    asset_id=shared_asset.id,
                    first_seen_in_engagement=now,
                    last_seen_in_engagement=now,
                ),
                EngagementAssetLink(
                    tenant_id=302,
                    engagement_id=engagement_two.id,
                    asset_id=shared_asset.id,
                    first_seen_in_engagement=now,
                    last_seen_in_engagement=now,
                ),
            ]
        )
        db.commit()

        service = KnowledgeQueryService(db)
        page = service.list_assets(
            user_id=second_user.id,
            tenant_id=302,
            engagement_id=engagement_two.id,
            filters=AssetsFilters(limit=20, offset=0),
        )
        detail = service.get_asset(
            user_id=second_user.id,
            tenant_id=302,
            engagement_id=engagement_two.id,
            asset_id=str(shared_asset.id),
        )

        assert page["total"] == 0
        assert page["items"] == []
        assert detail is None
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Phase 1: FK-synthesized edge tests
# ---------------------------------------------------------------------------


def _seed_fk_synthesis_sample(db):
    """Seed assets, services, findings with FK links but NO explicit relationships."""
    now = datetime(2026, 3, 22, 10, 0, 0, tzinfo=timezone.utc)
    tenant_id = 701
    owner = _seed_user(db, "fk-synth-owner")

    engagement = Engagement(
        user_id=owner.id,
        tenant_id=tenant_id,
        name="FK Synth Engagement",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(engagement)
    db.flush()

    asset = KnowledgeAsset(
        id=_stable_uuid("fk-asset"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=engagement.id,
        asset_key="host.ip:192.168.1.1",
        asset_type="host.ip",
        display_name="192.168.1.1",
        ip_address="192.168.1.1",
        status="up",
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now,
        asset_metadata={},
    )
    db.add(asset)
    db.flush()

    service_linked = KnowledgeService(
        id=_stable_uuid("fk-svc-linked"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=engagement.id,
        service_key="service.socket:192.168.1.1/tcp/5432",
        asset_id=asset.id,
        protocol="tcp",
        port=5432,
        service_name="postgresql",
        status="open",
        first_seen_at=now - timedelta(hours=12),
        last_seen_at=now,
        service_metadata={"state": {"service_name": "postgresql"}},
    )
    service_orphan = KnowledgeService(
        id=_stable_uuid("fk-svc-orphan"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=engagement.id,
        service_key="service.socket:10.99.99.99/tcp/80",
        asset_id=None,
        protocol="tcp",
        port=80,
        service_name="http",
        status="open",
        first_seen_at=now - timedelta(hours=6),
        last_seen_at=now,
        service_metadata={},
    )
    db.add_all([service_linked, service_orphan])
    db.flush()

    finding_on_asset = KnowledgeFinding(
        id=_stable_uuid("fk-finding-asset"),
        tenant_id=tenant_id,
        user_id=owner.id,
        engagement_id=engagement.id,
        finding_key="finding.vulnerability:host.ip:192.168.1.1:weak-auth",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key="host.ip:192.168.1.1",
        asset_id=asset.id,
        service_id=service_linked.id,
        title="Weak authentication",
        severity="high",
        status="open",
        assertion_level="observed",
        confidence="high",
        first_seen_at=now - timedelta(hours=4),
        last_seen_at=now,
        finding_metadata={},
    )
    db.add(finding_on_asset)
    db.commit()

    return {
        "owner": owner,
        "tenant_id": tenant_id,
        "asset": asset,
        "service_linked": service_linked,
        "service_orphan": service_orphan,
        "finding_on_asset": finding_on_asset,
    }


def test_graph_snapshot_synthesizes_exposes_edge_from_fk() -> None:
    """Service with asset_id FK but no explicit relationship gets a synthetic exposes edge."""
    engine, db = _build_session()
    try:
        seeded = _seed_fk_synthesis_sample(db)
        service = KnowledgeQueryService(db)
        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        exposes_edges = [
            e for e in snapshot["edges"]
            if e["relationship_type"] == "exposes"
        ]
        assert len(exposes_edges) == 1
        edge = exposes_edges[0]
        assert edge["source"] == "host.ip:192.168.1.1"
        assert edge["target"] == "service.socket:192.168.1.1/tcp/5432"
        assert edge["metadata"]["synthetic"] is True
    finally:
        db.close()
        engine.dispose()


def test_graph_snapshot_deduplicates_against_explicit_relationships() -> None:
    """FK-based exposes edge is NOT created when an explicit relationship already exists."""
    engine, db = _build_session()
    try:
        seeded = _seed_query_plane_sample(db)
        service = KnowledgeQueryService(db)
        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        exposes_edges = [
            e for e in snapshot["edges"]
            if e["relationship_type"] == "exposes"
               and e["source"] == "host.ip:10.0.0.10"
               and e["target"] == "service.socket:10.0.0.10/tcp/443"
        ]
        assert len(exposes_edges) == 1
        assert exposes_edges[0]["metadata"].get("synthetic") is not True
    finally:
        db.close()
        engine.dispose()


def test_graph_snapshot_synthesizes_has_finding_edges() -> None:
    """Findings with asset_id/service_id FK get synthetic has_finding edges."""
    engine, db = _build_session()
    try:
        seeded = _seed_fk_synthesis_sample(db)
        service = KnowledgeQueryService(db)
        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        has_finding_edges = [
            e for e in snapshot["edges"]
            if e["relationship_type"] == "has_finding"
        ]
        sources = {e["source"] for e in has_finding_edges}
        assert "host.ip:192.168.1.1" in sources
        assert "service.socket:192.168.1.1/tcp/5432" in sources
        assert all(e["metadata"]["synthetic"] is True for e in has_finding_edges)
    finally:
        db.close()
        engine.dispose()


def test_graph_snapshot_service_metadata_includes_asset_key() -> None:
    """Service nodes in the graph include asset_key in metadata when linked via FK."""
    engine, db = _build_session()
    try:
        seeded = _seed_fk_synthesis_sample(db)
        service = KnowledgeQueryService(db)
        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        svc_nodes = [n for n in snapshot["nodes"] if n["node_type"] == "service"]
        linked = next(n for n in svc_nodes if n["id"] == "service.socket:192.168.1.1/tcp/5432")
        orphan = next(n for n in svc_nodes if n["id"] == "service.socket:10.99.99.99/tcp/80")

        assert linked["metadata"]["asset_key"] == "host.ip:192.168.1.1"
        assert linked["metadata"]["transport_protocol"] == "tcp"
        assert "protocol" not in linked["metadata"]
        assert "asset_key" not in orphan["metadata"]
    finally:
        db.close()
        engine.dispose()


def test_graph_snapshot_orphan_service_has_no_synthetic_edge() -> None:
    """Service with asset_id=None produces no synthetic exposes edge."""
    engine, db = _build_session()
    try:
        seeded = _seed_fk_synthesis_sample(db)
        service = KnowledgeQueryService(db)
        snapshot = service.get_graph_snapshot(user_id=seeded["owner"].id)

        orphan_edges = [
            e for e in snapshot["edges"]
            if "10.99.99.99" in str(e.get("source", "")) or "10.99.99.99" in str(e.get("target", ""))
        ]
        exposes_orphan = [e for e in orphan_edges if e["relationship_type"] == "exposes"]
        assert len(exposes_orphan) == 0
    finally:
        db.close()
        engine.dispose()


def test_graph_snapshot_synthetic_edges_maintain_deterministic_order() -> None:
    """Synthetic FK edges are included in deterministic sort order alongside explicit edges."""
    engine, db = _build_session()
    try:
        seeded = _seed_fk_synthesis_sample(db)
        service = KnowledgeQueryService(db)
        snapshot_a = service.get_graph_snapshot(user_id=seeded["owner"].id)
        snapshot_b = service.get_graph_snapshot(user_id=seeded["owner"].id)

        assert json.dumps(snapshot_a, sort_keys=True) == json.dumps(snapshot_b, sort_keys=True)

        edge_triplets = [
            (str(e["source"]), str(e["relationship_type"]), str(e["target"]))
            for e in snapshot_a["edges"]
        ]
        assert edge_triplets == sorted(edge_triplets)
    finally:
        db.close()
        engine.dispose()
