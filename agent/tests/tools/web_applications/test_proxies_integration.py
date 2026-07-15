import subprocess
from unittest.mock import patch

import pytest

from agent.tools.web_applications.web_application_proxies.mitmproxy import (
    MitmProxyArgs,
    MitmProxyTool,
    ProxyMode,
)


def test_mitmproxy_supports_pty():
    assert MitmProxyTool().supports_pty() is True


def test_mitmproxy_run_uses_execution_model(monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")

    with patch.object(MitmProxyTool, "build_command", return_value=["mitmproxy"]) as build_cmd, patch.object(
        MitmProxyTool, "parse_output", return_value={"capture_status": "ok"}
    ) as parse_output, patch.object(
        MitmProxyTool, "create_artifacts", return_value=["artifacts/mitmproxy_regular_1.json"]
    ) as create_artifacts, patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mitmproxy"], 0, stdout="{}", stderr=""),
    ):
        result = tool.run(args)

    assert build_cmd.called
    assert parse_output.called
    assert create_artifacts.called
    assert result.metadata["capture_status"] == "ok"


def test_mitmproxy_build_command_for_pty():
    args = MitmProxyArgs(target="http://example.com", proxy_mode=ProxyMode.SOCKS)
    command = MitmProxyTool().build_command(args)
    assert all(isinstance(item, str) for item in command)
    assert "--mode" in command and ProxyMode.SOCKS.value in command


def test_mitmproxy_invalid_proxy_mode():
    with pytest.raises(Exception):
        MitmProxyArgs(target="http://example.com", proxy_mode="invalid")  # type: ignore[arg-type]


def test_mitmproxy_artifact_creation_error(monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")

    def _raise(*_args, **_kwargs):
        raise OSError("no write")

    monkeypatch.setattr("builtins.open", _raise)
    artifacts = tool.create_artifacts("data", args, timestamp=123)
    assert artifacts == []


def test_mitmproxy_run_network_error(monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="network error", stderr="failed")

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == 1
    assert result.metadata.get("exit_code") == 1


def test_mitmproxy_artifact_path_consistent(monkeypatch, tmp_path):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", proxy_mode=ProxyMode.TRANSPARENT)
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("payload", args, timestamp=1700000003)
    assert artifacts
    assert f"mitmproxy_{ProxyMode.TRANSPARENT.value}" in artifacts[0]


