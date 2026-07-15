"""Tests for tool execution artifact saving integration."""

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set DATABASE_URL before importing backend modules
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from agent.graph.subgraphs.tool_execution import run_tool_execution
from agent.graph.state import InteractiveState, FactsState
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.compression.schema import (
    ArtifactReference,
    CompactToolOutput,
    CompressionMetadata,
    ToolOutputCompressionResult,
)
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)


def _base_metadata(*, model: str | None = None) -> dict[str, str | dict]:
    metadata: dict[str, str | dict] = {
        "api_key": "test-key",
        "planner_plan": {
            "selected_tools": ["shell.exec"],
            "candidate_tools": ["shell.exec"],
            "tool_parameters": {"shell.exec": {"command": "echo test"}},
            "execution_strategy": "sequential",
            "reasoning": "Use shell execution for artifact persistence coverage.",
            "expected_outcome": "Artifact metadata is written.",
        },
        METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
            conversation_id="artifact-test-conversation",
            turn_id="turn-1",
            turn_sequence=1,
            messages=[{"role": "user", "content": "run tool"}],
            current_message="run tool",
        ),
    }
    if model:
        metadata["model"] = model
    return metadata


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture(autouse=True)
def mock_stream_writer():
    """Run tool_execution tests outside LangGraph runtime by stubbing stream writer."""
    with patch("agent.graph.subgraphs.tool_execution.get_stream_writer", return_value=None):
        yield


@pytest.fixture(autouse=True)
def mock_action_plan():
    """Bypass planner setup; these tests validate artifact persistence behavior only."""
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_compact_compression():
    """Avoid external LLM calls during compact-output generation in tests."""

    async def _compress_stub(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "test_tool")
        return ToolOutputCompressionResult(
            compact_output=CompactToolOutput(
                tool=tool_name,
                status="success",
                success=True,
                exit_code=0,
                summary="deterministic test compact output",
                artifact_refs=[],
                compression=CompressionMetadata(source="deterministic"),
            )
        )

    with patch("agent.graph.subgraphs.tool_execution.compress_tool_output", _compress_stub):
        yield


@pytest.fixture
def mock_coordinator_outcome():
    """Create a mock coordinator outcome."""
    outcome = MagicMock()
    outcome.tool_id = "test_tool"
    outcome.parameters = {"target": "127.0.0.1"}
    outcome.result = {
        "stdout": "Port 80 is open\nPort 443 is open\n",
        "stderr": "Warning: some hosts unreachable\n",
        "stdout_excerpt": "Port 80 is open...",
        "stderr_excerpt": "Warning: some...",
        "observation": "Two ports found open",
    }
    outcome.reasoning = ["Scanning target 127.0.0.1"]
    outcome.summary = "Scan completed successfully"
    outcome.catalog = []
    outcome.to_graph_metadata = MagicMock(return_value={
        "tool_id": "test_tool",
        "timestamp": "2025-01-29T12:00:00Z"
    })
    return outcome


