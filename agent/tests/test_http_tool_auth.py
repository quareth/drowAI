"""Auth mode coverage for HTTP request/download tools."""

from __future__ import annotations

import os
from pathlib import Path

from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


def _request_stdout() -> str:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "ok\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.01"
    )


def _download_stdout() -> str:
    return "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://example.com/file.bin\t2\t0\t0.01"


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


def test_http_request_basic_auth_maps_to_curl_user_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            auth_mode="basic",
            username="alice",
            password="p@ss",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=args)

        assert "--user" in cmd
        assert "alice:p@ss" in cmd
        assert metadata["auth_mode_used"] == "basic"
    finally:
        _workspace_teardown(previous, cwd)


def test_http_request_bearer_auth_maps_to_oauth_bearer_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            auth_mode="bearer",
            bearer_token="very-secret-token",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=args)

        assert "--oauth2-bearer" in cmd
        assert "very-secret-token" in cmd
        assert metadata["auth_mode_used"] == "bearer"
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_auth_mode_metadata_and_command_mapping(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="files/file.bin",
            auth_mode="bearer",
            bearer_token="token-123",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=args)

        assert "--oauth2-bearer" in cmd
        assert "token-123" in cmd
        assert metadata["auth_mode_used"] == "bearer"
    finally:
        _workspace_teardown(previous, cwd)


def test_auth_mode_validation_rejects_missing_required_credentials():
    request_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "auth_mode": "basic",
            "username": "alice",
        },
    )
    download_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "f.bin",
            "auth_mode": "bearer",
        },
    )

    assert request_result.success is False
    assert request_result.exit_code == -1
    assert "requires both username and password" in request_result.stderr.lower()
    assert download_result.success is False
    assert download_result.exit_code == -1
    assert "requires bearer_token" in download_result.stderr.lower()


def test_request_auth_mode_rejects_manual_authorization_header_conflict():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "headers": {"Authorization": "Bearer abc"},
            "auth_mode": "bearer",
            "bearer_token": "def",
        },
    )
    assert result.success is False
    assert result.exit_code == -1
    assert "cannot be combined with authorization header" in result.stderr.lower()
