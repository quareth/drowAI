""" tests for artifact tool schemas and runtime task-scoped adapters."""

from __future__ import annotations

from contextlib import contextmanager

from backend.services.artifact.memory_service import (
    ArtifactCatalogEntry,
    ArtifactCatalogPage,
    ArtifactReadResult,
)
from agent.tool_runtime.runtime_context import (
    bind_tool_runtime_context,
    build_tool_runtime_context,
)
from agent.tools.artifact.contracts import ArtifactReadArgs, ArtifactSearchArgs
from agent.tools.tool_registry import available_tools, get_tool


def test_artifact_search_schema_is_task_safe() -> None:
    schema = ArtifactSearchArgs.model_json_schema()
    properties = schema.get("properties", {})

    assert "task_id" not in properties
    assert "path" not in properties

    args = ArtifactSearchArgs()
    assert args.limit == 20
    assert args.offset == 0


def test_artifact_read_schema_is_excerpt_first_and_task_safe() -> None:
    schema = ArtifactReadArgs.model_json_schema()
    properties = schema.get("properties", {})

    assert "task_id" not in properties
    assert "path" not in properties
    assert properties["mode"]["default"] == "auto"

    args = ArtifactReadArgs(artifact_id="artifact-123")
    assert args.mode == "auto"
    assert args.max_chars == 4000


def test_artifact_tools_are_registry_compatible() -> None:
    tools = set(available_tools())
    assert "artifact.search" in tools
    assert "artifact.read" in tools

    search_cls = get_tool("artifact.search")
    read_cls = get_tool("artifact.read")
    search_tool = search_cls()
    read_tool = read_cls()

    missing_context_search = search_tool.validate_and_run({"limit": 1})
    missing_context_read = read_tool.validate_and_run({"artifact_id": "artifact-123"})
    assert missing_context_search.success is False
    assert missing_context_search.exit_code == 2
    assert "requires active runtime task context" in missing_context_search.stderr
    assert missing_context_read.success is False
    assert missing_context_read.exit_code == 2
    assert "requires active runtime task context" in missing_context_read.stderr

    class _FakeMemoryService:
        def search_task_artifacts(self, *, task_id: int, filters) -> ArtifactCatalogPage:  # noqa: ANN001
            assert task_id == 7
            assert filters.query == "scan"
            return ArtifactCatalogPage(
                artifacts=(
                    ArtifactCatalogEntry(
                        artifact_id="art-1",
                        execution_id="exec-1",
                        tool_call_id="tc-1",
                        tool_name="shell.exec",
                        task_id=task_id,
                        artifact_kind="stdout",
                        relative_path="artifacts/out.txt",
                        turn_id="turn-1",
                        turn_sequence=4,
                        byte_size=100,
                        mime_type="text/plain",
                        content_availability="available",
                        label="stdout from shell.exec (turn 4)",
                        created_at=None,
                    ),
                ),
                total=1,
                limit=20,
                offset=0,
            )

        def read_task_artifact(self, *, task_id: int, artifact_id: str, request) -> ArtifactReadResult:  # noqa: ANN001
            assert task_id == 7
            assert artifact_id == "art-1"
            assert request.mode == "auto"
            return ArtifactReadResult(
                status="ready",
                artifact_id="art-1",
                content="artifact body",
                content_availability="available",
                mode_used="head",
                truncated=False,
                source="inline_db",
                artifact=None,
            )

    @contextmanager
    def _fake_memory_session():
        yield _FakeMemoryService()

    import agent.tools.artifact.search as search_module
    import agent.tools.artifact.read as read_module

    with bind_tool_runtime_context(build_tool_runtime_context(task_id=7)):
        from unittest.mock import patch

        with patch.object(search_module, "artifact_memory_session", _fake_memory_session):
            search_result = search_tool.validate_and_run({"query": "scan", "limit": 20})
        with patch.object(read_module, "artifact_memory_session", _fake_memory_session):
            read_result = read_tool.validate_and_run({"artifact_id": "art-1"})

    assert search_result.success is True
    assert search_result.exit_code == 0
    assert "art-1" in search_result.stdout
    assert search_result.metadata["artifact_search"]["catalog_page"]["total"] == 1

    assert read_result.success is True
    assert read_result.exit_code == 0
    assert read_result.stdout == "artifact body"
    assert read_result.metadata["artifact_read"]["status"] == "ready"


def test_artifact_tools_shape_service_failures_as_tool_errors() -> None:
    class _ExplodingMemoryService:
        def search_task_artifacts(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("db unavailable")

        def read_task_artifact(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("db unavailable")

    @contextmanager
    def _exploding_memory_session():
        yield _ExplodingMemoryService()

    import agent.tools.artifact.search as search_module
    import agent.tools.artifact.read as read_module

    search_tool = get_tool("artifact.search")()
    read_tool = get_tool("artifact.read")()

    with bind_tool_runtime_context(build_tool_runtime_context(task_id=7)):
        from unittest.mock import patch

        with patch.object(search_module, "artifact_memory_session", _exploding_memory_session):
            search_result = search_tool.validate_and_run({"query": "scan", "limit": 20})
        with patch.object(read_module, "artifact_memory_session", _exploding_memory_session):
            read_result = read_tool.validate_and_run({"artifact_id": "art-1"})

    assert search_result.success is False
    assert search_result.exit_code == 1
    assert search_result.metadata["artifact_search"]["status"] == "error"
    assert "artifact.search failed" in search_result.stderr

    assert read_result.success is False
    assert read_result.exit_code == 1
    assert read_result.metadata["artifact_read"]["status"] == "error"
    assert "artifact.read failed" in read_result.stderr
