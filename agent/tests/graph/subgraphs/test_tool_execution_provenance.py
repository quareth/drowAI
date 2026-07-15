"""
Unit tests for artifact provenance integration in the LangGraph tool execution subgraph.

Verifies that run_tool_execution records execution start/complete via ArtifactProvenanceService
when the feature flag is enabled, stores execution_id in metadata, and that DB failures
do not break tool execution.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Optional, Tuple
from uuid import uuid4

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
import types
from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome


_BUILDERS_PACKAGE = "agent.graph.builders"
if _BUILDERS_PACKAGE not in sys.modules:
    builders_pkg = types.ModuleType(_BUILDERS_PACKAGE)
    builders_pkg.__path__ = [
        str(Path(__file__).resolve().parents[3] / "graph" / "builders")
    ]
    sys.modules[_BUILDERS_PACKAGE] = builders_pkg

import agent.graph.subgraphs.tool_execution as tool_execution_module
from agent.graph.subgraphs.tool_execution import run_tool_execution, _get_provenance_service
from agent.graph.subgraphs.tool_execution_runtime.artifact_and_provenance import (
    finalize_provenance_execution,
)


def _base_metadata(*, reserved_message_id: int | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "api_key": "key",
        "model": "model",
        "tool_plan_prepared": True,
        "planner_plan": {
            "selected_tools": ["shell.exec"],
            "tool_parameters": {"shell.exec": {"command": "echo hello"}},
            "execution_strategy": "single",
            "reasoning": "",
            "expected_outcome": "",
            "tool_batch": {
                "tool_batch_id": "tb_provenance_single",
                "requested_execution_strategy": "single",
                "deferred_followups": [],
                "selection_rationale": "provenance single-call fixture",
                "tool_calls": [
                    {
                        "tool_call_id": "tc_provenance_single",
                        "tool_id": "shell.exec",
                        "parameters": {"command": "echo hello"},
                        "intent": "Run echo",
                    }
                ],
            },
        },
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id="provenance-test-conversation",
            turn_id="turn-1",
            turn_sequence=1,
            messages=[{"role": "user", "content": "Run echo"}],
            current_message="Run echo",
        ),
    }
    if reserved_message_id is not None:
        metadata["reserved_message_id"] = reserved_message_id
    return metadata


def test_finalize_provenance_derives_structured_command_text() -> None:
    captured: dict[str, Any] = {}
    execution_id = uuid4()

    class _ExecutionRepo:
        def get_by_id(self, _execution_id: Any) -> Any:
            return SimpleNamespace(tool_arguments={})

    class _Db:
        def flush(self) -> None:
            return None

    class _ProvenanceService:
        execution_repo = _ExecutionRepo()
        db = _Db()
        artifact_repo = SimpleNamespace(get_by_execution=lambda _execution_id: [])

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(id=execution_id)

    outcome = SimpleNamespace(
        result={
            "success": True,
            "stdout": "<nmaprun></nmaprun>",
            "stderr": "",
            "exit_code": 0,
            "artifacts": [],
            "metadata": {},
        },
        parameters={"target": "127.0.0.1", "ports": "443", "service_detection": True},
        tool_id="information_gathering.network_discovery.nmap",
    )
    facts = SimpleNamespace(metadata={})

    finalize_provenance_execution(
        get_provenance_service_fn=lambda: (_ProvenanceService(), None),
        execution_id=execution_id,
        outcome=outcome,
        facts=facts,
        tool_name="information_gathering.network_discovery.nmap",
        tool_call_id="tool-call-1",
        turn_sequence=1,
        workspace_path="/tmp/ws",
        artifact_path=None,
        should_persist_artifact_outputs_fn=lambda _tool_id: True,
        build_command_for_display_fn=lambda tool_id, params: f"{tool_id} {params}",
        collect_persistable_tool_artifact_paths_fn=lambda **_kwargs: [],
        collect_provenance_artifact_refs_fn=lambda **_kwargs: [],
        logger=SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        ),
        safe_inc_fn=lambda *_args, **_kwargs: None,
    )

    assert captured["command_text"] == "nmap -T4 -p 443 -sV -oX - 127.0.0.1"


def test_finalize_provenance_skips_legacy_artifact_reads_for_runner_data_plane() -> None:
    captured: dict[str, Any] = {}
    execution_id = uuid4()

    class _ExecutionRepo:
        def get_by_id(self, _execution_id: Any) -> Any:
            return SimpleNamespace(tool_arguments={})

    class _Db:
        def flush(self) -> None:
            return None

    class _ProvenanceService:
        execution_repo = _ExecutionRepo()
        db = _Db()
        artifact_repo = SimpleNamespace(get_by_execution=lambda _execution_id: [])

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(id=execution_id)

    collect_called = False

    def _collect_legacy_paths(**_kwargs: Any) -> list[str]:
        nonlocal collect_called
        collect_called = True
        return ["artifacts/legacy.txt"]

    outcome = SimpleNamespace(
        result={
            "success": True,
            "stdout": "runner output",
            "stderr": "",
            "exit_code": 0,
            "artifacts": ["artifacts/runner.txt"],
            "metadata": {
                "task_runtime_job_id": "task-runtime-job-1",
                "artifact_scope": "cloud_data_plane",
                "artifact_upload": {"status": "promoted"},
            },
        },
        parameters={"command": "echo runner"},
        tool_id="shell.exec",
    )
    facts = SimpleNamespace(metadata={})

    finalize_provenance_execution(
        get_provenance_service_fn=lambda: (_ProvenanceService(), None),
        execution_id=execution_id,
        outcome=outcome,
        facts=facts,
        tool_name="shell.exec",
        tool_call_id="tool-call-1",
        turn_sequence=1,
        workspace_path="/tmp/ws",
        artifact_path=None,
        should_persist_artifact_outputs_fn=lambda _tool_id: True,
        build_command_for_display_fn=lambda tool_id, params: f"{tool_id} {params}",
        collect_persistable_tool_artifact_paths_fn=_collect_legacy_paths,
        collect_provenance_artifact_refs_fn=lambda **_kwargs: [],
        logger=SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        ),
        safe_inc_fn=lambda *_args, **_kwargs: None,
    )

    assert collect_called is False
    assert captured["artifact_paths"] is None
    assert captured["stdout"] == ""
    assert captured["stderr"] == ""
    assert captured["command_text"] is None
    metadata_patch = captured["execution_metadata_patch"]
    assert metadata_patch["artifact_route"] == "runner_data_plane"
    assert metadata_patch["legacy_artifact_path_persistence"]["status"] == "skipped"


class _StubCoordinator:
    """Stub coordinator that returns a fixed outcome without calling executor."""

    async def run(self, request: Any) -> ToolExecutionOutcome:
        catalog = [
            ToolCatalogEntry(
                tool_id="shell.exec",
                name="shell.exec",
                category="shell",
                description="Run command",
            )
        ]
        return ToolExecutionOutcome(
            tool_id="shell.exec",
            parameters={"command": "echo hello"},
            catalog=catalog,
            result={
                "success": True,
                "stdout": "hello\n",
                "stderr": "",
                "metadata": {
                    "open_ports": [22, 80],
                    "semantic_observations": [
                        {"observation_type": "network.open_port"}
                    ],
                    "semantic_evidence": [
                        {
                            "type": "diagnostic",
                            "name": "ssh_banner",
                            "value": "OpenSSH_8.2",
                            "detail": {"note": "port_22"},
                        }
                    ],
                    "semantic_schema_version": "execution_plane.v1",
                    "capability_family": "network_discovery",
                },
                "observation": "Done",
                "stdout_excerpt": "hello",
                "stderr_excerpt": "",
            },
            summary="Command ran",
            reasoning=[],
            duration=0.1,
        )


def test_get_provenance_service_returns_none_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.config.feature_flags.is_artifact_provenance_enabled",
        lambda: False,
    )
    service, db = _get_provenance_service()
    assert service is None
    assert db is None


@pytest.mark.parametrize("truthy_value", ["1", "true", "yes", "on"])
def test_get_provenance_service_initializes_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    truthy_value: str,
) -> None:
    _ = truthy_value  # provenance is always enabled; kept for test matrix stability

    class _DummySession:
        def close(self) -> None:
            return None

    class _DummyService:
        def __init__(self, db: Any) -> None:
            self.db = db

    monkeypatch.setattr("backend.database.SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(
        "backend.services.artifact.provenance_service.ArtifactProvenanceService",
        _DummyService,
    )

    service, db = _get_provenance_service()
    assert service is not None
    assert db is not None


@pytest.mark.asyncio
async def test_execution_start_hook_calls_record_tool_execution_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    record_calls: list = []

    class MockProvenanceService:
        execution_repo = type("Repo", (), {"get_by_id": lambda self, eid: None})()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            record_calls.append(kwargs)
            return type("Execution", (), {"id": uuid4()})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            return None

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return MockProvenanceService(), None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=99,
        message="Run echo",
        capability="simple_tool_execution",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo hello"}},
        metadata=_base_metadata(reserved_message_id=321),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=99,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert len(record_calls) == 1
    call = record_calls[0]
    assert call["task_id"] == 99
    assert call["tool_name"] == "shell.exec"
    assert call["agent_path"] == "langgraph"
    assert call["chat_message_id"] == 321
    # Phase 2.2 (re-audit fix): the single execution path runs every call
    # through ``BatchValidator``, which normalizes parameters via the
    # shared ``validate_tool_parameters`` helper. ``tool_arguments`` now
    # carries the normalized payload (e.g. shell.exec default
    # ``timeout_sec`` / ``redact_output`` / ``idempotent``) instead of
    # only the raw user-provided keys — provenance now reflects the
    # parameters the tool actually executed with.
    assert call["tool_arguments"]["command"] == "echo hello"
    assert updated.facts.metadata.get("last_tool_result", {}).get("success") is True


@pytest.mark.asyncio
async def test_execution_complete_stores_last_execution_id_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    execution_id = uuid4()
    complete_calls: list = []

    class MockProvenanceService:
        execution_repo = type("Repo", (), {"get_by_id": lambda self, eid: None})()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            return type("Execution", (), {"id": execution_id})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            complete_calls.append(kwargs)
            return SimpleNamespace(id=execution_id)

    _mock_svc = MockProvenanceService()

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return _mock_svc, None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=100,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=100,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert updated.facts.metadata.get("last_execution_id") == str(execution_id)
    assert len(complete_calls) == 1
    assert complete_calls[0]["execution_id"] == execution_id
    assert complete_calls[0]["status"] == "success"
    assert complete_calls[0]["command_text"] == "echo hello"
    metadata_patch = complete_calls[0].get("execution_metadata_patch")
    assert metadata_patch is not None
    assert metadata_patch["tool_metadata"]["open_ports"] == [22, 80]
    assert metadata_patch["tool_metadata"]["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]
    assert metadata_patch["tool_metadata"]["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "ssh_banner",
            "value": "OpenSSH_8.2",
            "detail": {"note": "port_22"},
        }
    ]
    assert metadata_patch["tool_metadata"]["semantic_schema_version"] == "execution_plane.v1"
    assert metadata_patch["tool_metadata"]["capability_family"] == "network_discovery"
    assert metadata_patch["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]
    assert metadata_patch["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "ssh_banner",
            "value": "OpenSSH_8.2",
            "detail": {"note": "port_22"},
        }
    ]
    assert metadata_patch["semantic_schema_version"] == "execution_plane.v1"
    assert metadata_patch["capability_family"] == "network_discovery"


@pytest.mark.asyncio
async def test_execution_complete_none_does_not_store_last_execution_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    execution_id = uuid4()
    complete_calls: list = []

    class MockProvenanceService:
        execution_repo = type("Repo", (), {"get_by_id": lambda self, eid: None})()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            return type("Execution", (), {"id": execution_id})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            complete_calls.append(kwargs)
            return None

    _mock_svc = MockProvenanceService()

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return _mock_svc, None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=103,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=103,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert "last_execution_id" not in updated.facts.metadata
    assert len(complete_calls) == 1


@pytest.mark.asyncio
async def test_coordinator_error_finalizes_started_execution_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    execution_id = uuid4()
    complete_calls: list = []

    class _ExplodingCoordinator:
        async def run(self, request: Any) -> ToolExecutionOutcome:
            raise RuntimeError("planner blew up")

    class MockProvenanceService:
        execution_repo = type("Repo", (), {"get_by_id": lambda self, eid: None})()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            return type("Execution", (), {"id": execution_id})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            complete_calls.append(kwargs)
            return SimpleNamespace(id=execution_id)

    _mock_svc = MockProvenanceService()

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return _mock_svc, None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _ExplodingCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=104,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=104,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    # Phase 2.2 (re-audit fix): single execution path goes through
    # ``BatchExecutor``, whose Phase 5 failure policy catches per-call
    # exceptions and converts them into terminal ``FAILED`` rows rather
    # than propagating them out of the orchestrator. The provenance
    # error-finalization hook still fires inside ``run_one_call`` before
    # the executor catches the exception, so ``complete_tool_execution``
    # is still called with ``status="error"``.
    await run_tool_execution(state.as_graph_state(), context=context)

    assert len(complete_calls) == 1
    assert complete_calls[0]["execution_id"] == execution_id
    assert complete_calls[0]["status"] == "error"


@pytest.mark.asyncio
async def test_db_failure_on_record_start_does_not_break_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")

    def mock_get_provenance_raise() -> Tuple[Optional[Any], Optional[Any]]:
        raise RuntimeError("DB connection failed")

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance_raise)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=101,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=101,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert updated.facts.metadata.get("last_tool_result", {}).get("success") is True


@pytest.mark.asyncio
async def test_completion_masks_reconciled_tool_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    execution_id = uuid4()
    secret = "phase1-provenance-password-7777"

    class _SecretParamsCoordinator:
        async def run(self, request: Any) -> ToolExecutionOutcome:
            catalog = [
                ToolCatalogEntry(
                    tool_id="shell.exec",
                    name="shell.exec",
                    category="shell",
                    description="Run command",
                )
            ]
            return ToolExecutionOutcome(
                tool_id="shell.exec",
                parameters={
                    "command": f"curl -u user:password={secret} http://example.test",
                    "password": secret,
                },
                catalog=catalog,
                result={
                    "success": True,
                    "stdout": "ok\n",
                    "stderr": "",
                    "observation": "ok",
                    "stdout_excerpt": "ok",
                    "stderr_excerpt": "",
                },
                summary="ok",
                reasoning=[],
                duration=0.1,
            )

    class _ExecutionRepo:
        def __init__(self) -> None:
            self.execution = SimpleNamespace(tool_arguments={"placeholder": True})

        def get_by_id(self, _execution_id: Any) -> Any:
            return self.execution

    class _Db:
        def flush(self) -> None:
            return None

    class MockProvenanceService:
        def __init__(self) -> None:
            self.execution_repo = _ExecutionRepo()
            self.db = _Db()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            return type("Execution", (), {"id": execution_id})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            return SimpleNamespace(id=execution_id)

    _mock_svc = MockProvenanceService()

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return _mock_svc, None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _SecretParamsCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=105,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=105,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    await run_tool_execution(state.as_graph_state(), context=context)
    serialized_arguments = str(_mock_svc.execution_repo.execution.tool_arguments)
    assert secret not in serialized_arguments
    assert "<DURABLE_SECRET_MASK:" in serialized_arguments


@pytest.mark.asyncio
async def test_db_failure_on_complete_does_not_break_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ARTIFACT_PROVENANCE", "true")
    execution_id = uuid4()

    class MockProvenanceService:
        execution_repo = type("Repo", (), {"get_by_id": lambda self, eid: None})()

        def record_tool_execution(self, **kwargs: Any) -> Any:
            return type("Execution", (), {"id": execution_id})()

        def complete_tool_execution(self, **kwargs: Any) -> Any:
            raise RuntimeError("DB write failed on complete")

    _mock_svc = MockProvenanceService()

    def mock_get_provenance() -> Tuple[Optional[Any], Optional[Any]]:
        return _mock_svc, None

    monkeypatch.setattr(tool_execution_module, "ToolExecutionCoordinator", lambda config: _StubCoordinator())
    monkeypatch.setattr(tool_execution_module, "_get_provenance_service", mock_get_provenance)
    monkeypatch.setattr(tool_execution_module, "get_stream_writer", lambda: None)

    facts = FactsState(
        task_id=102,
        message="Run echo",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=102,
        user_id=1,
        workspace_path="/tmp/ws",
        feature_flags={},
        api_key="key",
        model="model",
    )

    result = await run_tool_execution(state.as_graph_state(), context=context)
    updated = InteractiveState.from_mapping(result)

    assert updated.facts.metadata.get("last_tool_result", {}).get("success") is True
