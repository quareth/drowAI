"""Tests for shell tool PTY support (build_command, parse_output, create_artifacts)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.executor import EnhancedCommandExecutor
from agent.tools.shell.contracts import ShellExecArgs, ShellScriptArgs
from agent.tools.shell.exec import ShellExecTool
from agent.tools.shell.script import ShellScriptTool


class MockShellCommandResult:
    """Lightweight PTY result stub."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0, status: str = "success"):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.status = status


def _mock_config(task_id: int, workspace_path: str) -> MagicMock:
    cfg = MagicMock()
    cfg.task_id = task_id
    cfg.workspace_path = workspace_path
    cfg.openai_api_key = "test-key"
    cfg.model_name = "gpt-4"
    cfg.individual_tool_timeout = 60
    cfg.tool_execution_timeout = 60
    return cfg


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


@pytest.fixture()
def workspace(tmp_path: Path):
    """Set up a temporary workspace for tests."""
    original = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(cwd)
    if original is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = original


class TestShellExecBuildCommand:
    def test_basic_command(self):
        tool = ShellExecTool()
        args = ShellExecArgs(command="echo test")
        cmd = tool.build_command(args)
        assert cmd[-1].endswith("echo test")
        assert cmd[0] in ("bash", "sh", "powershell")

    def test_command_with_env(self):
        tool = ShellExecTool()
        args = ShellExecArgs(command="echo $FOO", env={"FOO": "BAR"})
        cmd = " ".join(tool.build_command(args))
        assert "FOO" in cmd and "BAR" in cmd

    def test_command_with_cwd(self, workspace: Path):
        tool = ShellExecTool()
        args = ShellExecArgs(command="pwd", cwd="subdir")
        cmd = " ".join(tool.build_command(args))
        assert "cd" in cmd
        assert "subdir" in cmd

    def test_bash_shell_detection(self):
        tool = ShellExecTool()
        args = ShellExecArgs(command="whoami")
        cmd = tool.build_command(args)
        assert cmd[0] in ("bash", "sh", "powershell")

    def test_supports_pty(self):
        assert ShellExecTool().supports_pty() is True


class TestShellExecParseOutput:
    def test_successful_execution(self):
        tool = ShellExecTool()
        meta = tool.parse_output(stdout="ok", stderr="", exit_code=0, args=ShellExecArgs(command="echo ok"))
        payload = meta["shell_exec"]
        assert payload["success"] is True
        assert payload["exit_code"] == 0

    def test_failed_execution(self):
        tool = ShellExecTool()
        meta = tool.parse_output(stdout="", stderr="error", exit_code=1, args=ShellExecArgs(command="bad"))
        payload = meta["shell_exec"]
        assert payload["success"] is False
        assert payload["has_errors"] is True

    def test_empty_output(self):
        tool = ShellExecTool()
        meta = tool.parse_output(stdout="", stderr="", exit_code=0, args=ShellExecArgs(command="noop"))
        assert meta["shell_exec"]["output_length"] == 0

    def test_kali_welcome_stripped(self):
        tool = ShellExecTool()
        noisy = "Kali Linux\nwelcome\nreal output"
        meta = tool.parse_output(stdout=noisy, stderr="", exit_code=0, args=ShellExecArgs(command="echo"))
        assert meta["shell_exec"]["output_length"] <= len(noisy)

    def test_error_extraction(self):
        tool = ShellExecTool()
        meta = tool.parse_output(stdout="", stderr="fatal: failure\ntraceback", exit_code=1, args=ShellExecArgs(command="x"))
        assert meta["shell_exec"]["has_errors"] is True


class TestShellExecCreateArtifacts:
    def test_large_output_artifact(self, workspace: Path):
        tool = ShellExecTool()
        big = "A" * (11 * 1024)
        paths = tool.create_artifacts(stdout=big, args=ShellExecArgs(command="x"))
        assert any("shell_exec" in p for p in paths)
        for path in paths:
            assert Path(path).exists()

    def test_stderr_artifact(self, workspace: Path):
        tool = ShellExecTool()
        paths = tool.create_artifacts(stdout="", stderr="bad", args=ShellExecArgs(command="x"))
        assert any("errors" in p for p in paths)

    def test_small_output_no_artifact(self, workspace: Path):
        tool = ShellExecTool()
        paths = tool.create_artifacts(stdout="ok", args=ShellExecArgs(command="x"))
        assert paths == [] or all(Path(p).exists() for p in paths)

    def test_artifact_directory_creation(self, workspace: Path):
        tool = ShellExecTool()
        if Path("artifacts").exists():
            for child in Path("artifacts").iterdir():
                child.unlink()
            Path("artifacts").rmdir()
        paths = tool.create_artifacts(stdout="A" * (11 * 1024), args=ShellExecArgs(command="x"))
        assert Path("artifacts").exists()
        assert paths


