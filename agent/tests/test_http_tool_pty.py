"""PTY execution coverage for HTTP request/download tool transport path."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.executor import EnhancedCommandExecutor


class MockShellCommandResult:
    """Small PTY result stub matching executor expectations."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0, status: str = "success"):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.status = status


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


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


def _mock_config(task_id: int, workspace_path: str) -> MagicMock:
    cfg = MagicMock()
    cfg.task_id = task_id
    cfg.workspace_path = workspace_path
    cfg.openai_api_key = "test-key"
    cfg.model_name = "gpt-4"
    cfg.individual_tool_timeout = 60
    cfg.tool_execution_timeout = 60
    return cfg


def test_http_request_routes_via_pty_and_parses_metadata(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 6\r\n\r\n"
        "hello!\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttest/plain\t6\t0\t0.02"
    )
    mock_result = MockShellCommandResult(stdout=stdout, stderr="", exit_code=0)
    mocked_pty = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    executor = EnhancedCommandExecutor(config=_mock_config(task_id=11, workspace_path=str(workspace)))
    result = asyncio.run(
        executor._execute_via_pty(
            "information_gathering.web_enumeration.http_request",
            {
                "target": "https://example.com",
                "cookie": "sid=abc",
                "auth_mode": "bearer",
                "bearer_token": "secret-token",
                "resolve": ["example.com:443:1.2.3.4"],
                "connect_to": ["example.com:443:10.0.0.2:8443"],
                "interface": "eth0",
                "local_port": 5000,
                "ipv4_only": True,
                "retries": 2,
                "retry_delay": 1,
                "limit_rate": "200K",
                "transport": "pty",
            },
        )
    )

    assert result.success is True
    assert result.metadata["status_code"] == 200
    assert result.metadata["curl_exit_code"] == 0
    assert result.metadata["cookies_persisted"] is False
    assert result.metadata["auth_mode_used"] == "bearer"
    assert result.metadata["connection_controls_applied"]["ipv4_only"] is True
    assert result.metadata["retry_config_applied"]["retries"] == 2
    assert result.metadata["trace_mode"] == "none"
    called_command = mocked_pty.call_args.kwargs["command"]
    assert mocked_pty.call_args.kwargs["workspace_path"] == "/workspace"
    assert "curl" in called_command
    assert "https://example.com" in called_command
    assert "--cookie sid=abc" in called_command
    assert "--cookie-jar" not in called_command
    assert "--oauth2-bearer secret-token" in called_command
    assert "--resolve example.com:443:1.2.3.4" in called_command
    assert "--connect-to example.com:443:10.0.0.2:8443" in called_command
    assert "--interface eth0" in called_command
    assert "--local-port 5000" in called_command
    assert " -4" in called_command
    assert "--retry 2" in called_command
    assert "--retry-delay 1" in called_command
    assert "--limit-rate 200K" in called_command
    assert "--dump-header" not in called_command
    assert "--trace" not in called_command
    assert "--output" not in called_command


