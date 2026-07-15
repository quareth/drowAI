"""
Integration tests for LangGraph tool execution → artifact provenance persistence.

Verifies that when ENABLE_ARTIFACT_PROVENANCE is true, a full tool execution flow
creates tool_executions and execution_artifacts rows and stores execution_id in
facts.metadata. Uses in-memory SQLite and a stub coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.services.artifact.memory_service import (
    ArtifactMemoryService,
    ArtifactReadRequest,
    ArtifactSearchFilters,
)
from backend.services.artifact.provenance_service import ArtifactProvenanceService
from tests.tool_execution_module_helper import build_tool_execution_metadata, patch_tool_execution_attr


def _make_engine_and_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory


def _seed_user_and_task(db):
    user = User(username="provenance-integration-user", password="secret")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, name="provenance-integration-task")
    db.add(task)
    db.flush()
    return task


class _StubCoordinator:
    """Stub that returns a fixed outcome without running real tools."""

    def __init__(
        self,
        *,
        tool_id: str = "shell.exec",
        result: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tool_id = str(tool_id or "shell.exec")
        self.result = result or {
            "success": True,
            "stdout": "integration\n",
            "stderr": "",
            "observation": "Done",
            "stdout_excerpt": "integration",
            "stderr_excerpt": "",
        }
        self.parameters = parameters or {"command": "echo integration"}

    async def run(self, request):
        from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome

        catalog = [
            ToolCatalogEntry(
                tool_id=self.tool_id,
                name=self.tool_id,
                category="shell",
                description="Run command",
            )
        ]
        return ToolExecutionOutcome(
            tool_id=self.tool_id,
            parameters=self.parameters,
            catalog=catalog,
            result=self.result,
            summary="Command ran",
            reasoning=[],
            duration=0.05,
        )


def _build_state(*, task_id: int, capability: str, command: str = "echo integration"):
    from agent.graph.state import FactsState, InteractiveState

    facts = FactsState(
        task_id=task_id,
        message="Run echo",
        capability=capability,
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": command}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="Run echo",
            selected_tools=["shell.exec"],
            tool_parameters={"shell.exec": {"command": command}},
        ),
    )
    return InteractiveState(facts=facts)


@pytest.fixture(autouse=True)
def _mock_compact_output(monkeypatch: pytest.MonkeyPatch):
    """Keep integration tests deterministic without external model calls."""
    from agent.graph.compression.schema import (
        CompactToolOutput,
        CompressionMetadata,
        ToolOutputCompressionResult,
    )

    async def _stub_compress(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "shell.exec")
        return ToolOutputCompressionResult(
            compact_output=CompactToolOutput(
                tool=tool_name,
                status="success",
                success=True,
                exit_code=0,
                summary="deterministic compact output",
                artifact_refs=[],
                compression=CompressionMetadata(source="deterministic"),
            )
        )

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _stub_compress)
    patch_tool_execution_attr(monkeypatch, "_enqueue_execution_ingestion", lambda **kwargs: None)


@pytest.mark.asyncio
async def test_langgraph_tool_execution_persists_provenance_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full tool execution with flag on creates tool_executions row and last_execution_id."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    with session_factory() as db:
        task = _seed_user_and_task(db)
        db.commit()
        task_id = task.id

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import FactsState, InteractiveState
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    facts = FactsState(
        task_id=task_id,
        message="Run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo integration"}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="Run echo",
            selected_tools=["shell.exec"],
            tool_parameters={"shell.exec": {"command": "echo integration"}},
        ),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert "last_execution_id" in updated.facts.metadata
    execution_id_str = updated.facts.metadata["last_execution_id"]

    with session_factory() as db:
        service = ArtifactProvenanceService(db)
        executions = service.get_task_executions(task_id=task_id, limit=10)
        assert len(executions) >= 1
        execution = next((e for e in executions if str(e.id) == execution_id_str), None)
        assert execution is not None, f"Execution {execution_id_str} not found in DB"
        assert execution.tool_name == "shell.exec"
        assert execution.status == "success"
        assert execution.agent_path == "langgraph"

    engine.dispose()


@pytest.mark.asyncio
async def test_langgraph_tool_execution_does_not_trigger_ingestion_hook_during_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatch path should no longer enqueue ingestion before post-tool reasoning completes."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    with session_factory() as db:
        task = _seed_user_and_task(db)
        db.commit()
        task_id = task.id

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    captured: Dict[str, Any] = {}

    def _capture_ingestion_hook(**kwargs):
        captured.update(kwargs)

    patch_tool_execution_attr(monkeypatch, "_enqueue_execution_ingestion", _capture_ingestion_hook)

    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import FactsState, InteractiveState
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    facts = FactsState(
        task_id=task_id,
        message="Run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo integration"}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="Run echo",
            selected_tools=["shell.exec"],
            tool_parameters={"shell.exec": {"command": "echo integration"}},
        ),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert "last_execution_id" in updated.facts.metadata
    assert captured == {}

    engine.dispose()


@pytest.mark.asyncio
async def test_langgraph_tool_execution_remains_successful_when_ingestion_hook_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime tool execution should stay successful even if enqueue helper would raise."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    with session_factory() as db:
        task = _seed_user_and_task(db)
        db.commit()
        task_id = task.id

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)
    patch_tool_execution_attr(
        monkeypatch,
        "_enqueue_execution_ingestion",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("enqueue exploded")),
    )

    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import FactsState, InteractiveState
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    facts = FactsState(
        task_id=task_id,
        message="Run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo integration"}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="Run echo",
            selected_tools=["shell.exec"],
            tool_parameters={"shell.exec": {"command": "echo integration"}},
        ),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert "last_execution_id" in updated.facts.metadata

    engine.dispose()


@pytest.mark.asyncio
async def test_langgraph_tool_execution_skips_provenance_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When provenance is disabled, no DB writes and no last_execution_id."""
    monkeypatch.setattr(
        "backend.config.feature_flags.is_artifact_provenance_enabled",
        lambda: False,
    )
    engine, session_factory = _make_engine_and_session()
    with session_factory() as db:
        task = _seed_user_and_task(db)
        db.commit()
        task_id = task.id

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    patch_tool_execution_attr(monkeypatch, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import FactsState, InteractiveState
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    facts = FactsState(
        task_id=task_id,
        message="Run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo noop"}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message="Run echo",
            selected_tools=["shell.exec"],
            tool_parameters={"shell.exec": {"command": "echo noop"}},
        ),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert "last_execution_id" not in updated.facts.metadata

    with session_factory() as db:
        service = ArtifactProvenanceService(db)
        executions = service.get_task_executions(task_id=task_id, limit=10)
        assert len(executions) == 0

    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["artifact.search", "artifact.read"])
