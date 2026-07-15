"""Tests for ExecutionArtifactRepository batch insert and query behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_and_task(db, *, tenant_id: int = 1) -> Task:
    user = User(username=f"artifact-repo-user-{tenant_id}-{uuid.uuid4().hex[:8]}", password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=tenant_id, name="artifact-repo-task")
    db.add(task)
    db.flush()
    return task


def test_create_batch_inserts_multiple_artifacts() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task = _seed_user_and_task(db)
        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo hello"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )

        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "hello",
                    "content_sha256": artifact_repo.compute_content_hash("hello"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stderr",
                    "content_text": "warn",
                    "content_sha256": artifact_repo.compute_content_hash("warn"),
                    "byte_size": 4,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        assert len(created) == 2
        assert all(item.id is not None for item in created)
        by_execution = artifact_repo.get_by_execution(execution.id)
        assert len(by_execution) == 2
    finally:
        db.close()
        engine.dispose()


def test_get_by_task_with_kind_filter() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task = _seed_user_and_task(db)
        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo kinds"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "a",
                    "content_sha256": artifact_repo.compute_content_hash("a"),
                    "byte_size": 1,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "tmp/out.txt",
                    "content_sha256": artifact_repo.compute_content_hash("b"),
                    "byte_size": 1,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        filtered = artifact_repo.get_by_task(task_id=task.id, artifact_kind="tool_file")
        assert len(filtered) == 1
        assert filtered[0].artifact_kind == "tool_file"
    finally:
        db.close()
        engine.dispose()


def test_compute_content_hash_supports_text_and_bytes() -> None:
    hash_from_text = ExecutionArtifactRepository.compute_content_hash("hello")
    hash_from_bytes = ExecutionArtifactRepository.compute_content_hash(b"hello")
    assert hash_from_text == hash_from_bytes
    assert len(hash_from_text) == 64


def test_get_by_id_returns_artifact() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task = _seed_user_and_task(db)
        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo artifact id"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "id-test",
                    "content_sha256": artifact_repo.compute_content_hash("id-test"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )
        db.commit()

        fetched = artifact_repo.get_by_id(created[0].id)
        assert fetched is not None
        assert fetched.id == created[0].id
    finally:
        db.close()
        engine.dispose()


def test_create_batch_sets_tenant_id_from_execution_context() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task = _seed_user_and_task(db, tenant_id=73)
        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tenant"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )

        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "tenant test",
                    "content_sha256": artifact_repo.compute_content_hash("tenant test"),
                    "byte_size": 11,
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )
        db.commit()

        assert created[0].tenant_id == 73
    finally:
        db.close()
        engine.dispose()


def test_get_by_tenant_task_artifact_id_denies_cross_tenant_lookup() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task = _seed_user_and_task(db, tenant_id=45)
        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo scoped"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "scoped",
                    "content_sha256": artifact_repo.compute_content_hash("scoped"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        owner = artifact_repo.get_by_tenant_task_artifact_id(
            tenant_id=45,
            task_id=task.id,
            artifact_id=created[0].id,
        )
        cross_tenant = artifact_repo.get_by_tenant_task_artifact_id(
            tenant_id=99,
            task_id=task.id,
            artifact_id=created[0].id,
        )

        assert owner is not None
        assert owner.id == created[0].id
        assert cross_tenant is None
    finally:
        db.close()
        engine.dispose()


def test_list_by_tenant_task_filters_out_foreign_tenant_artifacts() -> None:
    engine, db = _build_session()
    try:
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        task_a = _seed_user_and_task(db, tenant_id=11)
        task_b = _seed_user_and_task(db, tenant_id=12)
        execution_a = execution_repo.create(
            task_id=task_a.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo a"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        execution_b = execution_repo.create(
            task_id=task_b.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo b"},
            agent_path="langgraph",
            status="started",
            started_at=datetime.now(timezone.utc),
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": execution_a.id,
                    "task_id": task_a.id,
                    "artifact_kind": "stdout",
                    "content_sha256": artifact_repo.compute_content_hash("a"),
                    "byte_size": 1,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution_b.id,
                    "task_id": task_b.id,
                    "artifact_kind": "stdout",
                    "content_sha256": artifact_repo.compute_content_hash("b"),
                    "byte_size": 1,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        tenant_a_rows = artifact_repo.list_by_tenant_task(tenant_id=11, task_id=task_a.id)
        tenant_b_rows = artifact_repo.list_by_tenant_task(tenant_id=12, task_id=task_b.id)

        assert len(tenant_a_rows) == 1
        assert len(tenant_b_rows) == 1
        assert tenant_a_rows[0].task_id == task_a.id
        assert tenant_b_rows[0].task_id == task_b.id
    finally:
        db.close()
        engine.dispose()
