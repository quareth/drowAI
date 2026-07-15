"""Tests for ArtifactMemoryService contracts and task-scope enforcement.

These tests verify that the shared service boundary enforces task context,
delegates retrieval through provenance query helpers, and returns deterministic
typed payloads for search/read contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.data_plane.artifact_read_service import ArtifactObjectReadResult
from backend.services.data_plane.artifact_read_service import ArtifactReadService
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.artifact.memory_service import (
    ArtifactCatalogPage,
    ArtifactMemoryScopeError,
    ArtifactMemoryService,
    ArtifactReadRequest,
    ArtifactSearchFilters,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_and_task(
    db,
    *,
    username: str,
    task_name: str,
    runtime_placement_mode: str = "local",
) -> Task:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    task = Task(
        user_id=user.id,
        name=task_name,
        runtime_placement_mode=runtime_placement_mode,
    )
    db.add(task)
    db.flush()
    return task


def test_service_rejects_invalid_task_context() -> None:
    engine, db = _build_session()
    try:
        service = ArtifactMemoryService(db)
        with pytest.raises(ArtifactMemoryScopeError):
            service.search_task_artifacts(task_id=0, filters=ArtifactSearchFilters())
        with pytest.raises(ArtifactMemoryScopeError):
            service.read_task_artifact(
                task_id=0,
                artifact_id=str(uuid4()),
                request=ArtifactReadRequest(),
            )
    finally:
        db.close()
        engine.dispose()


def test_search_contract_returns_typed_and_deterministic_page() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-1", task_name="memory-task-1")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo memory"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-memory-1",
            conversation_id="conv-memory",
            turn_id="turn-1",
            turn_sequence=1,
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "memory-output",
                    "content_sha256": artifact_repo.compute_content_hash("memory-output"),
                    "byte_size": 13,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/memory.txt",
                    "content_sha256": artifact_repo.compute_content_hash("memory-file"),
                    "byte_size": 11,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        filters = ArtifactSearchFilters(artifact_kind="stdout", limit=10, offset=0)
        first = service.search_task_artifacts(task_id=task.id, filters=filters)
        second = service.search_task_artifacts(task_id=task.id, filters=filters)

        assert isinstance(first, ArtifactCatalogPage)
        assert first == second
        assert first.total == 1
        assert first.limit == 10
        assert first.offset == 0
        assert len(first.artifacts) == 1
        row = first.artifacts[0]
        assert row.artifact_kind == "stdout"
        assert row.task_id == task.id
        assert row.execution_id == str(execution.id)
        assert row.tool_call_id == "tc-memory-1"
        assert row.tool_name == "shell.exec"
        assert row.turn_id == "turn-1"
        assert row.turn_sequence == 1
        assert row.content_availability == "available_inline"
        assert row.label == "stdout from shell.exec (turn 1)"
    finally:
        db.close()
        engine.dispose()


def test_search_contract_supports_catalog_filters_and_query_match() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-1b", task_name="memory-task-1b")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        exec_a = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo one"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-filter-a",
            conversation_id="conv-filter",
            turn_id="turn-a",
            turn_sequence=2,
        )
        exec_b = execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={"path": "notes.txt"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-filter-b",
            conversation_id="conv-other",
            turn_id="turn-b",
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": exec_a.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "stdout-a",
                    "content_sha256": artifact_repo.compute_content_hash("stdout-a"),
                    "byte_size": 8,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "relative_path": "artifacts/stdout-a.txt",
                },
                {
                    "execution_id": exec_b.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "content_text": None,
                    "content_sha256": artifact_repo.compute_content_hash("tool-b"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "relative_path": "artifacts/report-b.txt",
                },
            ]
        )
        db.commit()

        only_shell = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(tool_name="shell.exec"),
        )
        only_exec_b = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(execution_id=str(exec_b.id)),
        )
        by_turn = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(turn_id="turn-a"),
        )
        by_conversation = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(conversation_id="conv-other"),
        )
        by_query_label = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(query="stdout from shell.exec (turn 2)"),
        )
        by_query_fallback_label = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(
                query=f"tool_file from filesystem.read_file (execution {str(exec_b.id)[:8]})"
            ),
        )
        by_query_path = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(query="report-b.txt"),
        )

        assert only_shell.total == 1
        assert only_shell.artifacts[0].tool_name == "shell.exec"

        assert only_exec_b.total == 1
        assert only_exec_b.artifacts[0].execution_id == str(exec_b.id)

        assert by_turn.total == 1
        assert by_turn.artifacts[0].turn_id == "turn-a"

        assert by_conversation.total == 1
        assert by_conversation.artifacts[0].execution_id == str(exec_b.id)

        assert by_query_label.total == 1
        assert by_query_label.artifacts[0].tool_call_id == "tc-filter-a"

        assert by_query_fallback_label.total == 1
        assert by_query_fallback_label.artifacts[0].execution_id == str(exec_b.id)

        assert by_query_path.total == 1
        assert by_query_path.artifacts[0].relative_path == "artifacts/report-b.txt"
    finally:
        db.close()
        engine.dispose()


def test_search_contract_uses_deterministic_tie_break_ordering() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-1c", task_name="memory-task-1c")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)
        fixed_time = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tie"},
            agent_path="langgraph",
            status="success",
            started_at=fixed_time,
            turn_sequence=4,
        )

        low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        high_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        artifact_repo.create_batch(
            [
                {
                    "id": low_id,
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "low",
                    "content_sha256": artifact_repo.compute_content_hash("low"),
                    "byte_size": 3,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "created_at": fixed_time,
                },
                {
                    "id": high_id,
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "high",
                    "content_sha256": artifact_repo.compute_content_hash("high"),
                    "byte_size": 4,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "created_at": fixed_time,
                },
            ]
        )
        db.commit()

        page = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(limit=10, offset=0),
        )
        assert page.total == 2
        assert [row.artifact_id for row in page.artifacts] == [str(high_id), str(low_id)]
    finally:
        db.close()
        engine.dispose()


def test_search_contract_applies_db_pagination_and_total() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-1d", task_name="memory-task-1d")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)
        fixed_time = datetime(2026, 3, 6, 11, 0, 0, tzinfo=timezone.utc)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo page"},
            agent_path="langgraph",
            status="success",
            started_at=fixed_time,
        )

        first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        third_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
        artifact_repo.create_batch(
            [
                {
                    "id": first_id,
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "first",
                    "content_sha256": artifact_repo.compute_content_hash("first"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "created_at": fixed_time,
                },
                {
                    "id": second_id,
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "second",
                    "content_sha256": artifact_repo.compute_content_hash("second"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "created_at": fixed_time,
                },
                {
                    "id": third_id,
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "third",
                    "content_sha256": artifact_repo.compute_content_hash("third"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "created_at": fixed_time,
                },
            ]
        )
        db.commit()

        page = service.search_task_artifacts(
            task_id=task.id,
            filters=ArtifactSearchFilters(limit=1, offset=1),
        )

        assert page.total == 3
        assert page.limit == 1
        assert page.offset == 1
        assert len(page.artifacts) == 1
        assert page.artifacts[0].artifact_id == str(second_id)
    finally:
        db.close()
        engine.dispose()


def test_task_has_persisted_artifacts_uses_task_scoped_existence_query() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-1e", task_name="memory-task-1e")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        assert service.task_has_persisted_artifacts(task_id=task.id) is False

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo exists"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "exists",
                    "content_sha256": artifact_repo.compute_content_hash("exists"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        assert service.task_has_persisted_artifacts(task_id=task.id) is True

        with pytest.raises(ArtifactMemoryScopeError):
            service.task_has_persisted_artifacts(task_id=0)
    finally:
        db.close()
        engine.dispose()


def test_read_contract_returns_ready_not_available_and_not_found_states() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-2", task_name="memory-task-2")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo read"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-memory-2",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "abcdef",
                    "content_sha256": artifact_repo.compute_content_hash("abcdef"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/no-inline.txt",
                    "content_sha256": artifact_repo.compute_content_hash("missing"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                },
            ]
        )
        db.commit()

        ready = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=4),
        )
        no_content = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[1].id),
            request=ArtifactReadRequest(),
        )
        not_found = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(uuid4()),
            request=ArtifactReadRequest(),
        )

        assert ready.status == "ready"
        assert ready.content == "abcd"
        assert ready.truncated is True
        assert ready.source == "inline_db"
        assert ready.artifact is not None
        assert ready.artifact.label.startswith("stdout from shell.exec")

        assert no_content.status == "not_available"
        assert no_content.content is None
        assert no_content.source == "none"

        assert not_found.status == "not_found"
        assert not_found.content is None
        assert not_found.artifact is None
    finally:
        db.close()
        engine.dispose()


def test_read_does_not_fallback_to_workspace_for_upload_pending_artifact() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-2b", task_name="memory-task-2b")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat artifacts/pending.txt"},
            agent_path="langgraph",
            status="pending",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-memory-2b",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/pending.txt",
                    "source_path": "/workspace/artifacts/pending.txt",
                    "content_sha256": artifact_repo.compute_content_hash("pending"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                    "upload_status": "upload_pending",
                },
            ]
        )
        db.commit()

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(),
        )

        assert result.status == "not_available"
        assert result.content is None
        assert result.source == "none"
    finally:
        db.close()
        engine.dispose()


def test_read_uses_object_store_when_inline_content_missing(tmp_path: Path) -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-2c", task_name="memory-task-2c")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat artifacts/object.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
            execution_transport="runner_control_channel",
            tool_call_id="tc-memory-2c",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/object.txt",
                    "object_key": "tenants/1/tasks/1/artifacts/object.txt",
                    "content_sha256": artifact_repo.compute_content_hash("object-content"),
                    "byte_size": 14,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                    "upload_status": "ready",
                },
            ]
        )
        db.commit()

        object_store = LocalObjectStore(root_path=tmp_path / "object-store")
        object_store.put_bytes(
            "tenants/1/tasks/1/artifacts/object.txt",
            b"object-content",
            content_type="text/plain",
        )
        read_service = ArtifactReadService(db, object_store=object_store)
        service = ArtifactMemoryService(db, object_read_service=read_service)

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=6),
        )

        assert result.status == "ready"
        assert result.source == "object_store"
        assert result.mode_used == "head"
        assert result.content == "object"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_runner_placement_read_never_falls_back_to_runtime_provider_when_object_unavailable() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(
            db,
            username="memory-user-2d",
            task_name="memory-task-2d",
            runtime_placement_mode="runner",
        )
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat artifacts/missing.txt"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-memory-2d",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/missing.txt",
                    "source_path": "/workspace/artifacts/missing.txt",
                    "object_key": "tenants/1/tasks/1/artifacts/missing.txt",
                    "content_sha256": artifact_repo.compute_content_hash("missing"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                    "upload_status": "ready",
                },
            ]
        )
        db.commit()

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("runtime provider fallback must not run for runner cloud artifact reads")

        service._read_runtime_artifact_text = _fail_if_called  # type: ignore[method-assign]
        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("failure_reason", "artifact_path"),
    [
        ("object_unavailable", "artifacts/missing-object-key.txt"),
        ("object_read_failed", "artifacts/object-read-failed.txt"),
        ("decode_failed", "artifacts/object-decode-failed.txt"),
    ],
)
def test_runner_cloud_object_read_failures_surface_not_available_availability(
    failure_reason: str,
    artifact_path: str,
) -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(
            db,
            username="memory-user-2e",
            task_name="memory-task-2e",
            runtime_placement_mode="runner",
        )
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": f"cat {artifact_path}"},
            agent_path="runner.tool_command",
            status="success",
            started_at=datetime.now(timezone.utc),
            execution_transport="runner_control_channel",
            tool_call_id="tc-memory-2e",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": artifact_path,
                    "object_key": "tenants/1/tasks/1/artifacts/failure.txt",
                    "content_sha256": artifact_repo.compute_content_hash("missing"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                    "upload_status": "ready",
                },
            ]
        )
        db.commit()

        class _StubObjectReadService:
            def read_artifact_text(self, **kwargs):
                del kwargs
                return ArtifactObjectReadResult(
                    status="not_available",
                    content=None,
                    truncated=False,
                    reason=failure_reason,
                )

        service = ArtifactMemoryService(db, object_read_service=_StubObjectReadService())
        service._read_runtime_artifact_text = lambda **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("runtime provider fallback must not run for runner cloud artifact reads")
        )

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
        assert result.content_availability == "not_available"
        assert result.artifact is not None
        assert result.artifact.content_availability == "not_available"
    finally:
        db.close()
        engine.dispose()


def test_read_falls_back_to_workspace_file_when_inline_content_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-3", task_name="memory-task-3")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        workspace_root = tmp_path / f"task-{task.id}"
        artifacts_dir = workspace_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "fallback.txt").write_text("0123456789abcdefghij", encoding="utf-8")
        monkeypatch.setattr(
            WorkspaceConfig,
            "get_task_workspace_path",
            staticmethod(lambda task_id: tmp_path / f"task-{task_id}"),
        )

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat artifacts/fallback.txt"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            workspace_path=str(workspace_root),
            tool_call_id="tc-memory-3",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/fallback.txt",
                    "content_sha256": artifact_repo.compute_content_hash("0123456789abcdefghij"),
                    "byte_size": 20,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                },
            ]
        )
        db.commit()

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=5),
        )

        assert result.status == "ready"
        assert result.source == "workspace_file"
        assert result.mode_used == "head"
        assert result.content == "01234"
        assert result.truncated is True
    finally:
        db.close()
        engine.dispose()


def test_read_ignores_poisoned_workspace_hint_even_with_task_named_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-3x", task_name="memory-task-3x")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        canonical_workspace = (tmp_path / "canonical" / f"task-{task.id}").resolve()
        poisoned_workspace = (tmp_path / "poison" / f"task-{task.id}").resolve()
        canonical_workspace.mkdir(parents=True, exist_ok=True)
        poisoned_workspace.mkdir(parents=True, exist_ok=True)

        poisoned_artifacts = poisoned_workspace / "artifacts"
        poisoned_artifacts.mkdir(parents=True, exist_ok=True)
        (poisoned_artifacts / "poisoned.txt").write_text("poisoned-content", encoding="utf-8")

        monkeypatch.setattr(
            WorkspaceConfig,
            "get_task_workspace_path",
            staticmethod(lambda task_id: tmp_path / "canonical" / f"task-{task_id}"),
        )

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat artifacts/poisoned.txt"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            # Deliberately poisoned: same basename task-{id}, different root.
            workspace_path=str(poisoned_workspace),
            tool_call_id="tc-memory-3x",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/poisoned.txt",
                    "content_sha256": artifact_repo.compute_content_hash("poisoned-content"),
                    "byte_size": 15,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                },
            ]
        )
        db.commit()

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=32),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
    finally:
        db.close()
        engine.dispose()


def test_read_rejects_workspace_escape_candidate_paths(tmp_path: Path) -> None:
    """Workspace fallback must reject traversal paths that escape task workspace roots."""
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-3b", task_name="memory-task-3b")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        workspace_root = tmp_path / f"task-{task.id}"
        workspace_root.mkdir(parents=True, exist_ok=True)
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "escape.txt").write_text("outside-task-workspace", encoding="utf-8")

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "cat ../outside/escape.txt"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            workspace_path=str(workspace_root),
            tool_call_id="tc-memory-3b",
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "../outside/escape.txt",
                    "content_sha256": artifact_repo.compute_content_hash("outside-task-workspace"),
                    "byte_size": 22,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "content_text": None,
                },
            ]
        )
        db.commit()

        result = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=32),
        )

        assert result.status == "not_available"
        assert result.source == "none"
        assert result.content is None
    finally:
        db.close()
        engine.dispose()


def test_read_rejects_cross_task_artifact_access() -> None:
    engine, db = _build_session()
    try:
        owner_task = _seed_user_and_task(db, username="memory-user-4a", task_name="memory-task-4a")
        other_task = _seed_user_and_task(db, username="memory-user-4b", task_name="memory-task-4b")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        owner_execution = execution_repo.create(
            task_id=owner_task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo owner"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        other_execution = execution_repo.create(
            task_id=other_task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo other"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": owner_execution.id,
                    "task_id": owner_task.id,
                    "artifact_kind": "stdout",
                    "content_text": "owner-only",
                    "content_sha256": artifact_repo.compute_content_hash("owner-only"),
                    "byte_size": 10,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": other_execution.id,
                    "task_id": other_task.id,
                    "artifact_kind": "stdout",
                    "content_text": "other-only",
                    "content_sha256": artifact_repo.compute_content_hash("other-only"),
                    "byte_size": 10,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        result = service.read_task_artifact(
            task_id=owner_task.id,
            artifact_id=str(created[1].id),
            request=ArtifactReadRequest(),
        )
        assert result.status == "not_found"
        assert result.content is None
        assert result.artifact is None
    finally:
        db.close()
        engine.dispose()


def test_read_modes_auto_and_full_remain_bounded() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="memory-user-5", task_name="memory-task-5")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        service = ArtifactMemoryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo long"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        long_text = "A" * 120
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": long_text,
                    "content_sha256": artifact_repo.compute_content_hash(long_text),
                    "byte_size": len(long_text),
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        auto_read = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="auto", max_chars=32),
        )
        full_read = service.read_task_artifact(
            task_id=task.id,
            artifact_id=str(created[0].id),
            request=ArtifactReadRequest(mode="full", max_chars=32),
        )

        assert auto_read.status == "ready"
        assert auto_read.mode_used == "head"
        assert auto_read.content == long_text[:32]
        assert auto_read.truncated is True

        assert full_read.status == "ready"
        assert full_read.mode_used == "full"
        assert full_read.content == long_text[:32]
        assert full_read.truncated is True
    finally:
        db.close()
        engine.dispose()