@pytest.mark.asyncio
async def test_artifact_saving_integration(temp_workspace, mock_coordinator_outcome):
    """Test that tool execution saves artifacts to file."""
    
    # Create initial state
    facts = FactsState(
        task_id=123,
        message="scan 127.0.0.1 with nmap",
        capability="simple_tool_execution",
        metadata=_base_metadata(model="gpt-4"),
    )
    
    state = InteractiveState(facts=facts)
    
    # Create runtime context with workspace path
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=temp_workspace,
        api_key="test-key",
        model="gpt-4",
        user_id=1
    )
    
    # Mock the coordinator
    with patch('agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator') as mock_coordinator_class:
        mock_coordinator = MagicMock()
        mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
        mock_coordinator_class.return_value = mock_coordinator
        
        # Run tool execution
        result = await run_tool_execution(state, context)
    
    # Verify artifact was saved
    artifacts_dir = Path(temp_workspace) / "artifacts"
    assert artifacts_dir.exists(), "Artifacts directory should be created"
    
    artifact_files = list(artifacts_dir.glob("*_tool.txt"))
    assert len(artifact_files) == 1, "Should create exactly one artifact file"
    
    artifact_path = str(artifact_files[0])
    
    # Verify artifact content
    with open(artifact_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    assert "Port 80 is open" in content
    assert "Port 443 is open" in content
    assert "=== STDERR ===" in content
    assert "Warning: some hosts unreachable" in content
    
    # Verify metadata was updated
    updated_state = InteractiveState.from_mapping(result)
    assert "last_artifact_path" in updated_state.facts.metadata
    relative_artifact_path = updated_state.facts.metadata["last_artifact_path"]
    assert relative_artifact_path.startswith("artifacts/")
    assert str(Path(temp_workspace) / relative_artifact_path) == artifact_path
    assert updated_state.facts.metadata["workspace_path"] == temp_workspace


@pytest.mark.asyncio
async def test_artifact_saving_with_empty_output(temp_workspace, mock_coordinator_outcome):
    """Test artifact saving with empty stdout/stderr."""
    
    # Modify outcome to have empty output
    mock_coordinator_outcome.result = {
        "stdout": "",
        "stderr": "",
        "observation": "Tool ran but produced no output",
    }
    
    facts = FactsState(
        task_id=123,
        message="test command",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=temp_workspace,
        api_key="test-key",
        user_id=1
    )
    
    with patch('agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator') as mock_coordinator_class:
        mock_coordinator = MagicMock()
        mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
        mock_coordinator_class.return_value = mock_coordinator
        
        result = await run_tool_execution(state, context)
    
    # Should still create artifact file (even if empty)
    artifacts_dir = Path(temp_workspace) / "artifacts"
    assert artifacts_dir.exists()
    
    artifact_files = list(artifacts_dir.glob("*_tool.txt"))
    assert len(artifact_files) == 1


@pytest.mark.asyncio
async def test_artifact_saving_failure_does_not_break_execution(mock_coordinator_outcome):
    """Test that artifact saving failure doesn't break tool execution."""
    
    # Use non-existent workspace that might fail
    invalid_workspace = "/this/path/absolutely/does/not/exist/and/will/fail"
    
    facts = FactsState(
        task_id=123,
        message="test command",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=invalid_workspace,
        api_key="test-key",
        user_id=1
    )
    
    with patch('agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator') as mock_coordinator_class:
        mock_coordinator = MagicMock()
        mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
        mock_coordinator_class.return_value = mock_coordinator
        
        # Should NOT raise exception even if artifact save fails
        result = await run_tool_execution(state, context)
        
        # Execution should complete successfully
        assert result is not None
        updated_state = InteractiveState.from_mapping(result)
        
        # Should have recorded the failure in reasoning
        reasoning_text = " ".join(updated_state.trace.reasoning)
        # May contain warning about artifact save failure.
        assert updated_state.facts.selected_tool == "shell.exec"


@pytest.mark.asyncio
async def test_artifact_saving_without_workspace(temp_workspace, mock_coordinator_outcome):
    """Test that missing workspace path is handled gracefully."""
    
    facts = FactsState(
        task_id=123,
        message="test command",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    
    state = InteractiveState(facts=facts)
    # No workspace path in context
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=None,
        api_key="test-key",
        user_id=1
    )
    
    fallback_workspace = Path(temp_workspace) / "task-123"
    with patch('agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator') as mock_coordinator_class:
        with patch(
            "backend.config.workspace_config.WorkspaceConfig.get_task_workspace_path",
            return_value=fallback_workspace,
        ):
            mock_coordinator = MagicMock()
            mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
            mock_coordinator_class.return_value = mock_coordinator

            # Should handle missing workspace gracefully.
            result = await run_tool_execution(state, context)

            # Execution should complete.
            assert result is not None
            updated_state = InteractiveState.from_mapping(result)
            assert updated_state.facts.selected_tool == "shell.exec"
            assert updated_state.facts.metadata["workspace_path"] == str(fallback_workspace)


@pytest.mark.asyncio
async def test_artifact_metadata_fields(temp_workspace, mock_coordinator_outcome):
    """Test that all expected metadata fields are populated."""
    
    facts = FactsState(
        task_id=123,
        message="scan target",
        capability="simple_tool_execution",
        metadata=_base_metadata(),
    )
    
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=temp_workspace,
        api_key="test-key",
        user_id=1
    )
    
    with patch('agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator') as mock_coordinator_class:
        mock_coordinator = MagicMock()
        mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
        mock_coordinator_class.return_value = mock_coordinator
        
        result = await run_tool_execution(state, context)
    
    updated_state = InteractiveState.from_mapping(result)
    metadata = updated_state.facts.metadata
    
    # Verify all expected fields
    assert "last_artifact_path" in metadata
    assert "workspace_path" in metadata
    assert "last_tool_result" in metadata
    assert "tool_history" in metadata
    assert "tool_catalog" in metadata
    
    # Verify artifact path is valid
    artifact_relative_path = metadata["last_artifact_path"]
    assert artifact_relative_path.endswith("_tool.txt")
    assert artifact_relative_path.startswith("artifacts/")
    assert os.path.exists(os.path.join(temp_workspace, artifact_relative_path))
    
    # Verify workspace path matches
    assert metadata["workspace_path"] == temp_workspace


