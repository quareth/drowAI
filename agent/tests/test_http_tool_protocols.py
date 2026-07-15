"""HTTP protocol version and curl capability detection tests."""

from __future__ import annotations

import subprocess
import os
from pathlib import Path

import pytest

from agent.tools.information_gathering.web_enumeration import _http_capabilities as capabilities
from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs


@pytest.fixture
def workspace(tmp_path: Path):
    previous = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(cwd)
        if previous is None:
            os.environ.pop("WORKSPACE", None)
        else:
            os.environ["WORKSPACE"] = previous


def test_detect_curl_capabilities_cache(monkeypatch):
    capabilities.reset_curl_http_capabilities_cache()
    calls = {"count": 0}

    def _fake_run(cmd, capture_output, text, timeout):  # noqa: ANN001
        calls["count"] += 1
        _ = cmd, capture_output, text, timeout
        return subprocess.CompletedProcess(
            ["curl", "--version"],
            0,
            "curl 8.5.0\nProtocols: http https h2\nFeatures: alt-svc HTTP2 SSL\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    first = capabilities.detect_curl_http_capabilities()
    second = capabilities.detect_curl_http_capabilities()
    assert first["http2"] is True
    assert first["http3"] is False
    assert second == first
    assert calls["count"] == 1


def test_http_request_http2_flag_applies_when_capability_present(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.information_gathering.web_enumeration.http_request.detect_curl_http_capabilities",
        lambda: {"http2": True, "http3": False, "source": "mock"},
    )
    tool = HttpRequestTool()
    cmd = tool.build_command(HttpRequestArgs(target="https://example.com", http_version="2"))
    assert "--http2" in cmd


def test_http_request_http3_unsupported_returns_structured_error(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.information_gathering.web_enumeration.http_request.detect_curl_http_capabilities",
        lambda: {"http2": True, "http3": False, "source": "mock"},
    )
    tool = HttpRequestTool()
    result = tool.run(HttpRequestArgs(target="https://example.com", http_version="3"))
    assert result.success is False
    assert result.exit_code == -3
    assert result.metadata.get("error_type") == "unsupported_http_version"
    assert result.metadata.get("http_version_requested") == "3"
    assert result.metadata.get("curl_capabilities", {}).get("http3") is False


def test_http_download_http2_unsupported_returns_structured_error(workspace: Path, monkeypatch):
    monkeypatch.setattr(
        "agent.tools.information_gathering.web_enumeration.http_download.detect_curl_http_capabilities",
        lambda: {"http2": False, "http3": False, "source": "mock"},
    )
    tool = HttpDownloadTool()
    result = tool.run(
        HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path=str((workspace / "file.bin").name),
            http_version="2",
        )
    )
    assert result.success is False
    assert result.exit_code == -3
    assert result.metadata.get("error_type") == "unsupported_http_version"
    assert result.metadata.get("http_version_requested") == "2"
    assert result.metadata.get("curl_capabilities", {}).get("http2") is False


def test_http_request_pty_transport_skips_host_capability_gate(monkeypatch):
    def _raise_if_called():  # noqa: ANN202
        raise AssertionError("host capability detection should be skipped for PTY transport")

    monkeypatch.setattr(
        "agent.tools.information_gathering.web_enumeration.http_request.detect_curl_http_capabilities",
        _raise_if_called,
    )
    tool = HttpRequestTool()
    cmd = tool.build_command(
        HttpRequestArgs(
            target="https://example.com",
            http_version="3",
            transport="pty",
        )
    )
    assert "--http3" in cmd
    assert tool._curl_capabilities.get("source") == "deferred_to_runtime_transport"


def test_http_download_pty_transport_skips_host_capability_gate(workspace: Path, monkeypatch):
    def _raise_if_called():  # noqa: ANN202
        raise AssertionError("host capability detection should be skipped for PTY transport")

    monkeypatch.setattr(
        "agent.tools.information_gathering.web_enumeration.http_download.detect_curl_http_capabilities",
        _raise_if_called,
    )
    tool = HttpDownloadTool()
    cmd = tool.build_command(
        HttpDownloadArgs(
            target="https://example.com/file.bin",
            output_path=str((workspace / "file.bin").name),
            http_version="2",
            transport="pty",
        )
    )
    assert "--http2" in cmd
    assert tool._curl_capabilities.get("source") == "deferred_to_runtime_transport"