async def test_artifact_retrieval_tools_do_not_emit_followup_tool_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_id: str,
) -> None:
    """Graph execution must not create self-chasing follow-up artifacts for retrieval tools."""
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    with session_factory() as db:
        task = _seed_user_and_task(db)
        db.commit()
        task_id = int(task.id)

    workspace = tmp_path / f"task-{task_id}"
    (workspace / "artifacts").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("backend.database.SessionLocal", session_factory)
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(
            tool_id=tool_id,
            parameters={"task_id": task_id},
            result={
                "success": True,
                "stdout": "artifact retrieval result\n",
                "stderr": "",
                "observation": "done",
            },
        ),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import FactsState, InteractiveState
    from agent.graph.subgraphs.tool_execution import run_tool_execution

    facts = FactsState(
        task_id=task_id,
        message=f"run {tool_id}",
        capability="simple_tool_execution",
        selected_tool=tool_id,
        tool_parameters={tool_id: {"task_id": task_id}},
        metadata=build_tool_execution_metadata(
            task_id=task_id,
            message=f"run {tool_id}",
            selected_tools=[tool_id],
            tool_parameters={tool_id: {"task_id": task_id}},
        ),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=task_id,
        user_id=1,
        workspace_path=str(workspace),
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    artifact_files = list((workspace / "artifacts").glob("*_tool.txt"))
    assert artifact_files == []
    assert "last_artifact_path" not in updated.facts.metadata
    with session_factory() as db:
        memory = ArtifactMemoryService(db)
        page = memory.search_task_artifacts(
            task_id=task_id,
            filters=ArtifactSearchFilters(limit=20, offset=0),
        )
        assert page.total == 0

    engine.dispose()


@pytest.mark.asyncio
async def test_artifact_memory_followup_is_task_scoped_across_turns_and_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Phase 6 flow regression: persisted artifacts must be reusable within one task only.

    Covers:
    - simple-tool execution persists artifact evidence
    - deep-reasoning follow-up can retrieve same-task evidence
    - cross-task read remains blocked
    """
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    engine, session_factory = _make_engine_and_session()
    try:
        with session_factory() as db:
            user = User(username="provenance-flow-user", password="secret")
            db.add(user)
            db.flush()
            primary_task = Task(user_id=user.id, name="provenance-flow-primary")
            secondary_task = Task(user_id=user.id, name="provenance-flow-secondary")
            db.add(primary_task)
            db.add(secondary_task)
            db.commit()
            primary_task_id = int(primary_task.id)
            secondary_task_id = int(secondary_task.id)

        primary_workspace = tmp_path / f"task-{primary_task_id}"
        secondary_workspace = tmp_path / f"task-{secondary_task_id}"
        (primary_workspace / "artifacts").mkdir(parents=True, exist_ok=True)
        (secondary_workspace / "artifacts").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("backend.database.SessionLocal", session_factory)
        patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

        from agent.graph.infrastructure.state_models import GraphRuntimeContext
        from agent.graph.state import InteractiveState
        from agent.graph.subgraphs.tool_execution import run_tool_execution

        patch_tool_execution_attr(
            monkeypatch,
            "ToolExecutionCoordinator",
            lambda config: _StubCoordinator(
                result={
                    "success": True,
                    "stdout": "phase6-primary-turn-1\n",
                    "stderr": "",
                    "observation": "done",
                    "stdout_excerpt": "phase6-primary-turn-1",
                    "stderr_excerpt": "",
                }
            ),
        )
        first_result = await run_tool_execution(
            _build_state(task_id=primary_task_id, capability="simple_tool_execution").as_graph_state(),
            context=GraphRuntimeContext(
                task_id=primary_task_id,
                user_id=1,
                workspace_path=str(primary_workspace),
                feature_flags={},
                api_key="key",
                model="model",
                turn_id="turn-1",
                turn_sequence=1,
            ),
        )
        first_updated = InteractiveState.from_mapping(first_result)
        assert "last_execution_id" in first_updated.facts.metadata

        patch_tool_execution_attr(
            monkeypatch,
            "ToolExecutionCoordinator",
            lambda config: _StubCoordinator(
                result={
                    "success": True,
                    "stdout": "phase6-primary-turn-2\n",
                    "stderr": "",
                    "observation": "done",
                    "stdout_excerpt": "phase6-primary-turn-2",
                    "stderr_excerpt": "",
                }
            ),
        )
        second_result = await run_tool_execution(
            _build_state(task_id=primary_task_id, capability="deep_reasoning").as_graph_state(),
            context=GraphRuntimeContext(
                task_id=primary_task_id,
                user_id=1,
                workspace_path=str(primary_workspace),
                feature_flags={},
                api_key="key",
                model="model",
                turn_id="turn-2",
                turn_sequence=2,
            ),
        )
        second_updated = InteractiveState.from_mapping(second_result)
        assert "last_execution_id" in second_updated.facts.metadata

        patch_tool_execution_attr(
            monkeypatch,
            "ToolExecutionCoordinator",
            lambda config: _StubCoordinator(
                result={
                    "success": True,
                    "stdout": "phase6-secondary-task\n",
                    "stderr": "",
                    "observation": "done",
                    "stdout_excerpt": "phase6-secondary-task",
                    "stderr_excerpt": "",
                }
            ),
        )
        secondary_result = await run_tool_execution(
            _build_state(task_id=secondary_task_id, capability="simple_tool_execution").as_graph_state(),
            context=GraphRuntimeContext(
                task_id=secondary_task_id,
                user_id=1,
                workspace_path=str(secondary_workspace),
                feature_flags={},
                api_key="key",
                model="model",
                turn_id="turn-1",
                turn_sequence=1,
            ),
        )
        secondary_updated = InteractiveState.from_mapping(secondary_result)
        assert "last_execution_id" in secondary_updated.facts.metadata

        with session_factory() as db:
            memory = ArtifactMemoryService(db)
            primary_stdout = memory.search_task_artifacts(
                task_id=primary_task_id,
                filters=ArtifactSearchFilters(artifact_kind="stdout", limit=20),
            )
            assert primary_stdout.total == 2

            first_turn_read = memory.read_task_artifact(
                task_id=primary_task_id,
                artifact_id=primary_stdout.artifacts[0].artifact_id,
                request=ArtifactReadRequest(mode="auto", max_chars=200),
            )
            assert first_turn_read.status == "ready"
            assert "phase6-primary-turn" in (first_turn_read.content or "")

            secondary_stdout = memory.search_task_artifacts(
                task_id=secondary_task_id,
                filters=ArtifactSearchFilters(artifact_kind="stdout", limit=20),
            )
            assert secondary_stdout.total == 1

            blocked = memory.read_task_artifact(
                task_id=primary_task_id,
                artifact_id=secondary_stdout.artifacts[0].artifact_id,
                request=ArtifactReadRequest(mode="auto"),
            )
            assert blocked.status == "not_found"
            assert blocked.content is None
    finally:
        engine.dispose()
