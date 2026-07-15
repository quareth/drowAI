"""API tests for tenant-scoped knowledge router evidence endpoints.

These tests assert `/api/knowledge/evidence` redacts internal storage keys and
host/runtime path fields from lineage/metadata payloads, including grouped
execution members.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid as uuid_lib

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.routers import knowledge as knowledge_routes


def _stable_uuid(token: str) -> uuid_lib.UUID:
    return uuid_lib.uuid5(uuid_lib.NAMESPACE_DNS, f"drowai-knowledge-router-{token}")


def _assert_no_redacted_keys(value: object, forbidden_keys: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in forbidden_keys
            _assert_no_redacted_keys(item, forbidden_keys)
        return
    if isinstance(value, list):
        for item in value:
            _assert_no_redacted_keys(item, forbidden_keys)


@pytest.fixture
def api_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    with session_factory() as db:
        owner = User(username="knowledge-owner", password="secret")
        foreign = User(username="knowledge-foreign", password="secret")
        db.add_all([owner, foreign])
        db.flush()

        now = datetime(2026, 5, 25, 8, 0, 0, tzinfo=timezone.utc)
        owner_engagement = Engagement(
            user_id=owner.id,
            tenant_id=701,
            name="Owned",
            status="active",
            updated_at=now,
        )
        foreign_engagement = Engagement(
            user_id=foreign.id,
            tenant_id=702,
            name="Foreign",
            status="active",
            updated_at=now,
        )
        db.add_all([owner_engagement, foreign_engagement])
        db.flush()

        mixed_execution_id = _stable_uuid("execution-mixed")
        owner_command = KnowledgeEvidenceArchive(
            id=_stable_uuid("owner-command"),
            tenant_id=owner_engagement.tenant_id,
            user_id=owner.id,
            engagement_id=owner_engagement.id,
            task_id=9001,
            source_execution_id=mixed_execution_id,
            source_artifact_id=_stable_uuid("owner-command-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt="nmap command",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=3),
            lineage_snapshot={
                "source_tool": "nmap",
                "artifact_kind": "command",
                "source_path": "/workspace/private/command.txt",
            },
            archive_metadata={
                "type": "command",
                "object_key": "tenants/701/tasks/9001/object-command",
                "workspace_path": "/workspace/private",
                "nested": {
                    "local_path": "/Users/backend/workdir",
                },
            },
        )
        owner_stdout = KnowledgeEvidenceArchive(
            id=_stable_uuid("owner-stdout"),
            tenant_id=owner_engagement.tenant_id,
            user_id=owner.id,
            engagement_id=owner_engagement.id,
            task_id=9001,
            source_execution_id=mixed_execution_id,
            source_artifact_id=_stable_uuid("owner-stdout-artifact"),
            storage_mode="object_ref",
            inline_excerpt=None,
            object_key="tenants/701/tasks/9001/object-stdout",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=2),
            lineage_snapshot={
                "source_tool": "nmap",
                "artifact_kind": "stdout",
                "runtime_path": "/workspace/private/stdout.txt",
            },
            archive_metadata={
                "type": "stdout",
                "host_path": "/var/lib/drowai/evidence/stdout",
                "download": {
                    "object_key": "tenants/701/tasks/9001/object-stdout",
                },
            },
        )
        owner_legacy = KnowledgeEvidenceArchive(
            id=_stable_uuid("owner-legacy"),
            tenant_id=owner_engagement.tenant_id,
            user_id=owner.id,
            engagement_id=owner_engagement.id,
            task_id=9002,
            source_execution_id=_stable_uuid("execution-legacy"),
            source_artifact_id=_stable_uuid("owner-legacy-artifact"),
            storage_mode="archived_file",
            inline_excerpt=None,
            archived_file_ref="/Users/backend/workspaces/evidence.txt",
            created_at=now - timedelta(minutes=1),
            lineage_snapshot={
                "source_tool": "nikto",
                "artifact_kind": "tool_file",
                "archived_file_ref": "/Users/backend/workspaces/evidence.txt",
            },
            archive_metadata={
                "type": "tool_file",
                "fallback_path": "/tmp/fallback/evidence.txt",
            },
        )
        foreign_row = KnowledgeEvidenceArchive(
            id=_stable_uuid("foreign-row"),
            tenant_id=foreign_engagement.tenant_id,
            user_id=foreign.id,
            engagement_id=foreign_engagement.id,
            task_id=9003,
            source_execution_id=_stable_uuid("execution-foreign"),
            source_artifact_id=_stable_uuid("foreign-artifact"),
            storage_mode="inline_excerpt",
            inline_excerpt="foreign",
            archived_file_ref=None,
            created_at=now - timedelta(minutes=4),
            lineage_snapshot={"source_tool": "foreign-tool", "artifact_kind": "stdout"},
            archive_metadata={"type": "stdout"},
        )
        db.add_all([owner_command, owner_stdout, owner_legacy, foreign_row])
        db.commit()

        seeded = {
            "owner_id": owner.id,
            "foreign_id": foreign.id,
            "mixed_execution_id": str(mixed_execution_id),
            "legacy_execution_id": str(owner_legacy.source_execution_id),
        }

    app = FastAPI()
    app.include_router(knowledge_routes.router)

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
            return SimpleNamespace(id=seeded["foreign_id"], username="foreign", is_active=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    app.dependency_overrides[knowledge_routes.get_db] = fake_get_db
    app.dependency_overrides[knowledge_routes.get_current_user] = fake_get_current_user

    def fake_get_tenant_request_context(request: Request):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ", 1)[1].strip() if auth_header.startswith("Bearer ") else ""
        if token == "owner-token":
            return SimpleNamespace(tenant_id=701, user_id=seeded["owner_id"], role="owner")
        if token == "foreign-token":
            return SimpleNamespace(tenant_id=702, user_id=seeded["foreign_id"], role="owner")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

    app.dependency_overrides[knowledge_routes.get_tenant_request_context] = (
        fake_get_tenant_request_context
    )

    client = TestClient(app)
    try:
        yield client, seeded
    finally:
        app.dependency_overrides.clear()
        client.close()
        engine.dispose()


def test_list_evidence_redacts_internal_paths_and_object_keys(api_client) -> None:
    client, seeded = api_client
    response = client.get(
        "/api/knowledge/evidence",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["total"] == 2
    assert len(payload["items"]) == 2

    forbidden_keys = {
        "workspace_path",
        "container_path",
        "source_path",
        "fallback_path",
        "archived_file_ref",
        "host_path",
        "absolute_path",
        "local_path",
        "runtime_path",
        "runner_path",
        "object_key",
    }
    for item in payload["items"]:
        _assert_no_redacted_keys(item, forbidden_keys)

    mixed = next(
        item for item in payload["items"] if item["source_execution_id"] == seeded["mixed_execution_id"]
    )
    execution_group = mixed["metadata"]["execution_group"]
    assert execution_group["member_count"] == 2
    assert len(execution_group["members"]) == 2

    legacy = next(
        item for item in payload["items"] if item["source_execution_id"] == seeded["legacy_execution_id"]
    )
    assert legacy["storage_mode"] == "archived_file"