def test_http_request_pty_uses_stdout_response_when_process_cwd_differs(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_execute_via_pty(**kwargs):
        command = kwargs["command"]
        assert "--dump-header" not in command
        assert "--output" not in command
        stdout = (
            "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nX-Test: yes\r\n\r\n"
            "\x00ABC\n"
            "__DROWAI_HTTP_META__200\thttps://example.com/bin\tapplication/octet-stream\t4\t0\t0.02"
        )
        return MockShellCommandResult(stdout=stdout, stderr="", exit_code=0)

    mocked_pty = AsyncMock(side_effect=_fake_execute_via_pty)
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    original_cwd = Path.cwd()
    os.chdir("/")
    try:
        executor = EnhancedCommandExecutor(config=_mock_config(task_id=15, workspace_path=str(workspace)))
        result = asyncio.run(
            executor._execute_via_pty(
                "information_gathering.web_enumeration.http_request",
                    {
                        "target": "https://example.com/bin",
                        "transport": "pty",
                    },
                )
        )
    finally:
        os.chdir(original_cwd)

    assert result.success is True
    assert mocked_pty.call_args.kwargs["workspace_path"] == "/workspace"
    assert result.metadata["response_mode"] == "text"
    assert result.metadata["binary_response_detected"] is True
    assert result.metadata["response_headers"]["Content-Type"] == "application/octet-stream"
    assert result.metadata["response_headers"]["X-Test"] == "yes"
    assert "HTTP/1.1 200 OK" in result.stdout


def test_http_download_routes_via_pty_and_returns_download_metadata(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    content = b"download-content"
    expected_sha = hashlib.sha256(content).hexdigest()
    stdout = (
        '__DROWAI_HTTP_DOWNLOAD_META__{"http_code":200,'
        '"url_effective":"https://downloads.example.com/tool.bin",'
        '"size_download":512,'
        '"num_redirects":0,'
        '"time_total":0.07}'
    )
    mock_result = MockShellCommandResult(stdout=stdout, stderr="", exit_code=0)
    output_path = workspace / "downloads" / "tool.bin"
    cert_dir = workspace / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    (cert_dir / "client.crt").write_text("crt", encoding="utf-8")
    (cert_dir / "client.key").write_text("key", encoding="utf-8")
    (cert_dir / "ca.pem").write_text("ca", encoding="utf-8")

    async def _fake_execute_via_pty(**kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)
        return mock_result

    mocked_pty = AsyncMock(side_effect=_fake_execute_via_pty)
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    executor = EnhancedCommandExecutor(config=_mock_config(task_id=12, workspace_path=str(workspace)))
    result = asyncio.run(
        executor._execute_via_pty(
            "information_gathering.web_enumeration.http_download",
            {
                "target": "https://downloads.example.com/tool.bin",
                "output_path": "downloads/tool.bin",
                "expected_sha256": expected_sha,
                "cookie": "sid=abc",
                "persist_cookies": True,
                "client_cert_path": "certs/client.crt",
                "client_key_path": "certs/client.key",
                "client_key_passphrase": "mtls-pass",
                "ca_cert_path": "certs/ca.pem",
                "dump_headers_artifact": True,
                "trace_mode": "trace",
                "transport": "pty",
            },
        )
    )

    assert result.success is True
    assert result.metadata["status_code"] == 200
    assert result.metadata["effective_url"] == "https://downloads.example.com/tool.bin"
    assert result.metadata["checksum_verified"] is True
    assert result.metadata["mtls_used"] is True
    assert result.metadata["ca_cert_used"] is True
    assert result.metadata["trace_mode"] == "none"
    assert getattr(result, "artifacts", []) == []
    called_command = mocked_pty.call_args.kwargs["command"]
    assert "curl" in called_command
    assert "--output downloads/tool.bin" in called_command
    assert "--cookie sid=abc" in called_command
    assert "--cookie-jar artifacts/http_download_cookies.jar" in called_command
    assert "--cert certs/client.crt" in called_command
    assert "--key certs/client.key" in called_command
    assert "--pass mtls-pass" in called_command
    assert "--cacert certs/ca.pem" in called_command
    assert "--dump-header" not in called_command
    assert "--trace" not in called_command
    assert "__DROWAI_HTTP_DOWNLOAD_META__%{json}" in called_command
    assert "\t" not in called_command
    assert "\\t" not in called_command
    assert str(workspace) not in called_command


def test_http_download_pty_enforces_checksum_mismatch_failure(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output_path = workspace / "downloads" / "tool.bin"
    stdout = "__DROWAI_HTTP_DOWNLOAD_META__200\thttps://downloads.example.com/tool.bin\t512\t0\t0.07"
    mock_result = MockShellCommandResult(stdout=stdout, stderr="", exit_code=0)

    async def _fake_execute_via_pty(**kwargs):
        _ = kwargs
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"wrong-content")
        return mock_result

    mocked_pty = AsyncMock(side_effect=_fake_execute_via_pty)
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    executor = EnhancedCommandExecutor(config=_mock_config(task_id=13, workspace_path=str(workspace)))
    result = asyncio.run(
        executor._execute_via_pty(
            "information_gathering.web_enumeration.http_download",
            {
                "target": "https://downloads.example.com/tool.bin",
                "output_path": "downloads/tool.bin",
                "expected_sha256": "0" * 64,
                "transport": "pty",
            },
        )
    )

    assert result.success is False
    assert result.exit_code == 3
    assert "checksum mismatch" in result.stderr.lower()


def test_http_request_pty_redacts_sensitive_command_history(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 2\r\n\r\n"
        "ok\n"
        "__DROWAI_HTTP_META__200\thttps://example.com\ttext/plain\t2\t0\t0.02"
    )
    mock_result = MockShellCommandResult(stdout=stdout, stderr="", exit_code=0)
    mocked_pty = AsyncMock(return_value=mock_result)
    monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", mocked_pty)

    with patch("backend.services.terminal_session_manager.terminal_session_manager") as mock_terminal_manager:
        executor = EnhancedCommandExecutor(config=_mock_config(task_id=14, workspace_path=str(workspace)))
        result = asyncio.run(
            executor._execute_via_pty(
                "information_gathering.web_enumeration.http_request",
                {
                    "target": "https://example.com",
                    "auth_mode": "bearer",
                    "bearer_token": "super-secret-token",
                    "transport": "pty",
                },
            )
        )

    assert result.success is True
    executed_command = mocked_pty.call_args.kwargs["command"]
    assert "--oauth2-bearer super-secret-token" in executed_command

    persisted_command = mock_terminal_manager.record_agent_command.call_args[0][1]
    assert "super-secret-token" not in persisted_command
    assert "<REDACTED>" in persisted_command

    result_command_text = getattr(result, "command_text", "")
    assert "super-secret-token" not in result_command_text
    assert "<REDACTED>" in result_command_text