class TestShellExecRun:
    def test_run_uses_build_command(self, monkeypatch):
        tool = ShellExecTool()
        called: Dict[str, bool] = {}

        def fake_build(args):
            called["build"] = True
            return ["bash", "-c", "echo ok"]

        monkeypatch.setattr(tool, "build_command", fake_build)
        monkeypatch.setattr("agent.tools.shell.exec._run_subprocess", lambda cmd, cwd, env, timeout: (0, "ok", "", 0))
        result = tool.run(ShellExecArgs(command="echo ok"))
        assert called.get("build") is True
        assert result.success is True

    def test_run_uses_parse_output(self, monkeypatch):
        tool = ShellExecTool()
        called: Dict[str, bool] = {}

        def fake_parse(stdout, stderr, exit_code, args):
            called["parse"] = True
            return {"shell_exec": {"command": args.command, "exit_code": exit_code, "success": True}}

        monkeypatch.setattr(tool, "parse_output", fake_parse)
        monkeypatch.setattr("agent.tools.shell.exec._run_subprocess", lambda cmd, cwd, env, timeout: (0, "ok", "", 0))
        result = tool.run(ShellExecArgs(command="echo ok"))
        assert called.get("parse") is True
        assert result.metadata["shell_exec"]["success"] is True

    def test_run_uses_create_artifacts(self, monkeypatch, workspace: Path):
        tool = ShellExecTool()
        called: Dict[str, bool] = {}

        def fake_artifacts(stdout, args, timestamp=None, stderr=""):
            called["artifacts"] = True
            return ["artifacts/shell_exec_fake.txt"]

        monkeypatch.setattr(tool, "create_artifacts", fake_artifacts)
        monkeypatch.setattr("agent.tools.shell.exec._run_subprocess", lambda cmd, cwd, env, timeout: (0, "ok", "", 0))
        result = tool.run(ShellExecArgs(command="echo ok"))
        assert called.get("artifacts") is True
        assert hasattr(result, "artifacts")

    def test_backward_compatibility(self, workspace: Path):
        tool = ShellExecTool()
        monkeypatch = patch("agent.tools.shell.exec._run_subprocess", return_value=(0, "legacy", "", 0))
        with monkeypatch:
            result = tool.run(ShellExecArgs(command="echo legacy"))
        assert result.success is True
        assert "shell_exec" in result.metadata


