"""Unit tests for online password attack tools (ncrack, crowbar, patator, hydra)."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import List
from unittest.mock import patch

from agent.tools.password_attacks.online_attacks.ncrack import (
    NcrackArgs,
    NcrackTool,
    Protocol as NcrackProtocol,
)
from agent.tools.password_attacks.online_attacks.crowbar import (
    CrowbarArgs,
    CrowbarModule,
    CrowbarTool,
)
from agent.tools.password_attacks.online_attacks.patator import (
    PatatorArgs,
    PatatorModule,
    PatatorTool,
)
from agent.tools.password_attacks.online_attacks.hydra import (
    HydraArgs,
    HydraTool,
    Protocol as HydraProtocol,
)


def _completed(args: List[str], stdout: str = "", stderr: str = "", code: int = 0) -> CompletedProcess:
    return CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)


def _fixture_text(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "tests" / "tools" / "fixtures" / "outputs" / name).read_text(encoding="utf-8")


class TestNcrackTool:
    def test_build_command_minimal(self) -> None:
        tool = NcrackTool()
        args = NcrackArgs(target="10.0.0.1", protocol=NcrackProtocol.SSH, username="root", password="toor")

        cmd = tool.build_command(args)

        assert cmd[0] == "ncrack"
        assert "10.0.0.1" in cmd
        assert NcrackProtocol.SSH.value in cmd

    @patch("agent.tools.password_attacks.online_attacks.ncrack.subprocess.run")
    def test_run_success_masks_password(self, mock_run) -> None:
        tool = NcrackTool()
        args = NcrackArgs(target="10.0.0.1", protocol=NcrackProtocol.SSH, username="root", password="toor")
        mock_run.return_value = _completed(
            ["ncrack"],
            stdout="[SUCCESS] login: root password: toor",
            stderr="",
            code=0,
        )

        result = tool.run(args)

        assert result.success is True
        assert result.exit_code == 0
        assert result.metadata["credentials"][0]["password"] == "***"


class TestCrowbarTool:
    def test_build_command_basic(self) -> None:
        tool = CrowbarTool()
        args = CrowbarArgs(
            target="10.0.0.2",
            module=CrowbarModule.SSH,
            host="10.0.0.2",
            user="root",
            password="toor",
        )

        cmd = tool.build_command(args)

        assert cmd[0] == "crowbar"
        assert "-b" in cmd and CrowbarModule.SSH.value in cmd
        assert "-s" in cmd and "10.0.0.2" in cmd

    @patch("agent.tools.password_attacks.online_attacks.crowbar.subprocess.run")
    def test_run_success_metadata(self, mock_run) -> None:
        tool = CrowbarTool()
        args = CrowbarArgs(
            target="10.0.0.3",
            module=CrowbarModule.RDP,
            host="10.0.0.3",
            user="admin",
            password="secret",
        )
        mock_run.return_value = _completed(
            ["crowbar"],
            stdout="SUCCESS admin:secret",
            stderr="",
            code=0,
        )

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["module"] == CrowbarModule.RDP.value
        assert result.metadata["found_credentials"][0]["password"] == "***"


class TestPatatorTool:
    def test_build_command_with_wordlists(self) -> None:
        tool = PatatorTool()
        args = PatatorArgs(
            target="10.0.0.4",
            module=PatatorModule.SSH_LOGIN,
            host="10.0.0.4",
            user_file="users.txt",
            password_file="pw.txt",
        )

        cmd = tool.build_command(args)

        assert cmd[0] == "patator"
        assert "user=FILE0" in cmd
        assert "password=FILE1" in cmd
        assert "users.txt" in cmd
        assert "pw.txt" in cmd

    @patch("agent.tools.password_attacks.online_attacks.patator.subprocess.run")
    def test_run_success_masks_password(self, mock_run) -> None:
        tool = PatatorTool()
        args = PatatorArgs(
            target="10.0.0.4",
            module=PatatorModule.SSH_LOGIN,
            host="10.0.0.4",
            user="root",
            password="toor",
        )
        mock_run.return_value = _completed(
            ["patator"],
            stdout="SUCCESS root:toor",
            stderr="",
            code=0,
        )

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["found_credentials"][0]["password"] == "***"


class TestHydraTool:
    def test_parse_output_extracts_standard_success_rows_and_status(self) -> None:
        tool = HydraTool()
        args = HydraArgs(target="192.168.1.100", protocol=HydraProtocol.SSH, port=22)

        metadata = tool.parse_output(
            stdout=_fixture_text("password_attacks_online_attacks_hydra.txt"),
            stderr="",
            exit_code=0,
            args=args,
        )

        assert metadata["semantic_schema_version"] == "hydra.v1"
        assert metadata["capability_family"] == "credential_attack"
        assert metadata["general_info"]["version"] == "9.5"
        assert metadata["target_info"]["host"] == "192.168.1.100"
        assert metadata["target_info"]["port"] == 22
        assert metadata["attack_info"]["protocol"] == "ssh"
        assert metadata["statistics"]["successful_login_count"] == 4
        assert metadata["statistics"]["valid_passwords"] == 4
        assert metadata["statistics"]["last_status"]["tries_completed"] == 750
        assert [row["username"] for row in metadata["credentials"]] == [
            "admin",
            "root",
            "user",
            "test",
        ]
        assert all(row["password"] == "***" for row in metadata["credentials"])

    def test_emit_semantic_observations_masks_passwords_and_links_service_socket(self) -> None:
        tool = HydraTool()
        args = HydraArgs(target="192.168.1.100", protocol=HydraProtocol.SSH, port=22)
        stdout = _fixture_text("password_attacks_online_attacks_hydra.txt")
        metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)

        observations = tool.emit_semantic_observations(
            stdout=stdout,
            stderr="",
            exit_code=0,
            args=args,
            metadata=metadata,
        )

        assert len(observations) == 1
        observation = observations[0]
        assert observation["observation_type"] == "finding.vulnerability_confirmed"
        assert observation["subject_type"] == "finding.vulnerability"
        assert observation["subject_key"] == (
            "finding.vulnerability:service.socket:192.168.1.100/tcp/22:hydra/weak-auth"
        )
        payload = observation["payload"]
        assert payload["subject_key"] == "service.socket:192.168.1.100/tcp/22"
        assert payload["detector_id"] == "hydra/weak-auth"
        assert payload["severity"] == "high"
        assert payload["confidence"] == "confirmed"
        assert payload["successful_login_count"] == 4
        assert payload["account_identifiers"] == ["admin", "root", "user", "test"]
        assert payload["durable_masking_applied"] is True
        rendered = str(observations)
        assert "admin123" not in rendered
        assert "toor" not in rendered
        assert "password123" not in rendered
