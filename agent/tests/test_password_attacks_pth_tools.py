"""Unit tests for pass-the-hash tools (NTLMRelayX and PTH Toolkit)."""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import List
from unittest.mock import patch

from agent.tools.password_attacks.passing_the_hash.ntlmrelayx import (
    NTLMRelayXArgs,
    NTLMRelayXTool,
    RelayAction,
    RelayMode,
)
from agent.tools.password_attacks.passing_the_hash.passing_the_hash_toolkit import (
    AttackMode,
    HashType,
    PassingTheHashToolkitArgs,
    PassingTheHashToolkitTool,
    ProtocolType,
)


def _completed(args: List[str], stdout: str = "", stderr: str = "", code: int = 0) -> CompletedProcess:
    return CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)


class TestNTLMRelayXTool:
    def test_build_command_basic(self) -> None:
        tool = NTLMRelayXTool()
        args = NTLMRelayXArgs(mode=RelayMode.SMB, action=RelayAction.DUMP, target="10.0.0.5")

        cmd = tool.build_command(args)

        assert cmd[0] == "ntlmrelayx.py"
        assert "-m" in cmd and RelayMode.SMB.value in cmd
        assert "-a" in cmd and RelayAction.DUMP.value in cmd

    @patch("agent.tools.password_attacks.passing_the_hash.ntlmrelayx.subprocess.run")
    def test_run_masks_hashes(self, mock_run) -> None:
        tool = NTLMRelayXTool()
        args = NTLMRelayXArgs(mode=RelayMode.SMB, action=RelayAction.DUMP, target="10.0.0.5")
        mock_run.return_value = _completed(
            ["ntlmrelayx.py"],
            stdout="relay attempt\nntlm hash: ABCDEF",
            stderr="",
            code=0,
        )

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["exit_code"] == 0
        if result.metadata.get("captured_credentials"):
            assert result.metadata["captured_credentials"][0]["hash"] == "***"


class TestPTHToolkit:
    def test_build_command(self) -> None:
        tool = PassingTheHashToolkitTool()
        args = PassingTheHashToolkitArgs(
            target="10.0.0.6",
            username="admin",
            hash_value="aad3b435b51404eeaad3b435b51404ee:32ed87bdb5fdc5e9cba88547376818d4",
            attack_mode=AttackMode.SMB,
            hash_type=HashType.NTLM,
            protocol=ProtocolType.SMB,
        )

        cmd = tool.build_command(args)

        assert cmd[0] == "pth-toolkit"
        assert "-t" in cmd and "10.0.0.6" in cmd
        assert "-H" in cmd

    @patch("agent.tools.password_attacks.passing_the_hash.passing_the_hash_toolkit.subprocess.run")
    def test_run_success(self, mock_run) -> None:
        tool = PassingTheHashToolkitTool()
        args = PassingTheHashToolkitArgs(
            target="10.0.0.6",
            username="admin",
            hash_value="aad3b435b51404eeaad3b435b51404ee:32ed87bdb5fdc5e9cba88547376818d4",
            attack_mode=AttackMode.SMB,
            hash_type=HashType.NTLM,
            protocol=ProtocolType.SMB,
        )
        mock_run.return_value = _completed(
            ["pth-toolkit"],
            stdout="success authenticated",
            stderr="",
            code=0,
        )

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["hash_masked"] is True