class TestShellScriptBuildCommand:
    def test_bash_script(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo 1", interpreter="bash")
        cmd = tool.build_command(args)
        assert Path(tool._last_script_path).exists()
        assert cmd[0] in ("bash", "sh")

    def test_python_script(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script='print("hi")', interpreter="python3")
        cmd = tool.build_command(args)
        assert cmd[0] == "python3"

    def test_strict_mode(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo 1", interpreter="bash", strict_mode=True)
        tool.build_command(args)
        content = Path(tool._last_script_path).read_text()
        assert "set -euo pipefail" in content

    def test_script_file_creation(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo from file", interpreter="bash")
        tool.build_command(args)
        assert Path(tool._last_script_path).exists()

    def test_supports_pty(self):
        assert ShellScriptTool().supports_pty() is True


class TestShellScriptParseOutput:
    def test_successful_script(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo hi", interpreter="bash")
        tool.build_command(args)
        meta = tool.parse_output(stdout="ok", stderr="", exit_code=0, args=args)
        assert meta["shell_script"]["success"] is True

    def test_failed_script(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="exit 1", interpreter="bash")
        tool.build_command(args)
        meta = tool.parse_output(stdout="", stderr="fail", exit_code=1, args=args)
        assert meta["shell_script"]["has_errors"] is True

    def test_metadata_includes_script_path(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo path", interpreter="bash")
        tool.build_command(args)
        meta = tool.parse_output(stdout="", stderr="", exit_code=0, args=args)
        assert meta["shell_script"]["script_path"] is not None


class TestShellScriptCreateArtifacts:
    def test_script_file_in_artifacts(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo hi", interpreter="bash")
        tool.build_command(args)
        paths = tool.create_artifacts(stdout="", args=args)
        assert any(tool._last_script_path in p or Path(tool._last_script_path) == Path(p) for p in paths)

    def test_large_output_artifact(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo hi", interpreter="bash")
        tool.build_command(args)
        big = "B" * (11 * 1024)
        paths = tool.create_artifacts(stdout=big, args=args)
        assert any("output" in p for p in paths)

    def test_stderr_artifact(self, workspace: Path):
        tool = ShellScriptTool()
        args = ShellScriptArgs(script="echo hi", interpreter="bash")
        tool.build_command(args)
        paths = tool.create_artifacts(stdout="", stderr="err", args=args)
        assert any("errors" in p for p in paths)


class TestShellScriptRun:
    def test_run_reuses_methods(self, monkeypatch, workspace: Path):
        tool = ShellScriptTool()
        called: Dict[str, bool] = {}

        def fake_build(args):
            called["build"] = True
            return ["bash", str(workspace / "scripts" / "noop.sh")]

        def fake_parse(stdout, stderr, exit_code, args):
            called["parse"] = True
            return {"shell_script": {"exit_code": exit_code, "success": True}}

        def fake_artifacts(stdout, args, timestamp=None, stderr=""):
            called["artifacts"] = True
            return []

        monkeypatch.setattr(tool, "build_command", fake_build)
        monkeypatch.setattr(tool, "parse_output", fake_parse)
        monkeypatch.setattr(tool, "create_artifacts", fake_artifacts)
        monkeypatch.setattr("agent.tools.shell.script._run_subprocess", lambda cmd, cwd, env, timeout: (0, "ok", "", 0))

        result = tool.run(ShellScriptArgs(script="echo hi", interpreter="bash"))
        assert all(flag in called for flag in ("build", "parse", "artifacts"))
        assert result.success is True

    def test_backward_compatibility(self, workspace: Path):
        tool = ShellScriptTool()
        monkeypatch = patch("agent.tools.shell.script._run_subprocess", return_value=(0, "legacy", "", 0))
        with monkeypatch:
            result = tool.run(ShellScriptArgs(script="echo hi", interpreter="bash"))
        assert result.success is True
        assert "shell_script" in result.metadata


class TestPTYIntegration:
    @pytest.mark.asyncio
    async def test_shell_exec_pty_routing(self, workspace: Path, monkeypatch):
        # Mock PTY executor to return success
        mock_result = MockShellCommandResult(stdout="ok", stderr="", exit_code=0)
        monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", AsyncMock(return_value=mock_result))

        executor = EnhancedCommandExecutor(config=_mock_config(task_id=1, workspace_path=str(workspace)))

        called: Dict[str, bool] = {}

        def fake_build(self, args):
            called["build"] = True
            return ["bash", "-c", "echo ok"]

        monkeypatch.setattr(ShellExecTool, "build_command", fake_build)

        result = await executor._execute_via_pty("shell.exec", {"command": "echo ok"})
        assert called.get("build") is True
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_shell_script_pty_routing(self, workspace: Path, monkeypatch):
        mock_result = MockShellCommandResult(stdout="script", stderr="", exit_code=0)
        monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", AsyncMock(return_value=mock_result))

        executor = EnhancedCommandExecutor(config=_mock_config(task_id=2, workspace_path=str(workspace)))

        called: Dict[str, bool] = {}

        def fake_build(self, args):
            called["build"] = True
            return ["bash", "-c", "echo script"]

        monkeypatch.setattr(ShellScriptTool, "build_command", fake_build)

        result = await executor._execute_via_pty("shell.script", {"script": "echo script", "interpreter": "bash"})
        assert called.get("build") is True
        assert result.stdout == "script"

    @pytest.mark.asyncio
    async def test_manual_mapping_fallback(self, workspace: Path, monkeypatch):
        mock_result = MockShellCommandResult(stdout="fallback", stderr="", exit_code=0)
        monkeypatch.setattr("agent.tools.shell._pty_executor.execute_via_pty", AsyncMock(return_value=mock_result))
        executor = EnhancedCommandExecutor(config=_mock_config(task_id=3, workspace_path=str(workspace)))

        # Force supports_pty to False to trigger legacy mapping
        monkeypatch.setattr(ShellExecTool, "supports_pty", lambda self: False)

        result = await executor._execute_via_pty("shell.exec", {"command": "echo legacy"})
        assert result.stdout == "fallback"
        assert result.exit_code == 0


