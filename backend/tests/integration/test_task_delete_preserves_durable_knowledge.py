"""Integration tests for task deletion and durable-knowledge decoupling behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid as uuid_lib

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.task.cleanup_service import TaskCleanupService


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    db.execute(text("PRAGMA foreign_keys=ON"))
    return engine, db


def _seed_task_with_execution_and_artifact(
    db,
    *,
    source_path: str | None,
    relative_path: str | None,
    content_text: str | None,
    is_text: bool,
    byte_size: int,
) -> tuple[User, Engagement, Task, str]:
    tenant_id = 1
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (:id, :slug, :name, CURRENT_TIMESTAMP)"
        ),
        {"id": tenant_id, "slug": "tenant-1", "name": "Tenant 1"},
    )
    user = User(username=f"delete-safe-user-{uuid_lib.uuid4()}", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=tenant_id, name="Delete-safe engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(user_id=user.id, engagement_id=engagement.id, tenant_id=tenant_id, name="Delete-safe task")
    db.add(task)
    db.flush()

    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=tenant_id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "run"},
        agent_path="langgraph",
        status="success",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(execution)
    db.flush()

    artifact = ExecutionArtifact(
        id=uuid_lib.uuid4(),
        execution_id=execution.id,
        tenant_id=tenant_id,
        task_id=task.id,
        artifact_kind="file",
        source_path=source_path,
        relative_path=relative_path,
        content_text=content_text,
        content_sha256="b" * 64,
        byte_size=byte_size,
        mime_type="application/octet-stream" if not is_text else "text/plain",
        is_text=is_text,
    )
    db.add(artifact)
    db.commit()
    return user, engagement, task, str(execution.id)


@pytest.mark.asyncio
async def test_task_delete_preserves_durable_knowledge_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))
    engine, db = _build_session()
    try:
        user, engagement, task, execution_id = _seed_task_with_execution_and_artifact(
            db,
            source_path=None,
            relative_path="artifacts/input-evidence.bin",
            content_text=None,
            is_text=False,
            byte_size=4,
        )
        workspace = WorkspaceConfig.get_task_workspace_path(task.id)
        workspace_artifacts = workspace / "artifacts"
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        (workspace_artifacts / "input-evidence.bin").write_bytes(b"\x01\x02\x03\x04")
        run = KnowledgeIngestionRun(
            id=uuid_lib.uuid4(),
            tenant_id=task.tenant_id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            extractor_family="knowledge.integration_test",
            extractor_version="1.0",
            status="succeeded",
        )
        db.add(run)
        db.flush()
        observation = KnowledgeObservation(
            id=uuid_lib.uuid4(),
            tenant_id=task.tenant_id,
            user_id=user.id,
            ingestion_run_id=run.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=execution_id,
            observation_type="network.open_port",
            subject_type="service",
            subject_key="tcp://10.0.0.5:80",
            assertion_level="observed",
            dedupe_key="network.open_port::tcp://10.0.0.5:80",
            payload={"port": 80, "protocol": "tcp"},
            observed_at=datetime.now(timezone.utc),
        )
        db.add(observation)
        artifact_id = db.execute(
            text("SELECT id FROM execution_artifacts WHERE execution_id = :execution_id"),
            {"execution_id": execution_id},
        ).scalar_one()
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid_lib.uuid4(),
                tenant_id=task.tenant_id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution_id,
                source_artifact_id=artifact_id,
                storage_mode="inline_excerpt",
                inline_excerpt="durable inline excerpt",
                lineage_snapshot={"artifact_id": str(artifact_id)},
            )
        )
        db.commit()
        service = TaskCleanupService(db)
        task_id = int(task.id)
        user_id = int(user.id)
        engagement_id = int(engagement.id)

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
            lambda db, task_id, tenant_id: task,
        )

        class _RetirementService:
            async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
                assert task_id == int(task.id)
                assert engagement_id == int(task.engagement_id)
                return type("RetirementResult", (), {"success": True, "message": "retired"})()

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.TaskRetirementService",
            _RetirementService,
        )

        result = await service.delete_task(task_id=task_id, user_id=user_id, tenant_id=int(task.tenant_id))
        assert result["message"] == "Task and container deleted successfully"

        remaining_task = db.execute(text("SELECT COUNT(*) FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
        remaining_runs = db.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_ingestion_runs "
                "WHERE engagement_id = :engagement_id AND source_execution_id = :execution_id"
            ),
            {"engagement_id": engagement_id, "execution_id": execution_id},
        ).scalar_one()
        remaining_observations = db.execute(
            text(
                "SELECT COUNT(*) FROM knowledge_observations "
                "WHERE engagement_id = :engagement_id AND source_execution_id = :execution_id"
            ),
            {"engagement_id": engagement_id, "execution_id": execution_id},
        ).scalar_one()

        assert remaining_task == 0
        assert remaining_runs >= 1
        assert remaining_observations >= 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_task_delete_blocks_when_durable_evidence_cannot_be_materialized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))
    engine, db = _build_session()
    try:
        user, _engagement, task, _execution_id = _seed_task_with_execution_and_artifact(
            db,
            source_path=None,
            relative_path=None,
            content_text=None,
            is_text=False,
            byte_size=4096,
        )
        service = TaskCleanupService(db)
        task_id = int(task.id)
        user_id = int(user.id)

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
            lambda db, task_id, tenant_id: task,
        )

        class _RetirementService:
            async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
                assert task_id == int(task.id)
                return type("RetirementResult", (), {"success": True, "message": "retired"})()

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.TaskRetirementService",
            _RetirementService,
        )

        with pytest.raises(HTTPException) as exc:
            await service.delete_task(task_id=task_id, user_id=user_id, tenant_id=int(task.tenant_id))
        assert exc.value.status_code == 409
        assert "delete blocked" in str(exc.value.detail).lower()

        remaining_task = db.execute(text("SELECT COUNT(*) FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()
        assert remaining_task == 1
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_task_delete_runtime_phase_failure_keeps_task_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))
    engine, db = _build_session()
    try:
        user, engagement, task, execution_id = _seed_task_with_execution_and_artifact(
            db,
            source_path=None,
            relative_path="artifacts/catchup.bin",
            content_text=None,
            is_text=False,
            byte_size=4,
        )
        workspace = WorkspaceConfig.get_task_workspace_path(task.id)
        workspace_artifacts = workspace / "artifacts"
        workspace_artifacts.mkdir(parents=True, exist_ok=True)
        (workspace_artifacts / "catchup.bin").write_bytes(b"\x01\x02\x03\x04")

        service = TaskCleanupService(db)
        task_id = int(task.id)
        user_id = int(user.id)

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
            lambda db, task_id, tenant_id: task,
        )

        class _RetirementService:
            async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
                assert task_id == int(task.id)
                return type("RetirementResult", (), {"success": False, "message": "runtime teardown failed"})()

        monkeypatch.setattr(
            "backend.services.task.cleanup_service.TaskRetirementService",
            _RetirementService,
        )
        monkeypatch.setattr(
            service,
            "_enforce_delete_safety_preflight",
            lambda **_kwargs: None,
        )

        with pytest.raises(HTTPException) as exc:
            await service.delete_task(task_id=task_id, user_id=user_id, tenant_id=int(task.tenant_id))
        assert exc.value.status_code == 500
        assert "runtime teardown failed" in str(exc.value.detail).lower()

        remaining_task = db.execute(text("SELECT COUNT(*) FROM tasks WHERE id = :id"), {"id": task_id}).scalar_one()

        assert remaining_task == 1
    finally:
        db.close()
        engine.dispose()
