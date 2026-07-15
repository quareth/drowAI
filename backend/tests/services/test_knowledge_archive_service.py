"""Tests for archive policy decisions and minimal lineage persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.knowledge.archive_service import KnowledgeArchiveService
from backend.services.knowledge.evidence_storage_service import EvidenceStorageService
from backend.services.data_plane.local_object_store import LocalObjectStore


@pytest.fixture(autouse=True)
def _isolate_durable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_engagement_task(
    db,
    *,
    tenant_id: int = 1,
    runtime_placement_mode: str = "local",
):
    db.execute(
        text(
            "INSERT OR IGNORE INTO tenants (id, slug, name, created_at) "
            "VALUES (:id, :slug, :name, CURRENT_TIMESTAMP)"
        ),
        {"id": int(tenant_id), "slug": f"tenant-{tenant_id}", "name": f"Tenant {tenant_id}"},
    )
    user = User(username="knowledge-archive-user", password="secret")
    db.add(user)
    db.flush()
    engagement = Engagement(user_id=user.id, tenant_id=tenant_id, name="Archive Engagement", status="active")
    db.add(engagement)
    db.flush()
    task = Task(
        user_id=user.id,
        engagement_id=engagement.id,
        tenant_id=tenant_id,
        name="Archive Task",
        runtime_placement_mode=runtime_placement_mode,
    )
    db.add(task)
    db.flush()
    return user, engagement, task


def _seed_execution_with_artifact(
    db,
    *,
    task_id: int,
    artifact_kind: str,
    content_text: str | None,
    is_text: bool,
    byte_size: int,
    source_path: str | None = None,
    relative_path: str | None = None,
    upload_status: str = "inline",
    object_key: str | None = None,
) -> tuple[str, str]:
    task_tenant_id = db.execute(
        select(Task.tenant_id).where(Task.id == int(task_id))
    ).scalar_one()
    execution = ToolExecution(
        id=uuid_lib.uuid4(),
        tenant_id=int(task_tenant_id),
        task_id=task_id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo test"},
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
        tenant_id=int(task_tenant_id),
        task_id=task_id,
        artifact_kind=artifact_kind,
        content_text=content_text,
        content_sha256="a" * 64,
        byte_size=byte_size,
        mime_type="text/plain" if is_text else "application/octet-stream",
        is_text=is_text,
        source_path=source_path,
        relative_path=relative_path,
        upload_status=upload_status,
        object_key=object_key,
        storage_backend="local" if object_key else None,
    )
    db.add(artifact)
    db.flush()
    return str(execution.id), str(artifact.id)


def test_archive_policy_uses_inline_excerpt_for_small_high_value_text() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="stdout",
            content_text="small evidence text",
            is_text=True,
            byte_size=256,
        )
        service = KnowledgeArchiveService(db)

        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=False,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "inline_excerpt"
        assert archived.inline_excerpt is not None
        assert archived.archived_file_ref is None
    finally:
        db.close()
        engine.dispose()


def test_archive_service_sets_tenant_id_from_engagement_context() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db, tenant_id=88)
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="stdout",
            content_text="tenant evidence",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeArchiveService(db)

        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=False,
        )

        assert len(rows) == 1
        assert rows[0].tenant_id == 88
    finally:
        db.close()
        engine.dispose()


def test_archive_policy_uses_archived_file_for_large_text_when_delete_survival_required() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="stdout",
            content_text="x" * 20000,
            is_text=True,
            byte_size=20000,
        )
        service = KnowledgeArchiveService(db)

        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "archived_file"
        assert archived.inline_excerpt is not None
        assert archived.archived_file_ref is not None
    finally:
        db.close()
        engine.dispose()


def test_archive_policy_uses_metadata_only_for_binary_when_delete_survival_not_required() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=4096,
        )
        service = KnowledgeArchiveService(db)

        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=False,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "metadata_only"
        assert archived.inline_excerpt is None
        assert archived.archived_file_ref is None
    finally:
        db.close()
        engine.dispose()


def test_archive_rows_keep_minimal_lineage_without_cloning_artifact_schema() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        execution_id, artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="command",
            content_text="nmap -sV 10.0.0.5",
            is_text=True,
            byte_size=64,
        )
        service = KnowledgeArchiveService(db)

        service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=False,
        )
        persisted = (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.source_artifact_id == artifact_id)
            .one()
        )

        assert persisted.storage_mode in {"inline_excerpt", "archived_file", "metadata_only"}
        assert persisted.lineage_snapshot["execution_id"] == execution_id
        assert persisted.lineage_snapshot["artifact_id"] == artifact_id
        assert "source_path" not in persisted.lineage_snapshot
        assert "fallback_path" not in persisted.lineage_snapshot
    finally:
        db.close()
        engine.dispose()


def test_storage_mode_normalization_keeps_object_ref_and_legacy_archived_file() -> None:
    assert KnowledgeArchiveService.normalize_storage_mode("object_ref") == "object_ref"
    assert KnowledgeArchiveService.normalize_storage_mode("archived_file") == "archived_file"


def test_archive_service_writes_archived_file_to_engagement_owned_durable_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(WorkspaceConfig, "get_project_root", staticmethod(lambda: tmp_path))
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        workspace = WorkspaceConfig.get_task_workspace_path(task.id)
        workspace.mkdir(parents=True, exist_ok=True)
        source_file = workspace / "artifacts" / "capture.bin"
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes(b"\x00\x01\x02")

        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=3,
            source_path=str(source_file),
            relative_path="artifacts/capture.bin",
        )
        service = KnowledgeArchiveService(db)
        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "archived_file"
        assert archived.archived_file_ref is not None
        archived_path = Path(archived.archived_file_ref)
        assert archived_path.exists()
        assert str(archived_path).startswith(
            str(WorkspaceConfig.get_engagement_evidence_path(engagement.id))
        )
        assert "task-" not in str(archived_path)
    finally:
        db.close()
        engine.dispose()


def test_runner_delete_survival_archives_use_object_ref_not_archived_file(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db, runtime_placement_mode="runner")
        source_key = (
            f"tenants/{engagement.tenant_id}/tasks/{task.id}/executions/runner/artifacts/"
            "scan-report/report.bin"
        )
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=3,
            upload_status="ready",
            object_key=source_key,
            relative_path="reports/report.bin",
        )

        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        payload = b"\x01\x02\x03"
        object_store.put_bytes(source_key, payload, content_type="application/octet-stream")
        service = KnowledgeArchiveService(
            db,
            evidence_storage_service=EvidenceStorageService(object_store=object_store),
        )
        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "object_ref"
        assert archived.archived_file_ref is None
        assert archived.object_key is not None
        assert archived.object_key != source_key
        assert archived.byte_size == len(payload)
        assert archived.content_sha256 == "039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81"
        assert archived.mime_type == "application/octet-stream"
        assert object_store.read_bytes(str(archived.object_key)) == payload
    finally:
        db.close()
        engine.dispose()


def test_runner_delete_survival_does_not_create_archived_file_ref_when_object_missing(
    tmp_path: Path,
) -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db, runtime_placement_mode="runner")
        source_key = f"tenants/{engagement.tenant_id}/tasks/{task.id}/missing/object.bin"
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=3,
            upload_status="ready",
            object_key=source_key,
            relative_path="reports/object.bin",
        )
        service = KnowledgeArchiveService(
            db,
            evidence_storage_service=EvidenceStorageService(
                object_store=LocalObjectStore(root_path=tmp_path / "object-store")
            ),
        )
        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "metadata_only"
        assert archived.archived_file_ref is None
        assert archived.object_key is None
    finally:
        db.close()
        engine.dispose()


def test_runner_delete_survival_without_data_plane_markers_stays_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db, runtime_placement_mode="runner")
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=16,
            upload_status="inline",
            object_key=None,
            source_path="/workspace/reports/local-only.bin",
            relative_path="reports/local-only.bin",
        )
        service = KnowledgeArchiveService(db)
        monkeypatch.setattr(
            service,
            "_read_runtime_artifact_bytes",
            lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("runner placement must not fall back to runtime workspace reads")
            ),
        )
        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "metadata_only"
        assert archived.archived_file_ref is None
        assert archived.object_key is None
    finally:
        db.close()
        engine.dispose()


def test_archive_service_rejects_outside_workspace_source_path_for_copy() -> None:
    engine, db = _build_session()
    try:
        _user, engagement, task = _seed_user_engagement_task(db)
        outside_file = WorkspaceConfig.get_project_root() / "outside.bin"
        outside_file.write_bytes(b"\x10\x20\x30")
        execution_id, _artifact_id = _seed_execution_with_artifact(
            db,
            task_id=task.id,
            artifact_kind="file",
            content_text=None,
            is_text=False,
            byte_size=3,
            source_path=str(outside_file),
            relative_path=None,
        )
        service = KnowledgeArchiveService(db)
        rows = service.archive_execution_artifacts(
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution_id,
            delete_survival_required=True,
        )

        assert len(rows) == 1
        archived = rows[0]
        assert archived.storage_mode == "archived_file"
        assert str(archived.archived_file_ref or "").startswith("pending://")
        evidence_path = WorkspaceConfig.get_engagement_evidence_path(engagement.id)
        evidence_files = list(evidence_path.glob("*"))
        assert evidence_files == []
    finally:
        db.close()
        engine.dispose()
