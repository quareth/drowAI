"""Retry and transfer-rate control coverage for HTTP request/download tools."""

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


def test_http_request_retry_rate_maps_to_curl_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            retries=3,
            retry_delay=2,
            retry_max_time=10,
            retry_connrefused=True,
            limit_rate="200K",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=args)

        assert "--retry" in cmd and "3" in cmd
        assert "--retry-delay" in cmd and "2" in cmd
        assert "--retry-max-time" in cmd and "10" in cmd
        assert "--retry-connrefused" in cmd
        assert "--limit-rate" in cmd and "200K" in cmd
        assert metadata["retry_config_applied"] == {
            "retries": 3,
            "retry_delay": 2,
            "retry_max_time": 10,
            "retry_connrefused": True,
            "limit_rate": "200K",
        }
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_retry_rate_maps_to_curl_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="out/file.bin",
            retries=2,
            retry_delay=1,
            limit_rate="1M",
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=args)

        assert "--retry" in cmd and "2" in cmd
        assert "--retry-delay" in cmd and "1" in cmd
        assert "--limit-rate" in cmd and "1M" in cmd
        assert metadata["retry_config_applied"] == {
            "retries": 2,
            "retry_delay": 1,
            "limit_rate": "1M",
        }
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_transfer_controls_map_to_curl_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="out/file.bin",
            connect_timeout=5,
            speed_limit=300,
            speed_time=10,
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=args)

        assert "--connect-timeout" in cmd and "5" in cmd
        assert "--speed-limit" in cmd and "300" in cmd
        assert "--speed-time" in cmd and "10" in cmd
        assert metadata["transfer_controls_applied"] == {
            "connect_timeout": 5,
            "speed_limit": 300,
            "speed_time": 10,
        }
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_speed_controls_require_pair():
    result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "f.bin",
            "speed_limit": 300,
        },
    )

    assert result.success is False
    assert "speed_limit and speed_time" in result.stderr


def test_retry_options_require_retries():
    request_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {"target": "https://example.com", "retry_delay": 1},
    )
    download_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {"target": "https://example.com/file.bin", "output_path": "f.bin", "retry_connrefused": True},
    )

    assert request_result.success is False
    assert "require retries" in request_result.stderr.lower()
    assert download_result.success is False
    assert "require retries" in download_result.stderr.lower()


def test_retry_defaults_preserve_old_behavior(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        request_tool = HttpRequestTool()
        request_args = HttpRequestArgs(target="https://example.com")
        request_cmd = request_tool.build_command(request_args)
        request_meta = request_tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=request_args)

        download_tool = HttpDownloadTool()
        download_args = HttpDownloadArgs(target="https://example.com/file.bin", output_path="out/file.bin")
        download_cmd = download_tool.build_command(download_args)
        download_meta = download_tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=download_args)

        assert "--retry" not in request_cmd
        assert "--limit-rate" not in request_cmd
        assert request_meta["retry_config_applied"] == {}

        assert "--retry" not in download_cmd
        assert "--limit-rate" not in download_cmd
        assert download_meta["retry_config_applied"] == {}
    finally:
        _workspace_teardown(previous, cwd)
