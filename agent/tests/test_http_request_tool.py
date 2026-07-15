"""Unit coverage for the HTTP request tool command, parsing, and artifacts."""

from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "http_tools"


def _fixture_text(*parts: str) -> str:
    return (FIXTURE_ROOT / Path(*parts)).read_text(encoding="utf-8")


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


def test_build_command_includes_safe_flags_and_headers():
    tool = HttpRequestTool()
    args = HttpRequestArgs(
        target="https://example.com/api",
        method="POST",
        headers={"Authorization": "Bearer abc", "X-Test": "yes"},
        body='{"k":"v"}',
        content_type="application/json",
        timeout=20,
        follow_redirects=True,
    )

    cmd = tool.build_command(args)
    assert cmd[0] == "curl"
    assert "--request" in cmd and "POST" in cmd
    assert "--include" in cmd
    assert "--max-time" in cmd and "20" in cmd
    assert "--header" in cmd
    assert "--data" in cmd and '{"k":"v"}' in cmd
    assert "--write-out" in cmd
    assert cmd[-1] == "https://example.com/api"


def test_build_command_uses_curl_head_mode_for_head_requests():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com/download/4", method="HEAD")

    cmd = tool.build_command(args)

    assert "--head" in cmd
    assert "--request" not in cmd
    assert "HEAD" not in cmd


def test_head_request_with_content_length_does_not_fail_as_partial_transfer():
    class HeadHandler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "24")
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = socketserver.TCPServer(("127.0.0.1", 0), HeadHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = HttpRequestTool().run(
            HttpRequestArgs(
                target=f"http://127.0.0.1:{server.server_address[1]}/download/4",
                method="HEAD",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert result.success is True
    assert result.exit_code == 0
    assert result.metadata["status_code"] == 200
    assert result.metadata["content_length"] == 24


def test_successful_http_body_error_text_does_not_trip_cli_failure_detection():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com/api")
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 31\r\n\r\n"
        "error: application-level message\n"
        "__DROWAI_HTTP_META__200\thttps://example.com/api\ttext/plain\t31\t0\t0.01"
    )

    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)

    assert metadata["execution_outcome"] == "succeeded"
    assert tool.is_success_exit_code(
        0,
        args,
        stdout=stdout,
        stderr="",
        parsed_metadata=metadata,
    ) is True


def test_parse_output_uses_fixture_and_redacts_sensitive_values():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com", headers={"Authorization": "Bearer abc"})
    stdout = _fixture_text("request", "success_json_response.txt")

    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert metadata["status_code"] == 200
    assert metadata["content_type"] == "application/json"
    assert metadata["redirect_count"] == 0
    assert metadata["request_headers"]["Authorization"] == "<REDACTED>"
    assert metadata["response_headers"]["Set-Cookie"] == "<REDACTED>"
    assert "<REDACTED>" in rendered_stdout
    assert "abc.def.ghi" not in rendered_stdout


def test_parse_output_html_fixture_redacts_body_content():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://www.example.com/")
    stdout = _fixture_text("request", "success_html_response.txt")

    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert metadata["status_code"] == 200
    assert metadata["content_type"] == "text/html; charset=utf-8"
    assert "super-token" not in rendered_stdout
    assert "<REDACTED>" in rendered_stdout


def test_artifacts_created_when_body_is_truncated(workspace: Path):
    tool = HttpRequestTool()
    body = "X" * 2048
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
        f"{body}\n"
        "__DROWAI_HTTP_META__200\thttps://example.com/a\ttext/plain\t2048\t0\t0.01"
    )
    args = HttpRequestArgs(target="https://example.com/a", max_body_bytes=1024, capture_body=True)

    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    artifacts = tool.create_artifacts(stdout=stdout, args=args, timestamp=1234)

    assert metadata["body_truncated"] is True
    assert any("http_request_1234.txt" in path for path in artifacts)
    assert (workspace / "artifacts" / "http_request_1234.txt").exists()


def test_parse_output_redirect_chain_fixture():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com")
    stdout = _fixture_text("request", "redirect_chain_headers.txt")

    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    assert metadata["status_code"] == 200
    assert metadata["effective_url"] == "https://example.com/new"
    assert metadata["redirect_count"] == 1
    assert metadata["content_length"] == 12


def test_run_timeout_returns_controlled_failure(monkeypatch: pytest.MonkeyPatch):
    tool = HttpRequestTool()

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["curl"], timeout=1)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    result = tool.run(HttpRequestArgs(target="https://example.com", timeout=1))

    assert result.success is False
    assert result.exit_code == -2
    assert "timed out" in result.stderr.lower()


def test_invalid_url_returns_structured_validation_result():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {"target": "ftp://example.com"},
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"
    assert "http or https" in result.stderr.lower()


@pytest.mark.parametrize(
    ("fixture_name", "exit_code", "expected_fragment"),
    [
        ("timeout_stderr.txt", 28, "(28)"),
        ("tls_error_stderr.txt", 60, "(60)"),
    ],
)
def test_run_propagates_curl_stderr_failures_from_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    exit_code: int,
    expected_fragment: str,
):
    tool = HttpRequestTool()
    stderr = _fixture_text("request", fixture_name)

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["curl"], returncode=exit_code, stdout=b"", stderr=stderr.encode("utf-8"))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = tool.run(HttpRequestArgs(target="https://example.com"))

    assert result.success is False
    assert result.exit_code == exit_code
    assert expected_fragment in result.stderr
    assert result.metadata["curl_exit_code"] == exit_code
