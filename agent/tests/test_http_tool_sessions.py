"""Session/cookie coverage for HTTP request and download tools."""

from __future__ import annotations

import os
from pathlib import Path

from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs
from agent.tools.tool_registry import run_tool_by_name


def _request_stdout_fixture() -> str:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 2\r\n\r\n"
        "ok\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.01"
    )


def _download_stdout_fixture() -> str:
    return "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://downloads.example.com/file.bin\t10\t0\t0.03"


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


def test_http_request_cookie_file_flag_is_mapped_without_cookie_jar(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        (tmp_path / "cookies").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cookies" / "session.txt").write_text("a=1", encoding="utf-8")
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            cookie_file="cookies/session.txt",
        )

        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout_fixture(), stderr="", exit_code=0, args=args)

        assert "--cookie" in cmd and "cookies/session.txt" in cmd
        assert "--cookie-jar" not in cmd
        assert metadata["session_cookie_source"] == "file"
        assert metadata["cookies_persisted"] is False
    finally:
        _workspace_teardown(previous, cwd)


def test_http_request_cookie_persistence_fields_are_rejected(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        result = run_tool_by_name(
            "information_gathering.web_enumeration.http_request",
            {
                "target": "https://example.com",
                "cookie": "a=1; b=2",
                "persist_cookies": True,
                "cookie_jar": "cookies/persisted.jar",
            },
        )

        assert result.success is False
        assert "persist_cookies" in result.stderr
        assert "cookie_jar" in result.stderr
        assert "extra inputs" in result.stderr.lower()
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_cookie_file_and_jar_flags_are_mapped(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        (tmp_path / "cookies").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cookies" / "session.txt").write_text("a=1", encoding="utf-8")
        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://downloads.example.com/file.bin",
            output_path="downloads/file.bin",
            cookie_file="cookies/session.txt",
            cookie_jar="cookies/persisted.jar",
            persist_cookies=True,
        )

        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout_fixture(), stderr="", exit_code=0, args=args)

        assert "--cookie" in cmd and "cookies/session.txt" in cmd
        assert "--cookie-jar" in cmd and "cookies/persisted.jar" in cmd
        assert metadata["session_cookie_source"] == "file"
        assert metadata["cookies_persisted"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_http_session_paths_outside_workspace_fail_with_validation_error(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        request_result = run_tool_by_name(
            "information_gathering.web_enumeration.http_request",
            {
                "target": "https://example.com",
                "cookie_file": "../outside.cookies",
            },
        )
        download_result = run_tool_by_name(
            "information_gathering.web_enumeration.http_download",
            {
                "target": "https://example.com/file.bin",
                "output_path": "downloads/file.bin",
                "cookie_file": "../outside.cookies",
            },
        )

        assert request_result.success is False
        assert request_result.exit_code == -1
        assert request_result.metadata.get("error_type") == "validation_error"

        assert download_result.success is False
        assert download_result.exit_code == -1
        assert download_result.metadata.get("error_type") == "validation_error"
    finally:
        _workspace_teardown(previous, cwd)


def test_cookie_and_cookie_file_are_mutually_exclusive():
    request_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {
            "target": "https://example.com",
            "cookie": "a=1",
            "cookie_file": "cookies.txt",
        },
    )
    download_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "file.bin",
            "cookie": "a=1",
            "cookie_file": "cookies.txt",
        },
    )

    assert request_result.success is False
    assert "mutually exclusive" in request_result.stderr.lower()
    assert download_result.success is False
    assert "mutually exclusive" in download_result.stderr.lower()
