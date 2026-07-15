"""Tests for headless FTP and SSH service access tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent.tools.service_access.common import (
    FtpDownloadArgs,
    FtpListArgs,
    FtpLoginArgs,
    SshLoginArgs,
)
from agent.tools.service_access.ftp_download import FtpDownloadTool
from agent.tools.service_access.ftp_list import FtpListTool
from agent.tools.service_access.ftp_login import FtpLoginTool
from agent.tools.service_access.ssh_login import SshLoginTool
from agent.tools.tool_registry import available_tools, get_tool
from agent.tools.utils import sanitize_command_text


def test_service_access_tools_are_registry_discoverable() -> None:
    tools = set(available_tools())

    assert "service_access.ftp_login" in tools
    assert "service_access.ftp_list" in tools
    assert "service_access.ftp_download" in tools
    assert "service_access.ssh_login" in tools
    assert get_tool("service_access.ftp_login") is FtpLoginTool
    assert get_tool("service_access.ftp_list") is FtpListTool
    assert get_tool("service_access.ftp_download") is FtpDownloadTool
    assert get_tool("service_access.ssh_login") is SshLoginTool


def test_ftp_login_builds_direct_lftp_command_without_payload_files() -> None:
    args = FtpLoginArgs(host="10.0.0.5", username="nathan", password="secret")
    tool = FtpLoginTool()

    command = tool.build_command(args)

    assert command[0] == "lftp"
    assert "python3" not in command
    assert command[command.index("-u") + 1] == "nathan,secret"
    assert tool.prepare_workspace_files(args) == []
    assert tool.prepare_workspace_directories(args) == []


def test_ftp_list_parses_auth_failure_without_leaking_password() -> None:
    args = FtpListArgs(host="10.0.0.5", username="nathan", password="secret", remote_path="/")
    metadata = FtpListTool().parse_output("", "530 Login incorrect.\npassword=secret", 1, args)

    assert metadata["auth_success"] is False
    assert metadata["failure_reason"] == "authentication_failed"
    assert "secret" not in metadata["stderr_preview"]
    assert "<redacted>" in metadata["stderr_preview"]


def test_ftp_download_declares_workspace_output_and_no_artifacts(tmp_path: Path) -> None:
    args = FtpDownloadArgs(
        host="10.0.0.5",
        username="nathan",
        password="secret",
        remote_path="/home/nathan/file.txt",
        output_path="downloads/file.txt",
        create_parents=True,
    )
    tool = FtpDownloadTool()

    command = tool.build_command(args)
    directories = tool.prepare_workspace_directories(args)
    outputs = tool.runtime_output_files(args)

    assert command[0] == "lftp"
    assert "/workspace/downloads/file.txt" in command[command.index("-e") + 1]
    assert directories[0].relative_path == "downloads"
    assert outputs[0].relative_path == "downloads/file.txt"
    assert tool.create_artifacts("", args) == []

    destination = tmp_path / "downloads" / "file.txt"
    destination.parent.mkdir()
    destination.write_text("downloaded", encoding="utf-8")
    post = tool.postprocess_execution(
        args=args,
        stdout="",
        stderr="",
        exit_code=0,
        success=True,
        metadata=tool.parse_output("", "", 0, args),
        artifacts=["should-not-survive"],
        runtime_context=SimpleNamespace(host_workspace_path=str(tmp_path)),
    )

    assert post.success is True
    assert post.artifacts == []
    assert post.metadata["saved_path"] == "downloads/file.txt"
    assert post.metadata["bytes_written"] == len("downloaded")


def test_ssh_login_builds_direct_sshpass_command_and_sanitizes_password() -> None:
    args = SshLoginArgs(host="10.0.0.5", username="nathan", password="secret")
    command = SshLoginTool().build_command(args)

    assert command[:3] == ["sshpass", "-p", "secret"]
    assert command[-1] == "true"
    assert "nathan@10.0.0.5" in command

    sanitized = sanitize_command_text("sshpass -p secret ssh -p 22 nathan@10.0.0.5 true")
    assert "secret" not in sanitized
    assert "<REDACTED>" in sanitized
