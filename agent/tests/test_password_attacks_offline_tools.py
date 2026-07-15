"""Unit tests for offline password attack tools (rainbowcrack, samdump2, crunch)."""

from __future__ import annotations

from subprocess import CompletedProcess
from typing import List
from unittest.mock import patch

import os
import tempfile

from agent.tools.password_attacks.offline_attacks.rainbowcrack import (
    HashAlgorithm,
    Operation,
    RainbowCrackArgs,
    RainbowCrackTool,
)
from agent.tools.password_attacks.offline_attacks.samdump2 import (
    HashFormat,
    OutputFormat as SamOutputFormat,
    SAMdump2Args,
    SAMdump2Tool,
)
from agent.tools.password_attacks.offline_attacks.crunch import CrunchArgs, CrunchTool


def _completed(args: List[str], stdout: str = "", stderr: str = "", code: int = 0) -> CompletedProcess:
    return CompletedProcess(args=args, returncode=code, stdout=stdout, stderr=stderr)


class TestRainbowCrackTool:
    def test_build_command_includes_target(self) -> None:
        tool = RainbowCrackTool()
        args = RainbowCrackArgs(
            operation=Operation.CRACK_HASH,
            hash_algorithm=HashAlgorithm.NTLM,
            target="5d41402abc4b2a76b9719d911017c592",
        )

        cmd = tool.build_command(args)

        assert cmd[0] == "rcrack"
        assert args.target in cmd
        assert "--algorithm" in cmd

    @patch("agent.tools.password_attacks.offline_attacks.rainbowcrack.subprocess.run")
    def test_run_success(self, mock_run) -> None:
        tool = RainbowCrackTool()
        args = RainbowCrackArgs(
            operation=Operation.CRACK_HASH,
            hash_algorithm=HashAlgorithm.NTLM,
            target="hashvalue",
        )
        mock_run.return_value = _completed(["rcrack"], stdout="completed", stderr="", code=0)

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["exit_code"] == 0


class TestSAMdump2Tool:
    def test_build_command_basic(self) -> None:
        tool = SAMdump2Tool()
        args = SAMdump2Args(target="SYSTEM", hash_format=HashFormat.NTLM)

        cmd = tool.build_command(args)

        assert cmd[0] == "samdump2"
        assert "SYSTEM" in cmd
        assert "--hash-format" in cmd

    @patch("agent.tools.password_attacks.offline_attacks.samdump2.subprocess.run")
    def test_run_success(self, mock_run) -> None:
        tool = SAMdump2Tool()
        args = SAMdump2Args(target="SYSTEM", hash_format=HashFormat.NTLM)
        mock_run.return_value = _completed(["samdump2"], stdout="completed", stderr="", code=0)

        result = tool.run(args)

        assert result.success is True
        assert result.metadata["exit_code"] == 0


class TestCrunchTool:
    def test_build_command_with_defaults(self) -> None:
        tool = CrunchTool()
        args = CrunchArgs(min_length=3, max_length=4, charset="abc", output_file="out.txt")
        cmd = tool.build_command(args, output_path="out.txt")

        assert cmd[:3] == ["crunch", "3", "4"]
        assert "abc" in cmd
        assert "-o" in cmd and "out.txt" in cmd

    @patch("agent.tools.password_attacks.offline_attacks.crunch.subprocess.run")
    def test_run_success_adds_artifact(self, mock_run) -> None:
        tool = CrunchTool()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "wordlist.txt")
            # Pre-create the file so create_artifacts picks it up
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write("abc\n")

            args = CrunchArgs(min_length=1, max_length=2, charset="ab", output_file=output_path)
            mock_run.return_value = _completed(["crunch"], stdout="Done", stderr="", code=0)

            result = tool.run(args)

            assert result.success is True
            assert output_path in result.artifacts


