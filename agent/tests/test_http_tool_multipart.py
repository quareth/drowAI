"""Multipart request coverage for HTTP request tool behavior and parity."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.executor import EnhancedCommandExecutor
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


class MockShellCommandResult:
    """Small PTY result stub matching executor expectations."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0, status: str = "success"):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.status = status


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    previous = os.environ.get("WORKSPACE")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(cwd)
        if previous is None:
            monkeypatch.delenv("WORKSPACE", raising=False)
        else:
            monkeypatch.setenv("WORKSPACE", previous)


def _mock_config(task_id: int, workspace_path: str) -> MagicMock:
    cfg = MagicMock()
    cfg.task_id = task_id
    cfg.workspace_path = workspace_path
    cfg.openai_api_key = "test-key"
    cfg.model_name = "gpt-4"
    cfg.individual_tool_timeout = 60
    cfg.tool_execution_timeout = 60
    return cfg


def _multipart_stdout() -> str:
    return (
        "HTTP/1.1 201 Created\r\n"
        "Content-Type: application/json\r\n\r\n"
        '{"ok": true}\n'
        "__DROWAI_HTTP_META__201\thttps://example.com/upload\tapplication/json\t12\t0\t0.02"
    )


def test_http_request_builds_multipart_form_and_sets_metadata(workspace: Path):
    (workspace / "uploads").mkdir(parents=True, exist_ok=True)
    (workspace / "uploads" / "sample.txt").write_text("payload", encoding="utf-8")

    tool = HttpRequestTool()
    args = HttpRequestArgs(
        target="https://example.com/upload",
        form_fields={"note": "hello"},
        form_files={"file": "uploads/sample.txt"},
    )
    cmd = tool.build_command(args)
    metadata = tool.parse_output(stdout=_multipart_stdout(), stderr="", exit_code=0, args=args)

    assert "--form" in cmd
    assert "note=hello" in cmd
    assert "file=@uploads/sample.txt" in cmd
    assert "--request" in cmd and "POST" in cmd
    assert metadata["multipart_used"] is True
    assert metadata["request_method"] == "POST"


def test_http_request_rejects_body_with_multipart_schema_validation():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com/upload",
            "body": '{"bad":"mix"}',
            "form_fields": {"name": "x"},
        },
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"
    assert "cannot be combined" in result.stderr.lower()


def test_http_request_rejects_missing_upload_file_with_validation_error(workspace: Path):
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com/upload",
            "form_files": {"file": "uploads/missing.txt"},
        },
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"
    assert "must point to an existing file" in result.stderr.lower()


def test_http_request_multipart_pty_path_matches_direct_metadata_shape(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    (workspace / "uploads").mkdir(parents=True, exist_ok=True)
    (workspace / "uploads" / "sample.txt").write_text("payload", encoding="utf-8")

    args = {
        "target": "https://example.com/upload",
        "form_fields": {"note": "hello"},
        "form_files": {"file": "uploads/sample.txt"},
        "transport": "pty",
    }

    mocked_pty = AsyncMock(
        return_value=MockShellCommandResult(stdout=_multipart_stdout(), stderr="", exit_code=0)
    )
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    executor = EnhancedCommandExecutor(config=_mock_config(task_id=22, workspace_path=str(workspace)))
    pty_result = asyncio.run(
        executor._execute_via_pty(
            "information_gathering.web_enumeration.http_request",
            args,
        )
    )
    direct_tool = HttpRequestTool()
    direct_args = HttpRequestArgs(
        target="https://example.com/upload",
        form_fields={"note": "hello"},
        form_files={"file": "uploads/sample.txt"},
    )
    direct_tool.build_command(direct_args)
    direct_metadata = direct_tool.parse_output(stdout=_multipart_stdout(), stderr="", exit_code=0, args=direct_args)

    assert pty_result.success is True
    assert pty_result.metadata["multipart_used"] is True
    assert direct_metadata["multipart_used"] is True
    assert pty_result.metadata["request_method"] == direct_metadata["request_method"] == "POST"

    called_command = mocked_pty.call_args.kwargs["command"]
    assert "--form note=hello" in called_command
    assert "--form file=@uploads/sample.txt" in called_command