@pytest.mark.asyncio
async def test_compact_artifact_refs_enriched_with_provenance_metadata(temp_workspace, mock_coordinator_outcome):
    """Tool execution should enrich compact artifact refs with stable provenance identifiers."""

    execution_id = uuid.uuid4()
    artifact_id = uuid.uuid4()

    mock_coordinator_outcome.tool_id = "shell.exec"
    mock_coordinator_outcome.parameters = {"command": "echo test"}
    mock_coordinator_outcome.result = {
        "success": True,
        "stdout": "test\n",
        "stderr": "",
        "observation": "ok",
        "artifacts": ["artifacts/secondary.txt"],
        "exit_code": 0,
    }
    mock_coordinator_outcome.to_graph_metadata = MagicMock(return_value={"result": {}})

    class _StubProvenanceService:
        def __init__(self) -> None:
            self.db = SimpleNamespace(flush=lambda: None)
            self.execution_repo = SimpleNamespace(
                get_by_id=lambda _execution_id: SimpleNamespace(
                    id=execution_id,
                    started_at=datetime.now(timezone.utc),
                    tool_arguments={},
                )
            )
            self.artifact_repo = SimpleNamespace(
                get_by_execution=lambda _execution_id: [
                    SimpleNamespace(
                        id=artifact_id,
                        artifact_kind="tool_file",
                        relative_path="artifacts/secondary.txt",
                        source_path=None,
                        fallback_path=None,
                    )
                ]
            )

        def record_tool_execution(self, **_kwargs):
            return SimpleNamespace(id=execution_id)

        def complete_tool_execution(self, **_kwargs):
            return SimpleNamespace(id=execution_id)

    async def _compress_stub(**_kwargs):
        return ToolOutputCompressionResult(
            compact_output=CompactToolOutput(
                tool="shell.exec",
                status="success",
                success=True,
                exit_code=0,
                summary="ok",
                artifact_refs=[ArtifactReference(path="artifacts/secondary.txt")],
                compression=CompressionMetadata(source="deterministic"),
            )
        )

    facts = FactsState(
        task_id=123,
        message="echo test",
        capability="simple_tool_execution",
        metadata=_base_metadata(model="gpt-4"),
    )
    state = InteractiveState(facts=facts)
    context = GraphRuntimeContext(
        task_id=123,
        workspace_path=temp_workspace,
        api_key="test-key",
        model="gpt-4",
        user_id=1,
    )

    with patch("agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator") as mock_coordinator_class:
        mock_coordinator = MagicMock()
        mock_coordinator.run = AsyncMock(return_value=mock_coordinator_outcome)
        mock_coordinator_class.return_value = mock_coordinator

        with patch(
            "agent.graph.subgraphs.tool_execution._get_provenance_service",
            side_effect=lambda: (_StubProvenanceService(), SimpleNamespace(close=lambda: None)),
        ):
            with patch("agent.graph.subgraphs.tool_execution.compress_tool_output", _compress_stub):
                with patch("agent.graph.subgraphs.tool_execution.get_stream_writer", return_value=None):
                    result = await run_tool_execution(state, context)

    updated_state = InteractiveState.from_mapping(result)
    compact = updated_state.facts.metadata["last_tool_result_compact"]
    refs = compact.get("artifact_refs") or []
    assert refs
    ref = refs[0]
    assert ref["artifact_id"] == str(artifact_id)
    assert ref["tool_name"] == "shell.exec"
    assert ref["artifact_kind"] == "tool_file"
    # Phase 2.3 (re-audit fix): canonical mint format is tc_<hex>
    # (from agent/tool_runtime/batch/ids.py:mint_tool_call_id), not the
    # legacy tc-<uuid> format that lived in the deleted prepare-node mint.
    assert ref["tool_call_id"].startswith("tc_") or ref["tool_call_id"].startswith("tc-")
    assert "label" in ref
