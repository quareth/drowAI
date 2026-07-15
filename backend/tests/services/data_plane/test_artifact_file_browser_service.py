"""Tests for artifact-metadata-backed task file browsing in runner cloud mode.

Scope:
- Validates tree/search/content/download behavior from data-plane artifact rows.
- Verifies per-file and total ZIP bounds for multi-file downloads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config.data_plane import DataPlaneConfig
from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.data_plane.artifact_file_browser_service import ArtifactFileBrowserService


class _StubObjectStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = dict(payloads)

    def put_bytes(self, object_key: str, data: bytes, *, content_type: str | None = None, metadata=None):
        raise NotImplementedError

    def read_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        payload = self._payloads.get(object_key)
        if payload is None:
            raise FileNotFoundError(object_key)
        if max_bytes is None:
            return payload
        return payload[:max_bytes]

    def delete_object(self, object_key: str) -> bool:
        raise NotImplementedError

    def create_signed_upload(self, object_key: str, *, content_type: str | None = None, metadata=None):
        raise NotImplementedError

    def create_signed_download(self, object_key: str, *, response_filename: str | None = None):
        raise NotImplementedError

    def head_object(self, object_key: str):
        raise NotImplementedError


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _build_data_plane_config(*, root: Path, max_file_bytes: int, max_zip_bytes: int) -> DataPlaneConfig:
    return DataPlaneConfig(
        object_store_backend="local",
        local_object_store_root=root,
        object_store_bucket=None,
        object_store_prefix="",
        signed_upload_ttl_seconds=900,
        signed_download_ttl_seconds=900,
        max_artifact_size_bytes=max_file_bytes,
        max_manifest_items=256,
        max_zip_download_size_bytes=max_zip_bytes,
    )


def _seed_task(db: Session, *, username: str = "file-browser-user", tenant_id: int = 1) -> Task:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=tenant_id, name="task-file-browser")
    db.add(task)
    db.flush()
    return task


def _seed_execution(db: Session, *, task_id: int, tenant_id: int = 1):
    repository = ToolExecutionRepository(db)
    return repository.create(
        task_id=task_id,
        tenant_id=tenant_id,
        tool_name="shell.exec",
        tool_arguments={"command": "ls"},
        agent_path="runner.tool_command",
        status="success",
        started_at=datetime.now(UTC),
        execution_transport="runner_control_channel",
    )


def test_tree_search_and_pending_state_come_from_artifact_metadata(tmp_path: Path) -> None:
    db = _build_session()
    try:
        task = _seed_task(db, username="file-browser-tree")
        execution = _seed_execution(db, task_id=task.id)
        repository = ExecutionArtifactRepository(db)
        repository.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "/workspace/reports/ready.txt",
                    "object_key": "tenants/1/tasks/1/reports/ready.txt",
                    "upload_status": "ready",
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "reports/pending.txt",
                    "upload_status": "upload_pending",
                    "byte_size": 8,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        service = ArtifactFileBrowserService(
            db,
            object_store=_StubObjectStore({"tenants/1/tasks/1/reports/ready.txt": b"ready"}),
            data_plane_config=_build_data_plane_config(
                root=tmp_path / "object-store",
                max_file_bytes=1024,
                max_zip_bytes=4096,
            ),
        )

        tree = service.get_directory_tree(tenant_id=task.tenant_id, task_id=task.id, path="/reports")
        assert tree["path"] == "/reports"
        files = {child["name"]: child for child in tree["children"]}
        assert files["ready.txt"]["content_availability"] == "available_object"
        assert files["pending.txt"]["content_availability"] == "upload_pending"
        assert "tenants/1/tasks/1/reports/ready.txt" not in str(tree)

        search = service.search_files(
            tenant_id=task.tenant_id,
            task_id=task.id,
            query="pending",
            path="/reports",
        )
        assert search["total_count"] == 1
        assert search["results"][0]["path"] == "/reports/pending.txt"
    finally:
        db.close()


def test_content_preview_and_single_download_are_object_backed(tmp_path: Path) -> None:
    db = _build_session()
    try:
        task = _seed_task(db, username="file-browser-content")
        execution = _seed_execution(db, task_id=task.id)
        repository = ExecutionArtifactRepository(db)
        artifact = repository.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "reports/output.json",
                    "object_key": "tenants/1/tasks/2/reports/output.json",
                    "upload_status": "ready",
                    "byte_size": 33,
                    "mime_type": "application/json",
                    "is_text": True,
                }
            ]
        )[0]
        db.commit()

        object_payload = b'{"payload":"<script>alert(1)</script>"}'
        service = ArtifactFileBrowserService(
            db,
            object_store=_StubObjectStore({str(artifact.object_key): object_payload}),
            data_plane_config=_build_data_plane_config(
                root=tmp_path / "object-store",
                max_file_bytes=1024,
                max_zip_bytes=4096,
            ),
        )

        content = service.get_file_content(
            tenant_id=task.tenant_id,
            task_id=task.id,
            path="/reports/output.json",
        )
        assert content["path"] == "/reports/output.json"
        assert content["preview_type"] == "json"
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content["content"]

        download_path = service.resolve_download_path(
            tenant_id=task.tenant_id,
            task_id=task.id,
            path="reports/output.json",
        )
        try:
            assert download_path.read_bytes() == object_payload
            assert "object-store" not in str(download_path)
        finally:
            download_path.unlink(missing_ok=True)
    finally:
        db.close()


def test_zip_download_enforces_per_file_and_total_size_limits(tmp_path: Path) -> None:
    db = _build_session()
    try:
        task = _seed_task(db, username="file-browser-zip")
        execution = _seed_execution(db, task_id=task.id)
        repository = ExecutionArtifactRepository(db)
        repository.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "reports/one.txt",
                    "object_key": "obj/one",
                    "upload_status": "ready",
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "reports/two.txt",
                    "object_key": "obj/two",
                    "upload_status": "ready",
                    "byte_size": 4,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        per_file_service = ArtifactFileBrowserService(
            db,
            object_store=_StubObjectStore({"obj/one": b"123456", "obj/two": b"abcd"}),
            data_plane_config=_build_data_plane_config(
                root=tmp_path / "object-store-per-file",
                max_file_bytes=4,
                max_zip_bytes=100,
            ),
        )
        with pytest.raises(ValueError, match="per-file"):
            per_file_service.create_zip_archive(
                tenant_id=task.tenant_id,
                task_id=task.id,
                file_paths=["/reports/one.txt"],
            )

        total_service = ArtifactFileBrowserService(
            db,
            object_store=_StubObjectStore({"obj/one": b"1234", "obj/two": b"abcd"}),
            data_plane_config=_build_data_plane_config(
                root=tmp_path / "object-store-total",
                max_file_bytes=8,
                max_zip_bytes=7,
            ),
        )
        with pytest.raises(ValueError, match="total ZIP"):
            total_service.create_zip_archive(
                tenant_id=task.tenant_id,
                task_id=task.id,
                file_paths=["/reports"],
            )
    finally:
        db.close()


def test_download_fails_when_tenant_scope_does_not_match_task(tmp_path: Path) -> None:
    db = _build_session()
    try:
        tenant_a_task = _seed_task(db, username="file-browser-tenant-a", tenant_id=101)
        tenant_b_task = _seed_task(db, username="file-browser-tenant-b", tenant_id=202)
        execution = _seed_execution(db, task_id=tenant_b_task.id, tenant_id=tenant_b_task.tenant_id)
        repository = ExecutionArtifactRepository(db)
        repository.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": tenant_b_task.id,
                    "tenant_id": tenant_b_task.tenant_id,
                    "artifact_kind": "tool_file",
                    "relative_path": "reports/private.txt",
                    "object_key": "tenants/202/tasks/private.txt",
                    "upload_status": "ready",
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        service = ArtifactFileBrowserService(
            db,
            object_store=_StubObjectStore({"tenants/202/tasks/private.txt": b"private"}),
            data_plane_config=_build_data_plane_config(
                root=tmp_path / "object-store-mismatch",
                max_file_bytes=1024,
                max_zip_bytes=4096,
            ),
        )

        with pytest.raises(FileNotFoundError):
            service.resolve_download_path(
                tenant_id=tenant_a_task.tenant_id,
                task_id=tenant_b_task.id,
                path="/reports/private.txt",
            )
    finally:
        db.close()
