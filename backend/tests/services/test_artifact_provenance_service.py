"""Tests for ArtifactProvenanceService lifecycle and failure handling."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Task, User
from backend.models.tenant import Tenant
from backend.services.artifact.provenance_service import (
    ArtifactProvenanceService,
    MAX_CONTENT_SIZE,
    resolve_workspace_root,
    validate_artifact_path,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user_and_task(db) -> Task:
    tenant = Tenant(id=1, slug="default", name="Default")
    user = User(username="artifact-service-user", password="secret")
    db.merge(tenant)
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, name="artifact-service-task")
    db.add(task)
    db.flush()
    return task


def test_record_tool_execution_creates_row_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)

        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo hi"},
            tool_call_id="tc-123",
            conversation_id="conv-1",
            turn_id="turn-1",
            turn_sequence=1,
        )

        assert execution is not None
        assert execution.task_id == task.id
        assert execution.tool_call_id == "tc-123"
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_stores_large_stdout_without_content_text(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "generate"},
            started_at=datetime.now(timezone.utc),
        )
        assert execution is not None

        big_stdout = "x" * (MAX_CONTENT_SIZE + 200)
        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            exit_code=0,
            stdout=big_stdout,
        )
        assert completed is not None

        payload = service.get_execution_with_artifacts(execution.id, task_id=task.id)
        assert payload is not None
        artifacts = payload["artifacts"]
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact.artifact_kind == "stdout"
        assert artifact.content_text is None
        assert artifact.byte_size == len(big_stdout.encode("utf-8"))
        assert artifact.content_sha256 is not None
        assert len(artifact.content_sha256) == 64
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_persists_command_artifact(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "ls"},
            started_at=datetime.now(timezone.utc),
        )
        assert execution is not None

        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            exit_code=0,
            stdout="README.md\n",
            command_text="ls",
        )
        assert completed is not None

        payload = service.get_execution_with_artifacts(execution.id, task_id=task.id)
        assert payload is not None
        artifacts = payload["artifacts"]
        command_artifact = next(
            (artifact for artifact in artifacts if artifact.artifact_kind == "command"),
            None,
        )
        assert command_artifact is not None
        assert command_artifact.content_text == "ls"
        assert command_artifact.is_text is True
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_merges_execution_metadata_patch(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "nmap -sV 10.0.0.5"},
            started_at=datetime.now(timezone.utc),
            execution_metadata={
                "capability": "network_discovery",
                "tool_metadata": {"parsed_fields": {"hosts": 1}, "source": "parse_output"},
                "existing_key": "preserve-me",
            },
        )
        assert execution is not None

        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            exit_code=0,
            execution_metadata_patch={
                "tool_metadata": {
                    "semantic_observations": [{"observation_type": "network.open_port"}],
                    "semantic_schema_version": "execution_plane.v1",
                },
                "capability_family": "network",
                "artifact_refs": [{"artifact_kind": "stdout", "artifact_id": "artifact-1"}],
            },
        )
        assert completed is not None
        metadata = completed.execution_metadata
        assert metadata["existing_key"] == "preserve-me"
        assert metadata["capability"] == "network_discovery"
        assert metadata["tool_metadata"]["source"] == "parse_output"
        assert metadata["tool_metadata"]["parsed_fields"] == {"hosts": 1}
        assert metadata["tool_metadata"]["semantic_observations"] == [
            {"observation_type": "network.open_port"}
        ]
        assert metadata["tool_metadata"]["semantic_schema_version"] == "execution_plane.v1"
        assert metadata["capability_family"] == "network"
        assert metadata["artifact_refs"] == [{"artifact_kind": "stdout", "artifact_id": "artifact-1"}]
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_persists_artifacts_with_semantic_metadata_patch(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo semantic"},
            started_at=datetime.now(timezone.utc),
            execution_metadata={"start": {"attempt": 1}},
        )
        assert execution is not None

        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            exit_code=0,
            command_text="echo semantic",
            stdout="semantic\n",
            execution_metadata_patch={
                "tool_metadata": {"semantic_schema_version": "execution_plane.v1"},
                "capability_family": "shell",
            },
        )
        assert completed is not None
        assert completed.execution_metadata["start"] == {"attempt": 1}
        assert completed.execution_metadata["tool_metadata"]["semantic_schema_version"] == "execution_plane.v1"

        payload = service.get_execution_with_artifacts(execution.id, task_id=task.id)
        assert payload is not None
        artifacts = payload["artifacts"]
        assert len(artifacts) == 2
        artifact_kinds = {artifact.artifact_kind for artifact in artifacts}
        assert artifact_kinds == {"command", "stdout"}
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_reads_artifact_paths_through_provider(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    calls: list[dict] = []
    content = b"provider artifact\n"

    class FakeRuntimeOperationService:
        def __init__(self, _db):
            pass

        def context_for_internal_task(self, **kwargs):
            calls.append({"context": kwargs})
            return object()

        async def run_for_context(self, **kwargs):
            calls.append(
                {
                    "operation": kwargs["operation"],
                    "payload": kwargs["payload"],
                    "metadata": kwargs["metadata"],
                }
            )
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "path": "artifacts/result.txt",
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                },
            )

    monkeypatch.setattr(
        "backend.services.runtime_provider.runtime_artifact_access.RuntimeOperationService",
        FakeRuntimeOperationService,
    )

    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "write artifact"},
            started_at=datetime.now(timezone.utc),
        )
        assert execution is not None

        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            exit_code=0,
            artifact_paths=["/workspace/artifacts/result.txt"],
        )
        assert completed is not None

        payload = service.get_execution_with_artifacts(execution.id, task_id=task.id)
        assert payload is not None
        artifacts = payload["artifacts"]
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact.relative_path == "artifacts/result.txt"
        assert artifact.source_path == "artifacts/result.txt"
        assert artifact.content_text == content.decode("utf-8")
        assert artifact.byte_size == len(content)
        assert calls[0]["context"]["actor_type"].value == "system"
        assert calls[0]["context"]["actor_id"] == "artifact_provenance"
        assert calls[1]["operation"] == "read_runtime_artifact_file"
        assert calls[1]["payload"]["path"] == "artifacts/result.txt"
        assert calls[1]["payload"]["artifact_path"] == "artifacts/result.txt"
        assert calls[1]["payload"]["binary"] is True
        assert calls[1]["metadata"]["wait_for_result"] is True
    finally:
        db.close()
        engine.dispose()


def test_record_tool_execution_gracefully_degrades_on_db_error(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)

        def _boom(**_kwargs):
            raise RuntimeError("database unavailable")

        service.execution_repo.create = _boom  # type: ignore[assignment]
        result = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo fail"},
        )
        assert result is None
    finally:
        db.close()
        engine.dispose()


def test_get_execution_with_artifacts_is_task_scoped(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task_one = _seed_user_and_task(db)
        task_two = Task(user_id=task_one.user_id, name="artifact-service-task-2")
        db.add(task_two)
        db.flush()
        service = ArtifactProvenanceService(db)

        execution = service.record_tool_execution(
            task_id=task_one.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo scoped"},
        )
        assert execution is not None

        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            stdout="scoped",
        )
        assert completed is not None

        owner_payload = service.get_execution_with_artifacts(execution.id, task_id=task_one.id)
        cross_task_payload = service.get_execution_with_artifacts(execution.id, task_id=task_two.id)

        assert owner_payload is not None
        assert cross_task_payload is None
    finally:
        db.close()
        engine.dispose()


def test_record_tool_execution_skips_writes_when_flag_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.services.artifact.provenance_service.is_artifact_provenance_enabled",
        lambda: False,
    )
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo no-op"},
        )
        assert execution is None
        assert service.get_task_executions(task_id=task.id) == []
    finally:
        db.close()
        engine.dispose()


def test_service_get_task_and_conversation_executions(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)

        one = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo one"},
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        two = service.record_tool_execution(
            task_id=task.id,
            tool_name="filesystem.read_file",
            tool_arguments={"path": "README.md"},
            conversation_id="conv-1",
            turn_id="turn-2",
        )
        three = service.record_tool_execution(
            task_id=task.id,
            tool_name="network.nmap",
            tool_arguments={"target": "127.0.0.1"},
            conversation_id="conv-2",
            turn_id="turn-1",
        )
        assert one is not None and two is not None and three is not None

        task_rows = service.get_task_executions(task_id=task.id, limit=10, offset=0)
        conv_rows = service.get_conversation_executions(task_id=task.id, conversation_id="conv-1")
        conv_turn_rows = service.get_conversation_executions(
            task_id=task.id,
            conversation_id="conv-1",
            turn_id="turn-2",
        )

        assert len(task_rows) == 3
        assert len(conv_rows) == 2
        assert len(conv_turn_rows) == 1
        assert conv_turn_rows[0].turn_id == "turn-2"
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_returns_none_for_missing_execution(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        result = service.complete_tool_execution(
            execution_id="00000000-0000-0000-0000-000000000000",
            status="completed",
            exit_code=0,
            stdout="ok",
        )
        assert result is None
    finally:
        db.close()
        engine.dispose()


def test_complete_tool_execution_gracefully_handles_repository_error(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo repo-error"},
        )
        assert execution is not None

        def _boom(**_kwargs):
            raise RuntimeError("update failed")

        service.execution_repo.update_status = _boom  # type: ignore[assignment]
        result = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            stdout="done",
        )
        assert result is None
    finally:
        db.close()
        engine.dispose()


def test_task_delete_removes_runtime_provenance_rows(monkeypatch) -> None:
    """Lock current runtime-ownership contract before tenant baseline adds durable ownership."""

    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, db = _build_session()
    try:
        db.execute(text("PRAGMA foreign_keys=ON"))
        task = _seed_user_and_task(db)
        service = ArtifactProvenanceService(db)
        execution = service.record_tool_execution(
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo delete-contract"},
            tool_call_id="tc-delete-contract",
        )
        assert execution is not None
        completed = service.complete_tool_execution(
            execution_id=execution.id,
            status="completed",
            stdout="delete contract",
        )
        assert completed is not None

        pre_delete_counts = db.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM tool_executions WHERE task_id = :task_id) AS executions,
                    (SELECT COUNT(*) FROM execution_artifacts WHERE task_id = :task_id) AS artifacts
                """
            ),
            {"task_id": task.id},
        ).first()
        assert pre_delete_counts is not None
        assert pre_delete_counts.executions >= 1
        assert pre_delete_counts.artifacts >= 1

        db.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task.id})
        db.flush()

        post_delete_counts = db.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM tool_executions WHERE task_id = :task_id) AS executions,
                    (SELECT COUNT(*) FROM execution_artifacts WHERE task_id = :task_id) AS artifacts
                """
            ),
            {"task_id": task.id},
        ).first()
        assert post_delete_counts is not None
        assert post_delete_counts.executions == 0
        assert post_delete_counts.artifacts == 0
    finally:
        db.close()
        engine.dispose()


def test_validate_artifact_path_rejects_absolute_path_outside_workspace() -> None:
    with tempfile.TemporaryDirectory() as workspace_dir:
        with tempfile.NamedTemporaryFile(delete=False) as outside_file:
            outside_file.write(b"outside")
            outside_path = Path(outside_file.name)
        try:
            validated = validate_artifact_path(
                workspace_path=workspace_dir,
                candidate_path=str(outside_path),
            )
            assert validated is None
        finally:
            outside_path.unlink(missing_ok=True)


def test_validate_artifact_path_maps_container_workspace_prefix() -> None:
    with tempfile.TemporaryDirectory() as workspace_dir:
        artifact_rel = Path("artifacts") / "mapped.txt"
        artifact_abs = Path(workspace_dir) / artifact_rel
        artifact_abs.parent.mkdir(parents=True, exist_ok=True)
        artifact_abs.write_text("mapped", encoding="utf-8")

        validated = validate_artifact_path(
            workspace_path=workspace_dir,
            candidate_path="/workspace/artifacts/mapped.txt",
        )
        assert validated is not None
        _source_path, _fallback_path, _resolved_path, normalized_relative = validated
        assert normalized_relative == "artifacts/mapped.txt"


def test_resolve_workspace_root_uses_provider_projected_workspace_path() -> None:
    workspace_path = "/tmp/provider-workspace"
    resolved = resolve_workspace_root(task_id=9999, workspace_path=workspace_path)
    assert resolved is not None
    assert Path(resolved).as_posix() == Path(workspace_path).resolve().as_posix()


def test_resolve_workspace_root_does_not_reconstruct_canonical_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_id = 4321
    canonical = (tmp_path / "canonical" / f"task-{task_id}").resolve()
    poisoned = (tmp_path / "poison" / f"task-{task_id}").resolve()
    canonical.mkdir(parents=True, exist_ok=True)
    poisoned.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda _task_id: canonical),
    )

    resolved = resolve_workspace_root(task_id=task_id, workspace_path=str(poisoned))

    assert resolved == str(poisoned)
