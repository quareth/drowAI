"""Trace and debug artifact coverage for HTTP request/download tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import agent.tools.information_gathering.web_enumeration.http_request as http_request_module
from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs
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


def test_http_request_rejects_header_dump_and_trace_output_controls():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "dump_headers_artifact": True,
            "trace_mode": "trace_ascii",
        },
    )

    assert result.success is False
    assert "dump_headers_artifact" in result.stderr
    assert "trace_mode" in result.stderr
    assert "extra inputs" in result.stderr.lower()


def test_http_request_does_not_emit_header_dump_or_trace_flags(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            assert "--dump-header" not in cmd
            assert "--trace" not in cmd
            assert "--trace-ascii" not in cmd
            stdout = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok\n"
                "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.01"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpRequestTool()
        result = tool.run(
            HttpRequestArgs(
                target="https://example.com",
            )
        )

        assert result.success is True
        assert result.metadata["trace_mode"] == "none"
        assert result.artifacts == []
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_trace_mode_does_not_create_artifacts(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            work = Path(cwd or ".")
            output_path = work / cmd[cmd.index("--output") + 1]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ok")
            assert "--trace" not in cmd
            stdout = "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://example.com/file.bin\t2\t0\t0.01"
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpDownloadTool()
        result = tool.run(
            HttpDownloadArgs(
                target="https://example.com/file.bin",
                output_path="files/file.bin",
                trace_mode="trace",
            )
        )

        assert result.success is True
        assert result.metadata["trace_mode"] == "none"
        assert result.artifacts == []
        assert not Path("artifacts/http_download_trace.log").exists()
    finally:
        _workspace_teardown(previous, cwd)


def test_trace_artifact_path_requires_trace_mode():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "trace_artifact": "artifacts/custom_trace.log",
        },
    )
    assert result.success is False
    assert "trace_artifact" in result.stderr
    assert "extra inputs" in result.stderr.lower()


def test_trace_artifact_outside_workspace_is_rejected(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        result = run_tool_by_name(
            "information_gathering.web_enumeration.http_download",
            {
                "target": "https://example.com/file.bin",
                "output_path": "f.bin",
                "trace_mode": "trace",
                "trace_artifact": "../outside.log",
            },
        )
        assert result.success is False
        assert result.exit_code == -1
        assert result.metadata.get("error_type") == "validation_error"
    finally:
        _workspace_teardown(previous, cwd)


def test_http_request_trace_mode_is_rejected():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "trace_mode": "trace_ascii",
        },
    )

    assert result.success is False
    assert "trace_mode" in result.stderr
    assert "extra inputs" in result.stderr.lower()


def test_http_request_has_no_trace_artifact_redaction_path(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            assert "--trace-ascii" not in cmd
            stdout = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok\n"
                "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.01"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpRequestTool()
        result = tool.run(
            HttpRequestArgs(
                target="https://example.com",
            )
        )

        assert result.success is True
        assert not any("http_request_trace_" in path for path in result.artifacts)
        assert not list(Path("artifacts").glob("http_request_trace_*.log"))
    finally:
        _workspace_teardown(previous, cwd)


def test_http_request_text_mode_artifacts_are_redacted(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        body_secret = "Authorization: Bearer very-secret-token\n" + ("A" * 1500)

        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            stdout = (
                "HTTP/1.1 200 OK\r\n"
                "Set-Cookie: session=abc123\r\n"
                "Content-Type: text/plain\r\n\r\n"
                f"{body_secret}\n"
                "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t1540\t0\t0.01"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpRequestTool()
        result = tool.run(
            HttpRequestArgs(
                target="https://example.com",
                capture_body=True,
                max_body_bytes=1024,
            )
        )

        assert result.success is True
        body_artifact = next(
            path for path in result.artifacts if path.startswith("artifacts/http_request_") and not path.endswith("_headers.txt")
        )
        header_artifact = next(path for path in result.artifacts if path.endswith("_headers.txt"))
        body_text = Path(body_artifact).read_text(encoding="utf-8")
        header_text = Path(header_artifact).read_text(encoding="utf-8")

        assert "very-secret-token" not in body_text
        assert "session=abc123" not in header_text
        assert "<REDACTED>" in body_text
        assert "<REDACTED>" in header_text
    finally:
        _workspace_teardown(previous, cwd)


def test_http_request_text_mode_artifacts_are_skipped_when_redaction_fails(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        original_redact = http_request_module.redact_text_secrets

        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            stdout = (
                "HTTP/1.1 200 OK\r\n"
                "Set-Cookie: session=abc123\r\n"
                "Content-Type: text/plain\r\n\r\n"
                f"{'A' * 1500}FAIL_REDACT body\n"
                "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t1516\t0\t0.01"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        def _redact_guard(text: str) -> str:
            if "FAIL_REDACT" in text:
                raise ValueError("simulated redaction failure")
            return original_redact(text)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(http_request_module, "redact_text_secrets", _redact_guard)
        tool = HttpRequestTool()
        result = tool.run(
            HttpRequestArgs(
                target="https://example.com",
                capture_body=True,
                max_body_bytes=1024,
            )
        )

        assert result.success is True
        assert not any(
            path.startswith("artifacts/http_request_") and path.endswith(".txt") and not path.endswith("_headers.txt")
            for path in result.artifacts
        )
        assert not [
            path
            for path in Path("artifacts").glob("http_request_*.txt")
            if not path.name.endswith("_headers.txt")
        ]
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_does_not_emit_trace_artifact_files(tmp_path: Path, monkeypatch):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
            if list(cmd[:2]) == ["curl", "--version"]:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
                    "",
                )
            work = Path(cwd or ".")
            output_path = work / cmd[cmd.index("--output") + 1]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ok")
            assert "--trace" not in cmd
            stdout = "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://example.com/file.bin\t2\t0\t0.01"
            return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        tool = HttpDownloadTool()
        result = tool.run(
            HttpDownloadArgs(
                target="https://example.com/file.bin",
                output_path="files/file.bin",
                trace_mode="trace",
            )
        )

        assert result.success is True
        assert result.metadata["trace_mode"] == "none"
        assert not any(path.endswith("http_download_trace.log") for path in result.artifacts)
        assert not Path("artifacts/http_download_trace.log").exists()
    finally:
        _workspace_teardown(previous, cwd)
