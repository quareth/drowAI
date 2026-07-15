"""
Tests for ArtifactProvenanceQueryService read/query behavior.

These tests validate task-scoped lookups, pagination/filtering, timeline
aggregation, and serialization contracts for execution/artifact responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.artifact.provenance_query_service import (
    ArtifactProvenanceQueryService,
    ArtifactProvenanceScopeError,
)
import pytest


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_and_task(db, *, username: str, task_name: str, tenant_id: int = 1) -> Task:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, tenant_id=tenant_id, name=task_name)
    db.add(task)
    db.flush()
    return task


def test_get_execution_by_id_returns_execution_with_optional_artifacts() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-1", task_name="query-task-1")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo query"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-query-1",
            execution_metadata={
                "tool_metadata": {
                    "semantic_schema_version": "execution_plane.v1",
                    "semantic_observations": [{"observation_type": "finding.vulnerability_detected"}],
                },
                "capability_family": "web",
            },
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "command",
                    "content_text": "echo query",
                    "content_sha256": artifact_repo.compute_content_hash("echo query"),
                    "byte_size": 10,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "query",
                    "content_sha256": artifact_repo.compute_content_hash("query"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                }
            ]
        )
        db.commit()

        with_artifacts = query_service.get_execution_by_id(
            execution.id,
            task_id=task.id,
            include_artifacts=True,
        )
        without_artifacts = query_service.get_execution_by_id(
            execution.id,
            task_id=task.id,
            include_artifacts=False,
        )

        assert with_artifacts is not None
        assert with_artifacts["execution"]["execution_id"] == str(execution.id)
        assert len(with_artifacts["artifacts"]) == 2
        raw_output = with_artifacts["execution"]["raw_output"]
        assert raw_output["availability"] == "available"
        assert raw_output["reason"] == "artifacts_present"
        assert isinstance(raw_output["command_artifact_id"], str)
        artifact_kinds = {artifact["artifact_kind"] for artifact in with_artifacts["artifacts"]}
        assert "command" in artifact_kinds
        assert "stdout" in artifact_kinds
        assert "workspace_path" not in with_artifacts["execution"]
        assert "container_path" not in with_artifacts["execution"]
        assert "source_path" not in with_artifacts["artifacts"][0]
        assert "fallback_path" not in with_artifacts["artifacts"][0]
        assert with_artifacts["execution"]["execution_metadata"] == {
            "tool_metadata": {
                "semantic_schema_version": "execution_plane.v1",
                "semantic_observations": [{"observation_type": "finding.vulnerability_detected"}],
            },
            "capability_family": "web",
        }

        assert without_artifacts is not None
        assert "artifacts" not in without_artifacts
    finally:
        db.close()
        engine.dispose()


def test_get_execution_by_tool_call_id_is_task_scoped() -> None:
    engine, db = _build_session()
    try:
        task_one = _seed_user_and_task(db, username="query-user-2a", task_name="query-task-2a")
        task_two = _seed_user_and_task(db, username="query-user-2b", task_name="query-task-2b")
        execution_repo = ToolExecutionRepository(db)
        query_service = ArtifactProvenanceQueryService(db)
        collision_id = "tc-collision-query"

        one = execution_repo.create(
            task_id=task_one.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo one"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id=collision_id,
        )
        execution_repo.create(
            task_id=task_two.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo two"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id=collision_id,
        )
        db.commit()

        result = query_service.get_execution_by_tool_call_id(
            task_id=task_one.id,
            tool_call_id=collision_id,
            include_artifacts=False,
        )
        assert result is not None
        assert result["execution"]["execution_id"] == str(one.id)
        assert result["execution"]["task_id"] == task_one.id

        cross_task_lookup = query_service.get_execution_by_id(
            one.id,
            task_id=task_two.id,
            include_artifacts=False,
        )
        assert cross_task_lookup is None
    finally:
        db.close()
        engine.dispose()


def test_get_task_executions_applies_filters_and_pagination() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-3", task_name="query-task-3")
        execution_repo = ToolExecutionRepository(db)
        query_service = ArtifactProvenanceQueryService(db)
        t0 = datetime.now(timezone.utc) - timedelta(minutes=2)

        execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=t0,
            tool_call_id="tc-1",
        )
        execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="error",
            started_at=t0 + timedelta(minutes=1),
            tool_call_id="tc-2",
        )
        execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=t0 + timedelta(minutes=2),
            tool_call_id="tc-3",
        )
        db.commit()

        filtered = query_service.get_task_executions(
            task_id=task.id,
            tool_name="shell.exec",
            status="success",
            start_time=t0 + timedelta(seconds=30),
            end_time=t0 + timedelta(minutes=3),
            limit=10,
            offset=0,
        )
        paged = query_service.get_task_executions(
            task_id=task.id,
            limit=1,
            offset=1,
        )

        assert filtered["total"] == 1
        assert len(filtered["executions"]) == 1
        assert filtered["executions"][0]["tool_call_id"] == "tc-3"

        assert paged["total"] == 3
        assert len(paged["executions"]) == 1
    finally:
        db.close()
        engine.dispose()


def test_get_conversation_executions_filters_turn() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-4", task_name="query-task-4")
        execution_repo = ToolExecutionRepository(db)
        query_service = ArtifactProvenanceQueryService(db)
        t0 = datetime.now(timezone.utc)

        execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=t0,
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=t0 + timedelta(seconds=1),
            conversation_id="conv-1",
            turn_id="turn-2",
        )
        db.commit()

        conv_all = query_service.get_conversation_executions(
            task_id=task.id,
            conversation_id="conv-1",
            include_artifacts=False,
        )
        conv_turn = query_service.get_conversation_executions(
            task_id=task.id,
            conversation_id="conv-1",
            turn_id="turn-2",
            include_artifacts=False,
        )

        assert conv_all["total"] == 2
        assert len(conv_all["executions"]) == 2
        assert conv_turn["total"] == 1
        assert conv_turn["executions"][0]["turn_id"] == "turn-2"
    finally:
        db.close()
        engine.dispose()


def test_get_tool_execution_timeline_returns_chronological_rows_with_artifact_counts() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-5", task_name="query-task-5")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)
        t0 = datetime.now(timezone.utc)

        first = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=t0,
        )
        second = execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="error",
            started_at=t0 + timedelta(seconds=5),
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": first.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_sha256": artifact_repo.compute_content_hash("first"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": second.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_sha256": artifact_repo.compute_content_hash("second-a"),
                    "byte_size": 8,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": second.id,
                    "task_id": task.id,
                    "artifact_kind": "stderr",
                    "content_sha256": artifact_repo.compute_content_hash("second-b"),
                    "byte_size": 8,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        timeline = query_service.get_tool_execution_timeline(task_id=task.id, limit=10, offset=0)
        assert timeline["total"] == 2
        assert len(timeline["timeline"]) == 2
        assert timeline["timeline"][0]["execution_id"] == str(first.id)
        assert timeline["timeline"][0]["artifact_count"] == 1
        assert timeline["timeline"][1]["execution_id"] == str(second.id)
        assert timeline["timeline"][1]["artifact_count"] == 2
    finally:
        db.close()
        engine.dispose()


def test_get_artifact_by_id_and_search_artifacts() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-6", task_name="query-task-6")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "visible",
                    "content_sha256": artifact_repo.compute_content_hash("visible"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/out.txt",
                    "content_sha256": artifact_repo.compute_content_hash("file"),
                    "byte_size": 4,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        artifact = query_service.get_artifact_by_id(created[0].id, task_id=task.id)
        artifact_with_content = query_service.get_artifact_by_id(
            created[0].id,
            task_id=task.id,
            include_content=True,
        )
        artifact_cross_task = query_service.get_artifact_by_id(
            created[0].id,
            task_id=task.id + 1,
        )
        search = query_service.search_artifacts(task_id=task.id, artifact_kind="stdout", limit=10, offset=0)

        assert artifact is not None
        assert artifact["artifact_id"] == str(created[0].id)
        assert artifact["content_text"] is None
        assert artifact_with_content is not None
        assert artifact_with_content["content_text"] == "visible"
        assert "source_path" not in artifact
        assert "fallback_path" not in artifact
        assert artifact_cross_task is None

        assert search["total"] == 1
        assert len(search["artifacts"]) == 1
        assert search["artifacts"][0]["artifact_kind"] == "stdout"
        assert search["artifacts"][0]["content_text"] is None
    finally:
        db.close()
        engine.dispose()


def test_query_service_rejects_unscoped_uuid_lookups() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-8", task_name="query-task-8")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo scoped"},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        artifacts = artifact_repo.create_batch(
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

        with pytest.raises(ArtifactProvenanceScopeError):
            query_service.get_execution_by_id(execution.id, task_id=None)  # type: ignore[arg-type]

        with pytest.raises(ArtifactProvenanceScopeError):
            query_service.get_artifact_by_id(artifacts[0].id, task_id=None)  # type: ignore[arg-type]
    finally:
        db.close()
        engine.dispose()


def test_get_artifact_catalog_rows_returns_joined_and_filtered_results() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-7", task_name="query-task-7")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        exec_a = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-catalog-a",
            conversation_id="conv-catalog",
            turn_id="turn-a",
            turn_sequence=1,
        )
        exec_b = execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            tool_call_id="tc-catalog-b",
            conversation_id="conv-other",
            turn_id="turn-b",
            turn_sequence=2,
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": exec_a.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "visible",
                    "content_sha256": artifact_repo.compute_content_hash("visible"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": exec_b.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/result.txt",
                    "content_sha256": artifact_repo.compute_content_hash("result"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        rows = query_service.get_artifact_catalog_rows(task_id=task.id)
        filtered = query_service.get_artifact_catalog_rows(
            task_id=task.id,
            tool_name="filesystem.read_file",
            conversation_id="conv-other",
        )

        assert len(rows) == 2
        assert set(rows[0].keys()) >= {
            "artifact_id",
            "execution_id",
            "tool_call_id",
            "tool_name",
            "artifact_kind",
            "relative_path",
            "turn_id",
            "turn_sequence",
            "byte_size",
            "mime_type",
            "content_availability",
            "task_id",
            "created_at",
        }
        assert len(filtered) == 1
        assert filtered[0]["tool_name"] == "filesystem.read_file"
        assert filtered[0]["artifact_kind"] == "tool_file"
    finally:
        db.close()
        engine.dispose()


def test_get_artifact_catalog_page_paginates_and_matches_visible_label_queries() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-9", task_name="query-task-9")
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        turn_execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            turn_id="turn-z",
            turn_sequence=9,
        )
        fallback_execution = execution_repo.create(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
            turn_id="turn-fallback",
        )
        artifact_repo.create_batch(
            [
                {
                    "execution_id": turn_execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "alpha",
                    "content_sha256": artifact_repo.compute_content_hash("alpha"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
                {
                    "execution_id": fallback_execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/fallback.txt",
                    "content_sha256": artifact_repo.compute_content_hash("beta"),
                    "byte_size": 4,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        page = query_service.get_artifact_catalog_page(
            task_id=task.id,
            query_text="stdout from shell.exec (turn 9)",
            limit=10,
            offset=0,
        )
        fallback_page = query_service.get_artifact_catalog_page(
            task_id=task.id,
            query_text=(
                f"tool_file from filesystem.read_file (execution {str(fallback_execution.id)[:8]})"
            ),
            limit=10,
            offset=0,
        )
        path_page = query_service.get_artifact_catalog_page(
            task_id=task.id,
            query_text="fallback.txt",
            limit=10,
            offset=0,
        )
        paged_all = query_service.get_artifact_catalog_page(
            task_id=task.id,
            limit=1,
            offset=1,
        )

        assert page["total"] == 1
        assert len(page["rows"]) == 1
        assert page["rows"][0]["artifact_kind"] == "stdout"
        assert page["rows"][0]["tool_name"] == "shell.exec"

        assert fallback_page["total"] == 1
        assert len(fallback_page["rows"]) == 1
        assert fallback_page["rows"][0]["artifact_kind"] == "tool_file"
        assert fallback_page["rows"][0]["execution_id"] == str(fallback_execution.id)

        assert path_page["total"] == 1
        assert path_page["rows"][0]["relative_path"] == "artifacts/fallback.txt"

        assert paged_all["total"] == 2
        assert paged_all["limit"] == 1
        assert paged_all["offset"] == 1
        assert len(paged_all["rows"]) == 1
    finally:
        db.close()
        engine.dispose()


def test_get_artifact_by_id_applies_tenant_filter_before_task_and_artifact_ids() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(
            db,
            username="query-user-10",
            task_name="query-task-10",
            tenant_id=81,
        )
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
            agent_path="langgraph",
            status="success",
            started_at=datetime.now(timezone.utc),
        )
        created = artifact_repo.create_batch(
            [
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "stdout",
                    "content_text": "tenant scoped",
                    "content_sha256": artifact_repo.compute_content_hash("tenant scoped"),
                    "byte_size": 12,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        owner_result = query_service.get_artifact_by_id(
            created[0].id,
            tenant_id=81,
            task_id=task.id,
        )
        cross_tenant_result = query_service.get_artifact_by_id(
            created[0].id,
            tenant_id=999,
            task_id=task.id,
        )

        assert owner_result is not None
        assert owner_result["artifact_id"] == str(created[0].id)
        assert cross_tenant_result is None
    finally:
        db.close()
        engine.dispose()


def test_content_availability_serialization_uses_data_plane_states() -> None:
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db, username="query-user-11", task_name="query-task-11", tenant_id=91)
        execution_repo = ToolExecutionRepository(db)
        artifact_repo = ExecutionArtifactRepository(db)
        query_service = ArtifactProvenanceQueryService(db)

        execution = execution_repo.create(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={},
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
                    "content_text": "inline",
                    "content_sha256": artifact_repo.compute_content_hash("inline"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "upload_status": "inline",
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/ready.bin",
                    "object_key": "tenants/t/tasks/u/executions/e/artifacts/a/ready.bin",
                    "content_sha256": artifact_repo.compute_content_hash("ready"),
                    "byte_size": 5,
                    "mime_type": "application/octet-stream",
                    "is_text": False,
                    "upload_status": "ready",
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/pending.txt",
                    "object_key": "tenants/t/tasks/u/executions/e/artifacts/a/pending.txt",
                    "content_sha256": artifact_repo.compute_content_hash("pending"),
                    "byte_size": 7,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "upload_status": "upload_pending",
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/failed.txt",
                    "object_key": "tenants/t/tasks/u/executions/e/artifacts/a/failed.txt",
                    "content_sha256": artifact_repo.compute_content_hash("failed"),
                    "byte_size": 6,
                    "mime_type": "text/plain",
                    "is_text": True,
                    "upload_status": "upload_failed",
                },
                {
                    "execution_id": execution.id,
                    "task_id": task.id,
                    "artifact_kind": "tool_file",
                    "relative_path": "artifacts/local.txt",
                    "fallback_path": "/workspace/artifacts/local.txt",
                    "content_sha256": artifact_repo.compute_content_hash("local"),
                    "byte_size": 5,
                    "mime_type": "text/plain",
                    "is_text": True,
                },
            ]
        )
        db.commit()

        page = query_service.search_artifacts(tenant_id=91, task_id=task.id, limit=10, offset=0)
        by_kind = {row["artifact_kind"]: row["content_availability"] for row in page["artifacts"]}

        assert by_kind["stdout"] == "available_inline"
        assert by_kind["tool_file"] in {
            "available_object",
            "upload_pending",
            "upload_failed",
            "local_compatibility_only",
        }

        states = {row["content_availability"] for row in page["artifacts"]}
        assert "available_inline" in states
        assert "available_object" in states
        assert "upload_pending" in states
        assert "upload_failed" in states
        assert "local_compatibility_only" in states
    finally:
        db.close()
        engine.dispose()
