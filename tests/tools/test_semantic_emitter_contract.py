"""Contract tests for optional semantic emitter transport behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pydantic import BaseModel

from agent.tool_runtime.transport_router import (
    build_pty_tool_result,
    execute_single_tool_with_fallback,
)
from agent.tool_runtime.result_enrichment import merge_semantic_emitter_metadata
from agent.tools.base_tool import BaseTool, ToolPostprocessResult
from agent.tools.schemas import ToolResult


class _Args(BaseModel):
    command: str


class _EmitterTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        metadata = self.parse_output("scan output", "", 0, args)
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="scan output",
            stderr="",
            metadata=metadata,
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {"parsed": True}

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return [{"observation_type": "test.semantic"}]

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return [
            {
                "type": "result_summary",
                "name": "http_status",
                "value": "200",
                "detail": {"unit": "code"},
            }
        ]


class _LegacyTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        metadata = self.parse_output("legacy output", "", 0, args)
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="legacy output",
            stderr="",
            metadata=metadata,
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {"legacy": True}


class _PostprocessArtifactTool(BaseTool):
    args_model = _Args

    def __init__(self) -> None:
        self._ready_for_artifacts = False

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def postprocess_execution(
        self,
        *,
        args: BaseModel,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Any = None,
    ) -> ToolPostprocessResult:
        _ = args, runtime_context
        self._ready_for_artifacts = True
        return ToolPostprocessResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            metadata=dict(metadata or {}),
            artifacts=list(artifacts or []) + ["postprocess-artifact"],
        )

    def create_artifacts(self, stdout: str, args: BaseModel, timestamp: int | None = None) -> List[str]:
        _ = stdout, args, timestamp
        if not self._ready_for_artifacts:
            return []
        return ["created-artifact"]


class _ArtifactOnlyTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def create_artifacts(self, stdout: str, args: BaseModel, timestamp: int | None = None) -> List[str]:
        _ = stdout, args, timestamp
        return ["artifact-only"]


class _EvidenceMergeTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {
            "legacy": True,
            "semantic_evidence": [
                {
                    "type": "result_summary",
                    "name": "http_status",
                    "value": "200",
                    "detail": {"unit": "code"},
                    "source": "legacy",
                },
                {
                    "type": "diagnostic",
                    "name": "warning_count",
                    "value": 3,
                    "detail": {"severity": "low"},
                    "source": "legacy",
                },
            ],
        }

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return [
            {
                "type": "result_summary",
                "name": "http_status",
                "value": "200",
                "detail": {"unit": "code"},
                "source": "emitter",
            },
            {
                "type": "baseline",
                "name": "total_requests",
                "value": 100,
                "detail": {"unit": "requests"},
                "source": "emitter",
            },
        ]


class _UnknownTypeEvidenceTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {}

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return [
            {
                "type": "not_a_vocab_type",
                "name": "bad_type",
                "value": "ignored",
            }
        ]


class _LegacyOnlyEvidenceTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {
            "semantic_evidence": [
                {
                    "type": "diagnostic",
                    "name": "legacy_signal",
                    "value": 1,
                }
            ]
        }

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return []


class _LegacyGapEvidenceTool(BaseTool):
    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {
            "semantic_evidence": [
                {
                    "type": "diagnostic",
                    "name": "legacy_only_warning",
                    "value": True,
                    "detail": {"severity": "warning"},
                },
                {
                    "type": "result_summary",
                    "name": "http_status",
                    "value": "200",
                    "detail": {"unit": "code"},
                },
            ]
        }

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        return [
            {
                "type": "result_summary",
                "name": "http_status",
                "value": "200",
                "detail": {"unit": "code"},
            }
        ]


class _ParsedMetadataPopTool(BaseTool):
    args_model = _Args

    def __init__(self) -> None:
        self.last_parsed_metadata: Dict[str, Any] | None = None

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        parsed = {
            "parsed": True,
            "semantic_evidence": [
                {
                    "type": "diagnostic",
                    "name": "legacy_signal",
                    "value": 1,
                }
            ],
        }
        self.last_parsed_metadata = parsed
        return parsed


class _EvidenceEmitterRaisesTool(BaseTool):
    """Tool whose ``emit_semantic_evidence`` always raises.

    Used to pin the guide's compatibility contract: an emitter exception must
    not cause the legacy ``parsed_metadata['semantic_evidence']`` to be lost.
    """

    args_model = _Args

    def run(self, args: BaseModel) -> ToolResult:
        _ = args
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            metadata={},
            artifacts=[],
            execution_time=0.01,
        )

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: BaseModel) -> Dict[str, Any]:
        _ = stdout, stderr, exit_code, args
        return {
            "legacy": True,
            "semantic_evidence": [
                {
                    "type": "diagnostic",
                    "name": "legacy_signal",
                    "value": 1,
                }
            ],
        }

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BaseModel,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code, args, metadata
        raise RuntimeError("emitter intentionally failed")


def test_pty_result_includes_semantic_observations() -> None:
    tool = _EmitterTool()
    args = _Args(command="echo test")
    shell_result = SimpleNamespace(stdout="scan output", stderr="", exit_code=0, status="success")

    result = build_pty_tool_result(
        tool=tool,
        args=args,
        shell_result=shell_result,
        command="echo test",
        host_workspace_path=".",
    )

    assert result.metadata["parsed"] is True
    assert result.metadata["semantic_observations"] == [{"observation_type": "test.semantic"}]
    assert result.metadata["semantic_evidence"] == [
        {
            "type": "result_summary",
            "name": "http_status",
            "value": "200",
            "detail": {"unit": "code"},
        }
    ]
    assert "tool_metadata" not in result.metadata


def test_evidence_attached_on_pty_path() -> None:
    test_pty_result_includes_semantic_observations()


def test_pty_result_without_emitter_preserves_legacy_metadata() -> None:
    tool = _LegacyTool()
    args = _Args(command="echo legacy")
    shell_result = SimpleNamespace(stdout="legacy output", stderr="", exit_code=0, status="success")

    result = build_pty_tool_result(
        tool=tool,
        args=args,
        shell_result=shell_result,
        command="echo legacy",
        host_workspace_path=".",
    )

    assert result.metadata == {"legacy": True}
    assert "semantic_observations" not in result.metadata


def test_pty_result_postprocess_can_enable_artifact_creation() -> None:
    tool = _PostprocessArtifactTool()
    args = _Args(command="echo artifact")
    shell_result = SimpleNamespace(stdout="ok", stderr="", exit_code=0, status="success")

    result = build_pty_tool_result(
        tool=tool,
        args=args,
        shell_result=shell_result,
        command="echo artifact",
        host_workspace_path=".",
    )

    assert result.artifacts == ["postprocess-artifact", "created-artifact"]


def test_pty_result_artifact_only_tool_preserves_existing_behavior() -> None:
    tool = _ArtifactOnlyTool()
    args = _Args(command="echo artifact")
    shell_result = SimpleNamespace(stdout="ok", stderr="", exit_code=0, status="success")

    result = build_pty_tool_result(
        tool=tool,
        args=args,
        shell_result=shell_result,
        command="echo artifact",
        host_workspace_path=".",
    )

    assert result.artifacts == ["artifact-only"]


@pytest.mark.asyncio
async def test_direct_route_attaches_semantic_observations(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("agent.tools.tool_registry.get_tool", lambda _tool_id: _EmitterTool)
    config = SimpleNamespace(task_id=7, workspace_path=str(tmp_path), tool_execution_timeout=60, nmap_timeout=60)

    result = await execute_single_tool_with_fallback(
        tool_id="knowledge.cve_lookup",
        parameters={"command": "echo test"},
        config=config,
        logger=None,
        file_comm=None,
        validate_tool_parameters_fn=None,
        build_validation_error_result_fn=None,
        should_use_pty_fn=lambda _tool_id, _params: False,
        execute_via_pty_fn=None,
        execute_tool_via_comm_fn=None,
        run_tool_by_name_fn=lambda _tool_id, _params: ToolResult(
            success=True,
            exit_code=0,
            stdout="scan output",
            stderr="",
            metadata={"existing_key": "keep"},
            artifacts=[],
            execution_time=0.01,
        ),
        safe_inc_metric_fn=lambda _name: None,
        pty_session_not_available_exc_type=None,
    )

    assert result.success is True
    assert result.metadata["existing_key"] == "keep"
    assert result.metadata["parsed"] is True
    assert result.metadata["semantic_observations"] == [{"observation_type": "test.semantic"}]
    assert result.metadata["semantic_evidence"] == [
        {
            "type": "result_summary",
            "name": "http_status",
            "value": "200",
            "detail": {"unit": "code"},
        }
    ]
    assert "tool_metadata" not in result.metadata


@pytest.mark.asyncio
async def test_evidence_attached_on_direct_path(monkeypatch, tmp_path) -> None:
    await test_direct_route_attaches_semantic_observations(monkeypatch, tmp_path)


def test_emitter_unknown_type_dropped_does_not_raise() -> None:
    tool = _UnknownTypeEvidenceTool()
    args = _Args(command="echo unknown")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    assert "semantic_evidence" not in metadata


def test_legacy_parsed_metadata_semantic_evidence_still_accepted() -> None:
    tool = _LegacyOnlyEvidenceTool()
    args = _Args(command="echo legacy")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    assert metadata["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "legacy_signal",
            "value": 1,
            "detail": {},
        }
    ]


def test_merge_precedence_emitter_wins_on_identity_collision() -> None:
    tool = _EvidenceMergeTool()
    args = _Args(command="echo merge")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    assert metadata.get("legacy") is True
    assert metadata["semantic_evidence"] == [
        {
            "type": "result_summary",
            "name": "http_status",
            "value": "200",
            "detail": {"unit": "code"},
            "source": "emitter",
        },
        {
            "type": "baseline",
            "name": "total_requests",
            "value": 100,
            "detail": {"unit": "requests"},
            "source": "emitter",
        },
        {
            "type": "diagnostic",
            "name": "warning_count",
            "value": 3,
            "detail": {"severity": "low"},
            "source": "legacy",
        },
    ]


def test_merge_legacy_fills_gaps_but_never_duplicates() -> None:
    tool = _LegacyGapEvidenceTool()
    args = _Args(command="echo merge")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    evidence = metadata["semantic_evidence"]
    assert len([entry for entry in evidence if entry["name"] == "http_status"]) == 1
    assert any(entry["name"] == "legacy_only_warning" for entry in evidence)


def test_parsed_metadata_semantic_evidence_removed_after_merge() -> None:
    tool = _ParsedMetadataPopTool()
    args = _Args(command="echo merge")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    assert "semantic_evidence" in metadata
    assert tool.last_parsed_metadata is not None
    assert "semantic_evidence" not in tool.last_parsed_metadata


def test_legacy_parsed_evidence_survives_when_emitter_raises() -> None:
    """Emitter exceptions must not swallow the legacy parsed evidence fallback."""
    tool = _EvidenceEmitterRaisesTool()
    args = _Args(command="echo legacy-fallback")

    metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout="ok",
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    assert metadata.get("legacy") is True
    assert metadata["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "legacy_signal",
            "value": 1,
            "detail": {},
        }
    ]
