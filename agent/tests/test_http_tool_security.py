"""Security-focused tests for HTTP tool redaction and boundary enforcement."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent.tools.information_gathering.web_enumeration._helpers import validate_http_url
from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs


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


def test_validate_http_url_rejects_non_http_schemes():
    with pytest.raises(ValueError):
        validate_http_url("ftp://example.com/file.txt")
    with pytest.raises(ValueError):
        validate_http_url("file:///etc/passwd")


def test_http_request_redacts_sensitive_headers_and_bearer():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com", headers={"Authorization": "Bearer very-secret"})
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Set-Cookie: sessionid=abc123\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "authorization: Bearer very-secret\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t30\t0\t0.05"
    )
    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert metadata["request_headers"]["Authorization"] == "<REDACTED>"
    assert metadata["response_headers"]["Set-Cookie"] == "<REDACTED>"
    assert "very-secret" not in rendered_stdout
    assert "<REDACTED>" in rendered_stdout


def test_http_request_redacts_api_key_header_lines_and_url_credentials():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://user:pass@example.com/secure", headers={"X-Api-Key": "shh"})
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "X-Api-Key: server-secret\r\n\r\n"
        "x-api-key: server-secret\n"
        "__DROWAI_HTTP_META__200\thttps://user:pass@example.com/secure\ttext/plain\t18\t0\t0.02"
    )
    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert metadata["request_headers"]["X-Api-Key"] == "<REDACTED>"
    assert metadata["effective_url"] == "https://<REDACTED>@example.com/secure"
    assert "server-secret" not in rendered_stdout
    assert "<REDACTED>" in rendered_stdout


def test_http_request_redacts_basic_and_bearer_auth_traces():
    tool = HttpRequestTool()
    args = HttpRequestArgs(
        target="https://example.com",
        auth_mode="bearer",
        bearer_token="top-secret-token",
    )
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==\n"
        "curl --user admin:super-secret --oauth2-bearer top-secret-token\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t12\t0\t0.01"
    )
    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert metadata["auth_mode_used"] == "bearer"
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in rendered_stdout
    assert "admin:super-secret" not in rendered_stdout
    assert "top-secret-token" not in rendered_stdout
    assert "<REDACTED>" in rendered_stdout


def test_http_request_redacts_client_key_passphrase_surfaces():
    tool = HttpRequestTool()
    args = HttpRequestArgs(target="https://example.com")
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "curl --pass super-secret-passphrase\n"
        "client_key_passphrase: also-secret\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t12\t0\t0.01"
    )
    tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    rendered_stdout, _ = tool.render_result_output(args=args, stdout=stdout, stderr="")

    assert "super-secret-passphrase" not in rendered_stdout
    assert "also-secret" not in rendered_stdout
    assert "<REDACTED>" in rendered_stdout


@pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
def test_http_download_blocks_workspace_traversal(workspace: Path, transport: str | None):
    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="../escape.bin",
            transport=transport,
        )
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.metadata.get("error_type") == "validation_error"


def test_http_download_redacts_url_credentials_in_outputs(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    content = b"safe-content"

    def _fake_run(cmd, capture_output, text, timeout, cwd=None):  # noqa: ANN001
        _ = cwd
        output_idx = cmd.index("--output") + 1
        Path(cmd[output_idx]).write_bytes(content)
        stdout = "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://user:pass@example.com/private.bin\t12\t0\t0.03"
        return subprocess.CompletedProcess(cmd, 0, stdout.encode("utf-8"), b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://user:pass@example.com/private.bin",
            output_path="private/private.bin",
        )
    )

    assert result.success is True
    assert "user:pass" not in result.stdout
    assert "<REDACTED>@example.com" in result.stdout
    assert "<REDACTED>@example.com" in (result.metadata.get("effective_url") or "")
