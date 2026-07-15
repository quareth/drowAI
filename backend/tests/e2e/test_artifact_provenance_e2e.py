"""
End-to-end tests for artifact provenance lifecycle and queryability.

These tests validate the LangGraph-first write path from tool execution into
artifact provenance tables and then verify read-path behavior via the query
service, including graceful degradation and feature-flag behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.core import Task, User
from backend.services.artifact.memory_service import ArtifactMemoryService, ArtifactReadRequest
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from backend.services.artifact.provenance_service import ArtifactProvenanceService, MAX_CONTENT_SIZE
from tests.tool_execution_module_helper import build_tool_execution_metadata, patch_tool_execution_attr


def _make_engine_and_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory


def _seed_user_and_task(db: Session) -> int:
    user = User(username="artifact-e2e-user", password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, name="artifact-e2e-task")
    db.add(task)
    db.flush()
    return int(task.id)


def _patch_canonical_workspace_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_task_workspace_path",
        staticmethod(lambda task_id: (tmp_path / f"task-{int(task_id)}").resolve()),
    )


class _StubCoordinator:
    """Coordinator stub that returns deterministic tool execution outcomes."""

    def __init__(
        self,
        *,
        tool_id: str = "shell.exec",
        parameters: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tool_id = tool_id
        self.parameters = parameters or {"command": "echo e2e"}
        self.result = result or {
            "success": True,
            "stdout": "e2e\n",
            "stderr": "",
            "observation": "done",
            "stdout_excerpt": "e2e",
            "stderr_excerpt": "",
        }

    async def run(self, _request):
        from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome

        catalog = [
            ToolCatalogEntry(
                tool_id=self.tool_id,
                name=self.tool_id,
                category="shell",
                description="Stub tool",
            )
        ]
        return ToolExecutionOutcome(
            tool_id=self.tool_id,
            parameters=self.parameters,
            catalog=catalog,
            result=self.result,
            summary="stubbed execution",
            reasoning=[],
            duration=0.02,
        )


def _build_state(
    *,
    task_id: int,
    conversation_id: str,
    turn_id: str,
    workspace_path: str,
    turn_sequence: int = 1,
    tool_name: str = "shell.exec",
):
    from agent.graph.state import FactsState, InteractiveState

    facts = FactsState(
        task_id=task_id,
        message="run e2e command",
        conversation_id=conversation_id,
        capability="simple_tool_execution",
        selected_tool=tool_name,
        tool_parameters={tool_name: {"command": "echo e2e"}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="run e2e command",
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            selected_tools=[tool_name],
            tool_parameters={tool_name: {"command": "echo e2e"}},
            extra={"workspace_path": workspace_path},
        ),
    )
    return InteractiveState(facts=facts)


def _build_context(*, task_id: int, workspace_path: str, turn_id: str, turn_sequence: int):
    from agent.graph.infrastructure.state_models import GraphRuntimeContext

    return GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path=workspace_path,
        feature_flags={},
        api_key="key",
        model="model",
        turn_id=turn_id,
        turn_sequence=turn_sequence,
    )


@pytest.mark.asyncio
async def test_full_lifecycle_langgraph_to_db_to_query_with_context_linkage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """LangGraph run should persist records and support task-scoped query lookups."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    monkeypatch.setenv("LANGGRAPH_ENABLE_ARTIFACT_INDEXING", "false")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_path = str(tmp_path / f"task-{task_id}")
        (Path(workspace_path) / "artifacts").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("backend.database.SessionLocal", session_factory)
        patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
        patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

        from agent.graph.state import InteractiveState
        from agent.graph.subgraphs.tool_execution import run_tool_execution

        state = _build_state(
            task_id=task_id,
            conversation_id="conv-e2e-1",
            turn_id="turn-e2e-1",
            workspace_path=workspace_path,
            turn_sequence=7,
        )
        context = _build_context(
            task_id=task_id,
            workspace_path=workspace_path,
            turn_id="turn-e2e-1",
            turn_sequence=7,
        )

        result = await run_tool_execution(state.as_graph_state(), context=context)
        updated = InteractiveState.from_mapping(result)

        execution_id = updated.facts.metadata.get("last_execution_id")
        assert isinstance(execution_id, str) and execution_id

        with session_factory() as db:
            query_service = ArtifactProvenanceQueryService(db)
            execution_payload = query_service.get_execution_by_id(
                execution_id=execution_id,
                task_id=task_id,
                include_artifacts=True,
            )
            assert execution_payload is not None
            execution = execution_payload["execution"]

            assert execution["task_id"] == task_id
            assert execution["conversation_id"] == "conv-e2e-1"
            assert execution["turn_id"] == "turn-e2e-1"
            assert execution["turn_sequence"] == 7
            assert execution["tool_name"] == "shell.exec"
            assert "workspace_path" not in execution
            assert "container_path" not in execution
            assert execution["raw_output"]["availability"] == "available"
            assert execution["raw_output"]["reason"] == "artifacts_present"
            command_artifact_id = execution["raw_output"].get("command_artifact_id")
            assert isinstance(command_artifact_id, str) and command_artifact_id

            tool_call_id = execution["tool_call_id"]
            assert isinstance(tool_call_id, str) and tool_call_id

            by_tool_call = query_service.get_execution_by_tool_call_id(
                task_id=task_id,
                tool_call_id=tool_call_id,
                include_artifacts=True,
            )
            assert by_tool_call is not None
            assert by_tool_call["execution"]["execution_id"] == execution_id
            assert by_tool_call["execution"]["raw_output"]["availability"] == "available"
            assert by_tool_call["execution"]["raw_output"]["reason"] == "artifacts_present"
            stdout_artifact_id = by_tool_call["execution"]["raw_output"].get("stdout_artifact_id")
            assert isinstance(stdout_artifact_id, str) and stdout_artifact_id
            stdout_artifact = next(
                item for item in by_tool_call["artifacts"] if item["artifact_id"] == stdout_artifact_id
            )
            assert stdout_artifact["artifact_kind"] == "stdout"
            assert stdout_artifact["content_availability"] == "available"

            artifact_kinds = {item["artifact_kind"] for item in execution_payload["artifacts"]}
            assert "command" in artifact_kinds
            assert "stdout" in artifact_kinds
            for artifact in execution_payload["artifacts"]:
                assert "source_path" not in artifact
                assert "fallback_path" not in artifact
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_multi_artifact_execution_persists_multiple_tool_file_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tool returning multiple artifact paths should produce multiple DB artifact rows."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    monkeypatch.setenv("LANGGRAPH_ENABLE_ARTIFACT_INDEXING", "false")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_root = tmp_path / f"task-{task_id}"
        artifacts_dir = workspace_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        first_rel = "artifacts/custom-a.txt"
        second_rel = "artifacts/custom-b.txt"
        (workspace_root / first_rel).write_text("first artifact", encoding="utf-8")
        (workspace_root / second_rel).write_text("second artifact", encoding="utf-8")

        monkeypatch.setattr("backend.database.SessionLocal", session_factory)
        patch_tool_execution_attr(
            monkeypatch,
            "ToolExecutionCoordinator",
            lambda config: _StubCoordinator(
                result={
                    "success": True,
                    "stdout": "custom artifacts\n",
                    "stderr": "",
                    "artifacts": [first_rel, second_rel],
                    "observation": "done",
                    "stdout_excerpt": "custom artifacts",
                    "stderr_excerpt": "",
                }
            ),
        )
        patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

        from agent.graph.state import InteractiveState
        from agent.graph.subgraphs.tool_execution import run_tool_execution

        state = _build_state(
            task_id=task_id,
            conversation_id="conv-e2e-2",
            turn_id="turn-e2e-2",
            workspace_path=str(workspace_root),
        )
        context = _build_context(
            task_id=task_id,
            workspace_path=str(workspace_root),
            turn_id="turn-e2e-2",
            turn_sequence=2,
        )
        result = await run_tool_execution(state.as_graph_state(), context=context)
        updated = InteractiveState.from_mapping(result)
        execution_id = updated.facts.metadata.get("last_execution_id")
        assert isinstance(execution_id, str) and execution_id

        with session_factory() as db:
            payload = ArtifactProvenanceQueryService(db).get_execution_by_id(
                execution_id=execution_id,
                task_id=task_id,
                include_artifacts=True,
            )
            assert payload is not None
            relative_paths = {
                item["relative_path"]
                for item in payload["artifacts"]
                if item["artifact_kind"] == "tool_file"
            }
            assert first_rel in relative_paths
            assert second_rel in relative_paths
    finally:
        engine.dispose()


