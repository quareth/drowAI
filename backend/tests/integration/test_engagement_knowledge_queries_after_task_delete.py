"""Integration tests for engagement query durability after task delete.

This module verifies engagement-scoped APIs continue to return durable
knowledge after a contributing task is deleted, including evidence lineage
identifiers when available."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid as uuid_lib

import pytest
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeAsset, KnowledgeEvidenceArchive, KnowledgeFinding, KnowledgeIngestionRun, KnowledgeObservation, KnowledgeRelationship, KnowledgeService
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.routers import engagement_knowledge as engagement_routes
from backend.services.task.cleanup_service import TaskCleanupService


def _build_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db, session_factory


def _seed_runner_control_durable_fixture(db):
    now = datetime(2026, 3, 8, 9, 0, 0, tzinfo=timezone.utc)
    user = User(username=f"runner-control-delete-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()

    engagement = Engagement(user_id=user.id, name="Runner Control Delete Survival", status="active")
    db.add(engagement)
    db.flush()

    task = Task(user_id=user.id, engagement_id=engagement.id, name="Runner Control Durable Task")
    db.add(task)
    db.flush()

    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "collect"},
        agent_path="langgraph",
        status="success",
        started_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=9),
        duration_ms=1000,
    )
    db.add(execution)
    db.flush()

    artifact = ExecutionArtifact(
        id=uuid_lib.uuid4(),
        execution_id=execution.id,
        task_id=task.id,
        artifact_kind="file",
        source_path=None,
        relative_path="artifacts/runner-control-evidence.bin",
        content_text=None,
        content_sha256="c" * 64,
        byte_size=4,
        mime_type="application/octet-stream",
        is_text=False,
    )
    db.add(artifact)
    db.flush()

    run = KnowledgeIngestionRun(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        extractor_family="knowledge.integration_test",
        extractor_version="1.0",
        status="succeeded",
    )
    db.add(run)
    db.flush()

    observation = KnowledgeObservation(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        ingestion_run_id=run.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        observation_type="network.open_port",
        subject_type="service",
        subject_key="service.socket:10.0.0.10/tcp/443",
        assertion_level="observed",
        dedupe_key="network.open_port::service.socket:10.0.0.10/tcp/443",
        payload={"port": 443, "protocol": "tcp"},
        observed_at=now - timedelta(minutes=8),
    )
    db.add(observation)

    asset = KnowledgeAsset(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        engagement_id=engagement.id,
        asset_key="host.ip:10.0.0.10",
        asset_type="host.ip",
        display_name="10.0.0.10",
        ip_address="10.0.0.10",
        hostname=None,
        status="up",
        first_seen_at=now - timedelta(days=1),
        last_seen_at=now - timedelta(minutes=4),
        max_confidence="high",
        asset_metadata={"state": {"host_status": "up"}},
    )
    db.add(asset)
    db.flush()

    service = KnowledgeService(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        engagement_id=engagement.id,
        service_key="service.socket:10.0.0.10/tcp/443",
        asset_id=asset.id,
        protocol="tcp",
        port=443,
        service_name="https",
        product="nginx",
        version="1.25",
        status="open",
        first_seen_at=now - timedelta(hours=3),
        last_seen_at=now - timedelta(minutes=3),
        service_metadata={"state": {"service_name": "https"}},
    )
    db.add(service)
    db.flush()

    evidence_id = uuid_lib.uuid4()
    evidence_archive = KnowledgeEvidenceArchive(
        id=evidence_id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        source_artifact_id=artifact.id,
        storage_mode="inline_excerpt",
        inline_excerpt="Durable excerpt line one\nline two",
        archived_file_ref=None,
        lineage_snapshot={"source_tool": "nmap"},
        archive_metadata={"type": "terminal"},
    )
    db.add(evidence_archive)

    finding = KnowledgeFinding(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        engagement_id=engagement.id,
        finding_key="finding.vulnerability:host.ip:10.0.0.10:openssl-cve",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key="host.ip:10.0.0.10",
        asset_id=asset.id,
        service_id=service.id,
        title="OpenSSL vulnerability",
        severity="critical",
        status="open",
        assertion_level="observed",
        confidence="high",
        first_seen_at=now - timedelta(hours=1),
        last_seen_at=now - timedelta(minutes=2),
        evidence_summary={"evidence_refs": [{"evidence_archive_id": str(evidence_id)}]},
        finding_metadata={"source_tool": "nmap"},
    )
    db.add(finding)

    relationship = KnowledgeRelationship(
        id=uuid_lib.uuid4(),
        user_id=user.id,
        engagement_id=engagement.id,
        relationship_key="relationship.edge:host.ip:10.0.0.10:exposes:service.socket:10.0.0.10/tcp/443",
        source_subject_key="host.ip:10.0.0.10",
        relationship_type="exposes",
        target_subject_key="service.socket:10.0.0.10/tcp/443",
        confidence="high",
        first_seen_at=now - timedelta(minutes=30),
        last_seen_at=now - timedelta(minutes=2),
        relationship_metadata={"source": "projection"},
    )
    db.add(relationship)

    db.commit()
    return {
        "user": user,
        "engagement": engagement,
        "task": task,
        "execution": execution,
        "artifact": artifact,
        "evidence_id": str(evidence_id),
    }


@pytest.mark.asyncio
async def test_runner_control_engagement_queries_survive_task_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))
    engine, db, session_factory = _build_session()
    try:
        seeded = _seed_runner_control_durable_fixture(db)
        task = seeded["task"]
        user = seeded["user"]
        engagement = seeded["engagement"]
        task_id = int(task.id)
        user_id = int(user.id)
        engagement_id = int(engagement.id)
        execution_id = str(seeded["execution"].id)
        artifact_id = str(seeded["artifact"].id)

        workspace = WorkspaceConfig.get_task_workspace_path(task_id)
        workspace_artifacts = workspace / "artifacts"
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        (workspace_artifacts / "runner-control-evidence.bin").write_bytes(b"\x01\x02\x03\x04")

        cleanup_service = TaskCleanupService(db)
        monkeypatch.setattr(
            "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
            lambda db, task_id, tenant_id: task,
        )

        class _FakeRetirementService:
            async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
                assert task_id == int(task.id)
                assert engagement_id == int(engagement.id)
                return SimpleNamespace(success=True, message="retired")

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.TaskRetirementService",
            _FakeRetirementService,
        )

        delete_result = await cleanup_service.delete_task(
            task_id=task_id,
            user_id=user_id,
            tenant_id=int(task.tenant_id),
        )
        assert delete_result["message"] == "Task and container deleted successfully"

        remaining_task = db.execute(text("SELECT COUNT(*) FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
        assert remaining_task == 0

        app = FastAPI()
        app.include_router(engagement_routes.router)

        def _fake_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        def _fake_current_user(request: Request):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                )
            token = auth_header.split(" ", 1)[1].strip()
            if token == "owner-token":
                return SimpleNamespace(id=user_id, username="owner", is_active=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            )

        app.dependency_overrides[engagement_routes.get_db] = _fake_get_db
        app.dependency_overrides[engagement_routes.get_current_user] = _fake_current_user

        client = TestClient(app)
        try:
            headers = {"Authorization": "Bearer owner-token"}

            summary_resp = client.get(f"/api/engagements/{engagement_id}/summary", headers=headers)
            assert summary_resp.status_code == 200, summary_resp.text
            assert summary_resp.json()["open_findings_total"] >= 1

            findings_resp = client.get(f"/api/engagements/{engagement_id}/findings", headers=headers)
            assert findings_resp.status_code == 200, findings_resp.text
            findings_payload = findings_resp.json()
            assert findings_payload["total"] >= 1

            evidence_resp = client.get(f"/api/engagements/{engagement_id}/evidence", headers=headers)
            assert evidence_resp.status_code == 200, evidence_resp.text
            evidence_payload = evidence_resp.json()
            assert evidence_payload["total"] >= 1

            evidence_item = next(
                (row for row in evidence_payload["items"] if row.get("id") == seeded["evidence_id"]),
                None,
            )
            assert evidence_item is not None
            assert evidence_item["task_id"] == task_id
            assert evidence_item["source_execution_id"] == execution_id
            assert evidence_item["source_artifact_id"] == artifact_id
        finally:
            client.close()
            app.dependency_overrides.clear()
    finally:
        db.close()
        engine.dispose()
