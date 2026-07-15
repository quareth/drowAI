"""Binary request/response mode coverage for HTTP request tool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


def _workspace_setup(tmp_path: Path):
    previous = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    cwd = Path.cwd()
    os.chdir(tmp_path)
    return previous, cwd


def _workspace_teardown(previous: str | None, cwd: Path):
    os.chdir(cwd)
    if previous is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = previous


def test_binary_body_sources_are_mutually_exclusive():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "body": "x",
            "body_base64": "eA==",
        },
    )
    assert result.success is False
    assert "mutually exclusive" in result.stderr.lower()


def test_body_file_path_maps_to_data_binary_and_sets_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        (tmp_path / "payloads").mkdir(parents=True, exist_ok=True)
        (tmp_path / "payloads" / "blob.bin").write_bytes(b"\x00\x01\x02")
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com/upload",
            body_file_path="payloads/blob.bin",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(
            stdout="HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok\n__DROWAI_HTTP_META__200\thttps://example.com/upload\ttext/plain\t2\t0\t0.01",
            stderr="",
            exit_code=0,
            args=args,
        )

        assert "--data-binary" in cmd
        assert "@payloads/blob.bin" in cmd
        assert metadata["binary_body_used"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_body_base64_declares_runtime_file_without_command_build_write(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com/upload",
            body_base64="AAEC",
            transport="file-comm",
        )
        cmd = tool.build_command(args)
        workspace_files = tool.prepare_workspace_files(args)

        body_operand = cmd[cmd.index("--data-binary") + 1]
        relative_path = body_operand.removeprefix("@")
        assert relative_path.startswith("artifacts/http_request_body_base64_")
        assert not (tmp_path / relative_path).exists()
        assert len(workspace_files) == 1
        assert workspace_files[0].relative_path == relative_path
        assert workspace_files[0].content_bytes() == b"\x00\x01\x02"
    finally:
        _workspace_teardown(previous, cwd)


def test_response_file_modes_are_rejected():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com/bin",
            "response_mode": "artifact_only",
        },
    )

    assert result.success is False
    assert "response_mode" in result.stderr
    assert "extra inputs" in result.stderr.lower()


def test_http_request_binary_response_stays_on_stdout(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            assert "--dump-header" not in cmd
            assert "--output" not in cmd
            stdout = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
                b"\x00\x10\x20\x30\n"
                b"__DROWAI_HTTP_META__200\thttps://example.com/bin\tapplication/octet-stream\t4\t0\t0.02"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout, b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpRequestTool()
        result = tool.run(
            HttpRequestArgs(
                target="https://example.com/bin",
            )
        )

        assert result.success is True
        assert result.metadata["response_mode"] == "text"
        assert result.metadata["binary_response_detected"] is True
        assert not any("http_request_response_body_" in path for path in result.artifacts)
    finally:
        _workspace_teardown(previous, cwd)


def test_base64_preview_response_mode_is_rejected():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com/bin",
            "response_mode": "base64_preview",
        },
    )

    assert result.success is False
    assert "response_mode" in result.stderr
    assert "extra inputs" in result.stderr.lower()
