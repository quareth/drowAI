"""Connection and DNS control coverage for HTTP request/download tools."""

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


def test_http_request_connection_controls_map_to_curl_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpRequestTool()
        args = HttpRequestArgs(
            target="https://example.com",
            resolve=["example.com:443:1.2.3.4"],
            connect_to=["example.com:443:10.0.0.2:8443"],
            interface="eth0",
            local_port=5555,
            ipv4_only=True,
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_request_stdout(), stderr="", exit_code=0, args=args)

        assert "--resolve" in cmd and "example.com:443:1.2.3.4" in cmd
        assert "--connect-to" in cmd and "example.com:443:10.0.0.2:8443" in cmd
        assert "--interface" in cmd and "eth0" in cmd
        assert "--local-port" in cmd and "5555" in cmd
        assert "-4" in cmd
        controls = metadata["connection_controls_applied"]
        assert controls["resolve"] == ["example.com:443:1.2.3.4"]
        assert controls["connect_to"] == ["example.com:443:10.0.0.2:8443"]
        assert controls["interface"] == "eth0"
        assert controls["local_port"] == 5555
        assert controls["ipv4_only"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_http_download_connection_controls_map_to_curl_and_metadata(tmp_path: Path):
    previous, cwd = _workspace_setup(tmp_path)
    try:
        tool = HttpDownloadTool()
        args = HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path="out/file.bin",
            resolve=["example.com:443:1.2.3.4"],
            interface="eth0",
            ipv6_only=True,
        )
        cmd = tool.build_command(args)
        metadata = tool.parse_output(stdout=_download_stdout(), stderr="", exit_code=0, args=args)

        assert "--resolve" in cmd and "example.com:443:1.2.3.4" in cmd
        assert "--interface" in cmd and "eth0" in cmd
        assert "-6" in cmd
        controls = metadata["connection_controls_applied"]
        assert controls["resolve"] == ["example.com:443:1.2.3.4"]
        assert controls["interface"] == "eth0"
        assert controls["ipv6_only"] is True
    finally:
        _workspace_teardown(previous, cwd)


def test_connection_controls_reject_ip_family_conflict():
    request_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {"target": "https://example.com", "ipv4_only": True, "ipv6_only": True},
    )
    download_result = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "f.bin",
            "ipv4_only": True,
            "ipv6_only": True,
        },
    )
    assert request_result.success is False
    assert "mutually exclusive" in request_result.stderr.lower()
    assert download_result.success is False
    assert "mutually exclusive" in download_result.stderr.lower()


def test_connection_controls_reject_bad_resolve_and_connect_to_formats():
    req_bad_resolve = run_tool_by_name(
        "information_gathering.web_enumeration.http_request",
        {"target": "https://example.com", "resolve": ["example.com:443"]},
    )
    dl_bad_connect = run_tool_by_name(
        "information_gathering.web_enumeration.http_download",
        {
            "target": "https://example.com/file.bin",
            "output_path": "f.bin",
            "connect_to": ["example.com:443:backend.local"],
        },
    )
    assert req_bad_resolve.success is False
    assert "resolve entries must be host:port:address" in req_bad_resolve.stderr.lower()
    assert dl_bad_connect.success is False
    assert "connect_to entries must be host1:port1:host2:port2" in dl_bad_connect.stderr.lower()
