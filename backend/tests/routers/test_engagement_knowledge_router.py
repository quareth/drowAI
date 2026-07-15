"""API tests for engagement knowledge router endpoints.

These tests validate authentication, engagement ownership, pagination envelopes,
nested engagement scoping, and bounded durable evidence-read response contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid as uuid_lib

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config.workspace_config import WorkspaceConfig
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
from backend.routers import engagement_knowledge as engagement_routes


def _stable_uuid(token: str) -> uuid_lib.UUID:
    return uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"drowai-engagement-knowledge-router-{token}")


@pytest.fixture
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="knowledge-router-owner", password="secret")
        foreign = User(username="knowledge-router-foreign", password="secret")
        db.add_all([owner, foreign])
        db.flush()

        now = datetime(2026, 3, 8, 7, 0, 0, tzinfo=timezone.utc)
        owned = Engagement(user_id=owner.id, tenant_id=701, name="Owned", status="active", updated_at=now)
        archived_owned = Engagement(
            user_id=owner.id,
            tenant_id=701,
            name="Owned Archived",
            status="archived",
            updated_at=now - timedelta(minutes=5),
        )
        foreign_engagement = Engagement(
            user_id=foreign.id,
            tenant_id=702,
            name="Foreign",
            status="active",
            updated_at=now,
        )
        db.add_all([owned, archived_owned, foreign_engagement])
        db.flush()

        asset_a = KnowledgeAsset(
            id=_stable_uuid("asset-a"),
            tenant_id=owned.tenant_id,
            user_id=owner.id,
            engagement_id=owned.id,
            asset_key="host.ip:10.0.0.10",
            asset_type="host.ip",
            display_name="10.0.0.10",
            ip_address="10.0.0.10",
            status="up",
            first_seen_at=now - timedelta(days=2),
            last_seen_at=now - timedelta(hours=1),
            max_confidence="high",
        )
        db.add(asset_a)
        db.flush()

        service_a = KnowledgeService(
            id=_stable_uuid("service-a"),
            tenant_id=owned.tenant_id,
            user_id=owner.id,
            engagement_id=owned.id,
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
        )
        db.add(service_a)
        db.flush()

        finding_a = KnowledgeFinding(
            id=_stable_uuid("finding-a"),
            tenant_id=owned.tenant_id,
            user_id=owner.id,
            engagement_id=owned.id,
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
            evidence_summary={"evidence_refs": [{"evidence_archive_id": "ev-1"}]},
            finding_metadata={"evidence_refs": [{"evidence_archive_id": "ev-2"}]},
        )
        db.add(finding_a)

        relationship = KnowledgeRelationship(
            id=_stable_uuid("rel-a"),
            tenant_id=owned.tenant_id,
            user_id=owner.id,
            engagement_id=owned.id,
            relationship_key="relationship.edge:host.ip:10.0.0.10:exposes:service.socket:10.0.0.10/tcp/443",
            source_subject_key="host.ip:10.0.0.10",
            relationship_type="exposes",
            target_subject_key="service.socket:10.0.0.10/tcp/443",
            confidence="high",
            first_seen_at=now - timedelta(hours=7),
            last_seen_at=now - timedelta(hours=2),
        )
        db.add(relationship)

        durable_paths = WorkspaceConfig.ensure_engagement_durable_structure(owned.id)
        evidence_file = durable_paths["evidence"] / "router-evidence.txt"
        evidence_file.write_text("ABCDEFGHIJ", encoding="utf-8")

        evidence_a = KnowledgeEvidenceArchive(
            id=_stable_uuid("evidence-a"),
            tenant_id=owned.tenant_id,
            user_id=owner.id,
            engagement_id=owned.id,
            task_id=None,
            source_execution_id=_stable_uuid("execution-a"),
            source_artifact_id=_stable_uuid("artifact-a"),
            storage_mode="archived_file",
            inline_excerpt=None,
            archived_file_ref=str(evidence_file.resolve()),
            lineage_snapshot={"source_tool": "nmap", "source_path": "/tmp/secret-path"},
            archive_metadata={"type": "terminal", "workspace_path": "/tmp/workspace"},
        )
        evidence_b = KnowledgeEvidenceArchive(
            id=_stable_uuid("evidence-b"),
            tenant_id=foreign_engagement.tenant_id,
            user_id=foreign.id,
            engagement_id=foreign_engagement.id,
            task_id=None,
            source_execution_id=_stable_uuid("execution-b"),
            source_artifact_id=_stable_uuid("artifact-b"),
            storage_mode="inline_excerpt",
            inline_excerpt="foreign",
            archived_file_ref=None,
            lineage_snapshot={"source_tool": "metasploit"},
            archive_metadata={"type": "log"},
        )
        db.add_all([evidence_a, evidence_b])
        db.commit()

        seeded = {
            "owner_id": owner.id,
            "foreign_user_id": foreign.id,
            "engagement_id": owned.id,
            "archived_engagement_id": archived_owned.id,
            "foreign_engagement_id": foreign_engagement.id,
            "finding_id": str(finding_a.id),
            "asset_id": str(asset_a.id),
            "foreign_evidence_id": str(evidence_b.id),
            "evidence_id": str(evidence_a.id),
        }

    app = FastAPI()
    app.include_router(engagement_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )
        token = auth_header.split(" ", 1)[1].strip()
        if token == "owner-token":
            return SimpleNamespace(id=seeded["owner_id"], username="owner", is_active=True)
        if token == "foreign-token":
            return SimpleNamespace(id=seeded["foreign_user_id"], username="foreign", is_active=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    app.dependency_overrides[engagement_routes.get_db] = fake_get_db
    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user

    def fake_get_tenant_request_context(request: Request):
        if request.headers.get("Authorization", "") == "Bearer owner-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["owner_id"], role="owner")
        return SimpleNamespace(tenant_id=702, user_id=seeded["foreign_user_id"], role="owner")

    app.dependency_overrides[engagement_routes.get_tenant_request_context] = (
        fake_get_tenant_request_context
    )

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_unauthenticated_request_gets_401(api_client) -> None:
    client, seeded = api_client
    response = client.get(f"/api/engagements/{seeded['engagement_id']}")
    assert response.status_code == 401, response.text


def test_foreign_engagement_gets_404(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['foreign_engagement_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Engagement not found"


def test_list_engagements_returns_owned_only(api_client) -> None:
    client, _seeded = api_client
    response = client.get(
        "/api/engagements",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["name"] == "Owned"


def test_list_engagements_status_all_includes_archived(api_client) -> None:
    client, _seeded = api_client
    response = client.get(
        "/api/engagements?status=all",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2
    names = {item["name"] for item in payload["items"]}
    assert names == {"Owned", "Owned Archived"}


def test_get_engagement_returns_expected_payload(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["id"] == seeded["engagement_id"]
    assert payload["name"] == "Owned"


def test_summary_route_returns_aggregate_payload(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/summary",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["engagement_id"] == seeded["engagement_id"]
    assert payload["open_findings_total"] == 1
    assert payload["service_count"] == 1
    assert payload["evidence_count"] == 1


def test_list_routes_return_pagination_envelope(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/findings",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("items", "total", "limit", "offset")
    assert payload["total"] == 1
    assert len(payload["items"]) == 1


def test_services_route_returns_pagination_envelope(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/services",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == ("items", "total", "limit", "offset")
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["service_key"] == "service.socket:10.0.0.10/tcp/443"


def test_nested_entity_ids_are_engagement_scoped(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/findings/{seeded['asset_id']}",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Finding not found"


def test_relationship_graph_route_returns_expected_structure(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/relationships/graph",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["engagement_id"] == seeded["engagement_id"]
    assert len(payload["nodes"]) >= 3
    assert len(payload["edges"]) >= 1
    exposes_edges = [e for e in payload["edges"] if e.get("relationship_type") == "exposes"]
    assert len(exposes_edges) >= 1


def test_list_evidence_strips_internal_paths(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        f"/api/engagements/{seeded['engagement_id']}/evidence",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert "workspace_path" not in item.get("metadata", {})
    assert "source_path" not in item.get("lineage", {})


def test_evidence_read_for_foreign_evidence_id_returns_not_found(api_client) -> None:
    client, seeded = api_client
    response = client.post(
        f"/api/engagements/{seeded['engagement_id']}/evidence/{seeded['foreign_evidence_id']}/read",
        json={"mode": "head", "max_chars": 4},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "not_found"
    assert payload["source"] == "none"
    assert payload["content"] is None


def test_evidence_read_route_preserves_bounded_read_shape(api_client) -> None:
    client, seeded = api_client
    response = client.post(
        f"/api/engagements/{seeded['engagement_id']}/evidence/{seeded['evidence_id']}/read",
        json={"mode": "head", "max_chars": 4},
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert tuple(payload.keys()) == (
        "status",
        "evidence_archive_id",
        "storage_mode",
        "content",
        "mode_used",
        "truncated",
        "source",
    )
    assert payload["status"] == "ready"
    assert payload["mode_used"] == "head"
    assert payload["content"] == "ABCD"
    assert payload["truncated"] is True


def test_list_evidence_canonicalizes_mixed_artifacts_per_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="knowledge-router-canon-owner", password="secret")
        db.add(owner)
        db.flush()
        owner_id = owner.id

        now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        engagement = Engagement(user_id=owner.id, name="Owned Canon", status="active", updated_at=now)
        db.add(engagement)
        db.flush()

        execution_mixed = _stable_uuid("execution-mixed")
        mixed_command = KnowledgeEvidenceArchive(
            id=_stable_uuid("canon-command"),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_mixed,
            source_artifact_id=_stable_uuid("canon-command-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt="nmap 10.0.0.1",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=4),
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "command"},
            archive_metadata={"type": "command"},
        )
        mixed_stdout = KnowledgeEvidenceArchive(
            id=_stable_uuid("canon-stdout"),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_mixed,
            source_artifact_id=_stable_uuid("canon-stdout-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt="stdout content",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=3),
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
        )
        mixed_xml = KnowledgeEvidenceArchive(
            id=_stable_uuid("canon-xml"),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_mixed,
            source_artifact_id=_stable_uuid("canon-xml-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt=None,
            archived_file_ref=None,
            created_at=now - timedelta(minutes=2),
            lineage_snapshot={
                "source_tool": "nmap",
                "artifact_kind": "tool_file",
                "relative_path": "artifacts/nmap_1.xml",
            },
            archive_metadata={"type": "tool_file"},
        )
        mixed_tool_txt = KnowledgeEvidenceArchive(
            id=_stable_uuid("canon-tool-txt"),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=execution_mixed,
            source_artifact_id=_stable_uuid("canon-tool-txt-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt=None,
            archived_file_ref=None,
            created_at=now - timedelta(minutes=1),
            lineage_snapshot={
                "source_tool": "nmap",
                "artifact_kind": "tool_file",
                "relative_path": "artifacts/20260314120000000000_tool.txt",
            },
            archive_metadata={"type": "tool_file"},
        )

        second_execution = KnowledgeEvidenceArchive(
            id=_stable_uuid("canon-second-exec"),
            tenant_id=engagement.tenant_id,
            user_id=owner.id,
            engagement_id=engagement.id,
            task_id=None,
            source_execution_id=_stable_uuid("execution-second"),
            source_artifact_id=_stable_uuid("canon-second-exec-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt="second execution",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=5),
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "command"},
            archive_metadata={"type": "command"},
        )

        db.add_all([mixed_command, mixed_stdout, mixed_xml, mixed_tool_txt, second_execution])
        db.commit()

        engagement_id = engagement.id
        tenant_id = engagement.tenant_id
        expected_canonical_id = str(mixed_stdout.id)

    app = FastAPI()
    app.include_router(engagement_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(_request: Request):
        return SimpleNamespace(id=owner_id, username="owner", is_active=True)

    app.dependency_overrides[engagement_routes.get_db] = fake_get_db
    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[engagement_routes.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=tenant_id, user_id=owner_id, role="owner")
    )

    client = TestClient(app)
    try:
        response = client.get(
            f"/api/engagements/{engagement_id}/evidence",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 2
        assert len(payload["items"]) == 2

        execution_ids = [item["source_execution_id"] for item in payload["items"]]
        assert len(execution_ids) == len(set(execution_ids))

        mixed_rows = [item for item in payload["items"] if item["source_execution_id"] == str(execution_mixed)]
        assert len(mixed_rows) == 1
        assert mixed_rows[0]["id"] == expected_canonical_id
        assert mixed_rows[0]["evidence_type"] == "stdout"
        group = mixed_rows[0]["metadata"]["execution_group"]
        assert group["member_count"] == 3
        member_types = {member["evidence_type"] for member in group["members"]}
        assert member_types == {"command", "stdout", "tool_file"}
        assert any(member["evidence_type"] == "command" for member in group["members"])
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_engagement_evidence_route_isolates_same_user_engagement_rows() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="knowledge-owner-isolation", password="secret")
        db.add(owner)
        db.flush()
        now = datetime(2026, 5, 2, 10, 0, tzinfo=timezone.utc)
        engagement_one = Engagement(user_id=owner.id, tenant_id=401, name="Scope One", status="active", updated_at=now)
        engagement_two = Engagement(user_id=owner.id, tenant_id=401, name="Scope Two", status="active", updated_at=now)
        db.add_all([engagement_one, engagement_two])
        db.flush()

        evidence_one = KnowledgeEvidenceArchive(
            id=_stable_uuid("scope-evidence-one"),
            tenant_id=401,
            user_id=owner.id,
            engagement_id=engagement_one.id,
            task_id=None,
            source_execution_id=_stable_uuid("scope-execution-one"),
            source_artifact_id=_stable_uuid("scope-artifact-one"),
            storage_mode="inline_excerpt",
            inline_excerpt="one",
            archived_file_ref=None,
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
            created_at=now,
        )
        evidence_two = KnowledgeEvidenceArchive(
            id=_stable_uuid("scope-evidence-two"),
            tenant_id=401,
            user_id=owner.id,
            engagement_id=engagement_two.id,
            task_id=None,
            source_execution_id=_stable_uuid("scope-execution-two"),
            source_artifact_id=_stable_uuid("scope-artifact-two"),
            storage_mode="inline_excerpt",
            inline_excerpt="two",
            archived_file_ref=None,
            lineage_snapshot={"source_tool": "nmap", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
            created_at=now,
        )
        db.add_all([evidence_one, evidence_two])
        db.commit()
        owner_id = owner.id
        engagement_one_id = engagement_one.id
        expected_id = str(evidence_one.id)

    app = FastAPI()
    app.include_router(engagement_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(_request: Request):
        return SimpleNamespace(id=owner_id, username="owner", is_active=True)

    app.dependency_overrides[engagement_routes.get_db] = fake_get_db
    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[engagement_routes.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=401, user_id=owner_id, role="owner")
    )

    client = TestClient(app)
    try:
        response = client.get(
            f"/api/engagements/{engagement_one_id}/evidence",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 1
        assert len(payload["items"]) == 1
        assert payload["items"][0]["id"] == expected_id
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_engagement_assets_route_hides_same_tenant_assets_owned_by_other_users() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="knowledge-owner-tenant-shared", password="secret")
        second_user = User(username="knowledge-second-tenant-shared", password="secret")
        db.add_all([owner, second_user])
        db.flush()
        now = datetime(2026, 5, 2, 11, 0, tzinfo=timezone.utc)
        engagement_one = Engagement(user_id=owner.id, tenant_id=402, name="Tenant One", status="active", updated_at=now)
        engagement_two = Engagement(
            user_id=second_user.id,
            tenant_id=402,
            name="Tenant Two",
            status="active",
            updated_at=now,
        )
        db.add_all([engagement_one, engagement_two])
        db.flush()

        shared_asset = KnowledgeAsset(
            id=_stable_uuid("tenant-shared-asset"),
            tenant_id=402,
            user_id=owner.id,
            engagement_id=engagement_one.id,
            asset_key="host.ip:10.40.20.10",
            asset_type="host.ip",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(shared_asset)
        db.flush()
        db.add_all(
            [
                EngagementAssetLink(
                    tenant_id=402,
                    engagement_id=engagement_one.id,
                    asset_id=shared_asset.id,
                    first_seen_in_engagement=now,
                    last_seen_in_engagement=now,
                ),
                EngagementAssetLink(
                    tenant_id=402,
                    engagement_id=engagement_two.id,
                    asset_id=shared_asset.id,
                    first_seen_in_engagement=now,
                    last_seen_in_engagement=now,
                ),
            ]
        )
        db.commit()
        second_user_id = second_user.id
        engagement_two_id = engagement_two.id

    app = FastAPI()
    app.include_router(engagement_routes.router)

    def fake_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_get_current_user(_request: Request):
        return SimpleNamespace(id=second_user_id, username="second", is_active=True)

    app.dependency_overrides[engagement_routes.get_db] = fake_get_db
    app.dependency_overrides[engagement_routes.get_current_user] = fake_get_current_user
    app.dependency_overrides[engagement_routes.get_tenant_request_context] = (
        lambda: SimpleNamespace(tenant_id=402, user_id=second_user_id, role="owner")
    )

    client = TestClient(app)
    try:
        response = client.get(
            f"/api/engagements/{engagement_two_id}/assets",
            headers={"Authorization": "Bearer second-token"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["total"] == 0
        assert payload["items"] == []
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()