def test_timeline_query_returns_chronological_order() -> None:
    """Timeline endpoint data should be ordered chronologically by started_at."""
    from pytest import MonkeyPatch

    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()

        with session_factory() as db:
            service = ArtifactProvenanceService(db)
            query = ArtifactProvenanceQueryService(db)
            t0 = datetime.now(timezone.utc) - timedelta(minutes=1)

            first = service.record_tool_execution(
                task_id=task_id,
                tool_name="shell.exec",
                tool_arguments={"command": "echo first"},
                started_at=t0,
                conversation_id="conv-timeline",
                turn_id="turn-1",
                turn_sequence=1,
            )
            second = service.record_tool_execution(
                task_id=task_id,
                tool_name="shell.exec",
                tool_arguments={"command": "echo second"},
                started_at=t0 + timedelta(seconds=20),
                conversation_id="conv-timeline",
                turn_id="turn-2",
                turn_sequence=2,
            )
            assert first is not None and second is not None

            timeline = query.get_tool_execution_timeline(task_id=task_id, limit=10, offset=0)
            ids = [row["execution_id"] for row in timeline["timeline"]]
            assert ids[:2] == [str(first.id), str(second.id)]
    finally:
        monkeypatch.undo()
        engine.dispose()


@pytest.mark.asyncio
async def test_db_write_failures_do_not_break_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tool execution should succeed even when provenance DB writes fail."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    monkeypatch.setenv("LANGGRAPH_ENABLE_ARTIFACT_INDEXING", "false")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_path = str(tmp_path / f"task-{task_id}")
        (Path(workspace_path) / "artifacts").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("backend.database.SessionLocal", session_factory)
        patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
        patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

        class _FailingProvenanceService:
            def record_tool_execution(self, **_kwargs):
                raise RuntimeError("db unavailable")

            def complete_tool_execution(self, **_kwargs):
                raise RuntimeError("db unavailable")

        class _NoopSession:
            @staticmethod
            def close() -> None:
                return None

        patch_tool_execution_attr(
            monkeypatch,
            "_get_provenance_service",
            lambda: (_FailingProvenanceService(), _NoopSession()),
        )

        from agent.graph.state import InteractiveState
        from agent.graph.subgraphs.tool_execution import run_tool_execution

        state = _build_state(
            task_id=task_id,
            conversation_id="conv-e2e-fail",
            turn_id="turn-e2e-fail",
            workspace_path=workspace_path,
        )
        context = _build_context(
            task_id=task_id,
            workspace_path=workspace_path,
            turn_id="turn-e2e-fail",
            turn_sequence=1,
        )
        result = await run_tool_execution(state.as_graph_state(), context=context)
        updated = InteractiveState.from_mapping(result)

        # Degraded mode: execution still completes but no persisted execution_id.
        assert updated.trace.executed_tools
        assert "last_execution_id" not in updated.facts.metadata
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_feature_flag_disabled_skips_provenance_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When provenance is disabled, no DB writes and no last_execution_id."""
    monkeypatch.setattr(
        "backend.config.feature_flags.is_artifact_provenance_enabled",
        lambda: False,
    )
    monkeypatch.setenv("LANGGRAPH_ENABLE_ARTIFACT_INDEXING", "false")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_path = str(tmp_path / f"task-{task_id}")
        (Path(workspace_path) / "artifacts").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("backend.database.SessionLocal", session_factory)
        patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
        patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

        from agent.graph.state import InteractiveState
        from agent.graph.subgraphs.tool_execution import run_tool_execution

        state = _build_state(
            task_id=task_id,
            conversation_id="conv-e2e-disabled",
            turn_id="turn-e2e-disabled",
            workspace_path=workspace_path,
        )
        context = _build_context(
            task_id=task_id,
            workspace_path=workspace_path,
            turn_id="turn-e2e-disabled",
            turn_sequence=1,
        )
        result = await run_tool_execution(state.as_graph_state(), context=context)
        updated = InteractiveState.from_mapping(result)
        assert "last_execution_id" not in updated.facts.metadata

        with session_factory() as db:
            task_executions = ArtifactProvenanceService(db).get_task_executions(
                task_id=task_id,
                limit=10,
                offset=0,
            )
            assert task_executions == []
    finally:
        engine.dispose()


def test_large_artifact_uses_path_reference_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Artifacts larger than threshold should keep path/hash metadata but omit inline content."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_root = tmp_path / f"task-{task_id}"
        artifacts_dir = workspace_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        large_path = artifacts_dir / "large.txt"
        large_path.write_text("X" * (MAX_CONTENT_SIZE + 4096), encoding="utf-8")
        large_rel = "artifacts/large.txt"

        with session_factory() as db:
            service = ArtifactProvenanceService(db)
            execution = service.record_tool_execution(
                task_id=task_id,
                tool_name="shell.exec",
                tool_arguments={"command": "emit large"},
                workspace_path=str(workspace_root),
            )
            assert execution is not None
            completed = service.complete_tool_execution(
                execution_id=execution.id,
                status="success",
                artifact_paths=[large_rel],
                workspace_path=str(workspace_root),
            )
            assert completed is not None

            query = ArtifactProvenanceQueryService(db)
            payload = query.get_execution_by_id(
                execution_id=execution.id,
                task_id=task_id,
                include_artifacts=True,
            )
            assert payload is not None
            large_artifacts = [
                item for item in payload["artifacts"] if item["relative_path"] == large_rel
            ]
            assert large_artifacts
            artifact = large_artifacts[0]
            assert artifact["byte_size"] > MAX_CONTENT_SIZE
            assert artifact["content_text"] is None
            assert artifact["content_availability"] == "unavailable_text_omitted"
            assert artifact["relative_path"] == large_rel
            assert isinstance(artifact["content_sha256"], str) and len(artifact["content_sha256"]) == 64
    finally:
        engine.dispose()


def test_empty_text_artifact_content_is_marked_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Empty text artifacts are valid content and should not be marked omitted."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_root = tmp_path / f"task-{task_id}"
        artifacts_dir = workspace_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        empty_rel = "artifacts/empty.txt"
        (workspace_root / empty_rel).write_text("", encoding="utf-8")

        with session_factory() as db:
            service = ArtifactProvenanceService(db)
            execution = service.record_tool_execution(
                task_id=task_id,
                tool_name="shell.exec",
                tool_arguments={"command": "cat /dev/null"},
                workspace_path=str(workspace_root),
            )
            assert execution is not None

            completed = service.complete_tool_execution(
                execution_id=execution.id,
                status="success",
                artifact_paths=[empty_rel],
                workspace_path=str(workspace_root),
            )
            assert completed is not None

            payload = ArtifactProvenanceQueryService(db).get_execution_by_id(
                execution_id=execution.id,
                task_id=task_id,
                include_artifacts=True,
            )
            assert payload is not None
            empty_artifact = next(
                item for item in payload["artifacts"] if item["relative_path"] == empty_rel
            )
            assert empty_artifact["content_availability"] == "available"

            artifact_detail = ArtifactProvenanceQueryService(db).get_artifact_by_id(
                empty_artifact["artifact_id"],
                task_id=task_id,
                include_content=True,
            )
            assert artifact_detail is not None
            assert artifact_detail["content_text"] == ""
            assert artifact_detail["content_availability"] == "available"
    finally:
        engine.dispose()


def test_execution_without_stdout_stderr_artifacts_reports_not_available() -> None:
    """Executions without stdout/stderr refs should expose deterministic raw-output state."""
    from pytest import MonkeyPatch

    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()

        with session_factory() as db:
            service = ArtifactProvenanceService(db)
            query = ArtifactProvenanceQueryService(db)
            execution = service.record_tool_execution(
                task_id=task_id,
                tool_name="filesystem.read_file",
                tool_arguments={"path": "README.md"},
            )
            assert execution is not None
            completed = service.complete_tool_execution(
                execution_id=execution.id,
                status="success",
                artifact_paths=[],
            )
            assert completed is not None

            payload = query.get_execution_by_tool_call_id(
                task_id=task_id,
                tool_call_id=execution.tool_call_id,
                include_artifacts=True,
            )
            assert payload is not None
            assert payload["execution"]["raw_output"]["availability"] == "not_available"
            assert payload["execution"]["raw_output"]["reason"] == "missing_command_stdout_stderr_artifacts"
    finally:
        monkeypatch.undo()
        engine.dispose()


def test_artifact_memory_read_uses_workspace_fallback_for_omitted_inline_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Artifact memory should read bounded file excerpts when DB inline content is omitted."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            task_id = _seed_user_and_task(db)
            db.commit()
        _patch_canonical_workspace_root(monkeypatch, tmp_path)

        workspace_root = tmp_path / f"task-{task_id}"
        artifacts_dir = workspace_root / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_rel = "artifacts/large-fallback.txt"
        artifact_text = "Y" * (MAX_CONTENT_SIZE + 4096)
        (workspace_root / artifact_rel).write_text(artifact_text, encoding="utf-8")

        with session_factory() as db:
            provenance = ArtifactProvenanceService(db)
            execution = provenance.record_tool_execution(
                task_id=task_id,
                tool_name="shell.exec",
                tool_arguments={"command": "emit-large"},
                workspace_path=str(workspace_root),
            )
            assert execution is not None

            completed = provenance.complete_tool_execution(
                execution_id=execution.id,
                status="success",
                artifact_paths=[artifact_rel],
                workspace_path=str(workspace_root),
            )
            assert completed is not None

            artifact_payload = ArtifactProvenanceQueryService(db).search_artifacts(
                task_id=task_id,
                artifact_kind="tool_file",
                limit=10,
                offset=0,
            )
            assert artifact_payload["total"] >= 1
            artifact_id = artifact_payload["artifacts"][0]["artifact_id"]

            memory = ArtifactMemoryService(db)
            auto_read = memory.read_task_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                request=ArtifactReadRequest(mode="auto", max_chars=128),
            )
            full_read = memory.read_task_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                request=ArtifactReadRequest(mode="full", max_chars=128),
            )

            assert auto_read.status == "ready"
            assert auto_read.source == "workspace_file"
            assert auto_read.mode_used == "head"
            assert auto_read.content == artifact_text[:128]
            assert auto_read.truncated is True

            assert full_read.status == "omitted_by_policy"
            assert full_read.source == "workspace_file"
            assert full_read.mode_used == "full"
            assert full_read.content == artifact_text[:128]
            assert full_read.truncated is True
    finally:
        engine.dispose()
