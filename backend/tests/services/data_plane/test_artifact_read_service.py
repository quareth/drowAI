"""Tests for object-backed artifact text reads in the data plane.

Scope:
- Validates tenant/task/artifact scoping for object-backed reads.
- Confirms stable `not_available` outcomes without object-key/path leakage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.data_plane.artifact_read_service import ArtifactReadService


class _StubObjectStore:
    def __init__(self, *, data: bytes | None = None, should_raise: bool = False) -> None:
        self._data = data if data is not None else b""
        self._should_raise = should_raise

    def put_bytes(self, object_key: str, data: bytes, *, content_type: str | None = None, metadata=None):
        raise NotImplementedError

    def read_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        if self._should_raise:
            raise RuntimeError("backend path /tmp/internal/secret")
        if max_bytes is None:
            return self._data
        return self._data[:max_bytes]

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


def _seed_user_task(db: Session, *, username: str, task_name: str, tenant_id: int = 1) -> Task:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=tenant_id, name=task_name)
    db.add(task)
    db.flush()
    return task


def test_read_artifact_text_returns_ready_for_object_backed_text() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-1", task_name="obj-read-task-1")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat out.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
            execution_transport="runner_control_channel",
        )
        artifact = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/out.txt",
                    "object_key": "tenants/1/tasks/1/out.txt",
                    "upload_status": "ready",
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                }
            ]
        )[0]
        db.commit()

        service = ArtifactReadService(db, object_store=_StubObjectStore(data=b"hello-world"))
        result = service.read_artifact_text(
            task_id=task.id,
            artifact_id=str(artifact.id),
            max_bytes=5,
        )

        assert result.status == "ready"
        assert result.content == "hello"
        assert result.truncated is True
    finally:
        db.close()


def test_read_artifact_text_returns_not_available_for_binary_artifact() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-2", task_name="obj-read-task-2")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat screenshot.png"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifact = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/screenshot.png",
                    "object_key": "tenants/1/tasks/1/screenshot.png",
                    "upload_status": "ready",
                    "mime_type": "image/png",
                    "is_text": False,
                }
            ]
        )[0]
        db.commit()

        service = ArtifactReadService(db, object_store=_StubObjectStore(data=b"\x89PNG"))
        result = service.read_artifact_text(task_id=task.id, artifact_id=str(artifact.id))

        assert result.status == "not_available"
        assert result.reason == "not_text_artifact"
        assert result.content is None
    finally:
        db.close()


def test_read_artifact_text_enforces_tenant_scope() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-3", task_name="obj-read-task-3", tenant_id=2)
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tenant_id=2,
            tool_name="shell.exec",
            tool_arguments={"command": "cat private.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifact = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": 2,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/private.txt",
                    "object_key": "tenants/2/tasks/2/private.txt",
                    "upload_status": "ready",
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )[0]
        db.commit()

        service = ArtifactReadService(db, object_store=_StubObjectStore(data=b"secret"))
        denied = service.read_artifact_text(
            task_id=task.id,
            tenant_id=1,
            artifact_id=str(artifact.id),
        )
        allowed = service.read_artifact_text(
            task_id=task.id,
            tenant_id=2,
            artifact_id=str(artifact.id),
        )

        assert denied.status == "not_found"
        assert allowed.status == "ready"
        assert allowed.content == "secret"
    finally:
        db.close()


def test_read_artifact_text_without_tenant_uses_task_tenant_scope() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-3b", task_name="obj-read-task-3b", tenant_id=1)
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tenant_id=1,
            tool_name="shell.exec",
            tool_arguments={"command": "cat mismatch.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifact = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "tenant_id": 2,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/mismatch.txt",
                    "object_key": "tenants/2/tasks/2/mismatch.txt",
                    "upload_status": "ready",
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )[0]
        db.commit()

        service = ArtifactReadService(db, object_store=_StubObjectStore(data=b"secret"))
        result = service.read_artifact_text(
            task_id=task.id,
            artifact_id=str(artifact.id),
        )

        assert result.status == "not_found"
    finally:
        db.close()


def test_read_artifact_text_hides_object_key_and_paths_on_read_failure() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-4", task_name="obj-read-task-4")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat failed.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifact = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/failed.txt",
                    "object_key": "tenants/1/tasks/1/secret-object-key.txt",
                    "upload_status": "ready",
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )[0]
        db.commit()

        service = ArtifactReadService(db, object_store=_StubObjectStore(should_raise=True))
        result = service.read_artifact_text(
            task_id=task.id,
            artifact_id=str(artifact.id),
        )

        assert result.status == "not_available"
        assert result.reason == "object_read_failed"
        assert result.content is None
        assert "secret-object-key" not in str(result)
        assert "/tmp/internal/secret" not in str(result)
    finally:
        db.close()


def test_read_artifact_text_returns_not_found_for_invalid_uuid() -> None:
    db = _build_session()
    try:
        task = _seed_user_task(db, username="obj-read-user-5", task_name="obj-read-task-5")
        service = ArtifactReadService(db, object_store=_StubObjectStore(data=b"unused"))
        result = service.read_artifact_text(task_id=task.id, artifact_id=str(uuid4())[:-2])
        assert result.status == "not_found"
    finally:
        db.close()
