import subprocess
from unittest.mock import patch

import pytest

from agent.tools.web_applications.web_application_proxies.mitmproxy import (
    CaptureMode,
    MitmProxyArgs,
    MitmProxyTool,
    ProxyMode,
)


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


def test_mitmproxy_build_command_minimal():
    args = MitmProxyArgs(target="http://example.com")
    command = MitmProxyTool().build_command(args)
    assert command[0] == "mitmproxy"
    assert "--listen-port" in command and str(args.port) in command
    assert "--listen-host" in command and args.host in command
    assert "--set" in command and f"output_format={args.output_format}" in command


def test_mitmproxy_build_command_with_ssl():
    args = MitmProxyArgs(
        target="http://example.com",
        ssl_insecure=True,
        ssl_version="TLS1.2",
        certs="/tmp/cert.pem",
        client_certs="/tmp/client.pem",
    )
    command = MitmProxyTool().build_command(args)
    assert "--ssl-insecure" in command
    assert "--set" in command and "ssl_version=TLS1.2" in command
    assert "--certs" in command and "/tmp/cert.pem" in command
    assert "client_certs=/tmp/client.pem" in command


def test_mitmproxy_build_command_with_filtering():
    args = MitmProxyArgs(
        target="http://example.com",
        filter_expression="~u example",
        ignore_hosts="example.com",
        allow_hosts="api.example.com",
        capture_mode=CaptureMode.FILTERED,
    )
    command = MitmProxyTool().build_command(args)
    assert "flow_filter=~u example" in command
    assert "--ignore-hosts" in command and "example.com" in command
    assert "--allow-hosts" in command and "api.example.com" in command
    assert "capture_mode=filtered" in command


def test_mitmproxy_build_command_with_script():
    args = MitmProxyArgs(
        target="http://example.com",
        script_file="addon.py",
        addon_paths="/opt/mitm/addons",
        headers="X-Test: 1",
    )
    command = MitmProxyTool().build_command(args)
    assert "--scripts" in command and "addon.py" in command
    assert "addon_paths=/opt/mitm/addons" in command
    assert "inject_headers=X-Test: 1" in command


def test_mitmproxy_build_command_transparent_mode():
    args = MitmProxyArgs(target="http://example.com", proxy_mode=ProxyMode.TRANSPARENT)
    command = MitmProxyTool().build_command(args)
    assert "--mode" in command and ProxyMode.TRANSPARENT.value in command
    assert args.target not in command  # target should not be positional


def test_mitmproxy_build_command_reverse_mode():
    args = MitmProxyArgs(
        target="http://example.com",
        proxy_mode=ProxyMode.REVERSE,
        upstream_proxy="http://upstream:8080",
    )
    command = MitmProxyTool().build_command(args)
    assert "--mode" in command and ProxyMode.REVERSE.value in command
    assert "--upstream" in command and "http://upstream:8080" in command
    assert args.target not in command


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


def test_mitmproxy_parse_output_json():
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", output_format="json")
    stdout = (
        '{"requests_captured": 5, "responses_captured": 4, "ssl_connections": 2, '
        '"status": "running", "duration": 12, "total_traffic": 2048, "hosts": ["a", "b"]}'
    )
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["requests_captured"] == 5
    assert metadata["responses_captured"] == 4
    assert metadata["ssl_connections"] == 2
    assert metadata["capture_status"] == "running"
    assert metadata["unique_hosts"] == 2


def test_mitmproxy_parse_output_text():
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", output_format="json")
    stdout = "Requests captured: 3\nResponses captured: 2\nSSL connections: 1\nStatus: active\nHost: api.example.com"
    metadata = tool.parse_output(stdout, "", 0, args)
    assert metadata["requests_captured"] == 3
    assert metadata["responses_captured"] == 2
    assert metadata["ssl_connections"] == 1
    assert metadata["capture_status"] in {"active", "unknown"}
    assert metadata["unique_hosts"] >= 1


def test_mitmproxy_parse_output_empty():
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")
    metadata = tool.parse_output("", "", 0, args)
    assert metadata["capture_status"] == "no_output"
    assert metadata["requests_captured"] == 0


def test_mitmproxy_parse_output_malformed():
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")
    metadata = tool.parse_output("{invalid", "", 1, args)
    assert metadata["raw_output_length"] > 0
    assert metadata["lines_processed"] >= 1
    assert "parse_error" in metadata or metadata["capture_status"] != "unknown"


# ---------------------------------------------------------------------------
# create_artifacts
# ---------------------------------------------------------------------------


def test_mitmproxy_create_artifacts(tmp_path, monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", proxy_mode=ProxyMode.SOCKS)
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("capture data", args, timestamp=1700000000)
    assert artifacts
    assert (tmp_path / artifacts[0]).exists()


def test_mitmproxy_create_artifacts_json(tmp_path, monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", output_format="json")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts('{"requests_captured": 1}', args, timestamp=1700000001)
    assert artifacts
    assert artifacts[0].endswith(".json")
    assert (tmp_path / artifacts[0]).exists()


def test_mitmproxy_create_artifacts_har(tmp_path, monkeypatch):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", output_format="har", output_file="captures/flow.har")
    monkeypatch.chdir(tmp_path)
    artifacts = tool.create_artifacts("har content", args, timestamp=1700000002)
    assert artifacts
    assert artifacts[0].endswith("flow.har")
    assert (tmp_path / artifacts[0]).exists()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_mitmproxy_run_success(monkeypatch, tmp_path):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", script_file="addon.py")
    stdout = '{"requests_captured": 1}'

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.chdir(tmp_path)
    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["requests_captured"] == 1
    assert result.artifacts


def test_mitmproxy_run_timeout():
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")

    def _mock_run(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2


def test_mitmproxy_run_with_script(monkeypatch, tmp_path):
    tool = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com", script_file="hook.py")
    stdout = '{"requests_captured": 2}'

    def _mock_run(cmd, capture_output, text, timeout):
        # ensure script flag is passed through build_command
        assert "--scripts" in cmd and "hook.py" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.chdir(tmp_path)
    with patch("subprocess.run", _mock_run):
        result = tool.run(args)

    assert result.success is True
    assert result.metadata["requests_captured"] == 2


