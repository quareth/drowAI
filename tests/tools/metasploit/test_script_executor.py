"""
Tests for the Metasploit Script Executor.

The script executor runs msfconsole in non-interactive mode using
the -x flag for stateless operations.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

from agent.tools.exploitation_tools.metasploit.script_executor import (
    ScriptResult,
    ScriptExecutor,
    build_module_commands,
    build_search_command,
    result_to_dict,
    get_script_executor,
)


class TestScriptExecutor:
    """Test ScriptExecutor class."""

    @pytest.fixture
    def executor(self):
        """Create ScriptExecutor instance."""
        return ScriptExecutor(quiet=True)

    def test_build_command_line_simple(self, executor):
        """Build simple command line."""
        cmd = executor.build_command_line(["search smb"])

        assert "msfconsole" in cmd
        assert "-q" in cmd
        assert "-x" in cmd
        # Commands should be joined with semicolons
        command_str = cmd[cmd.index("-x") + 1]
        assert "search smb" in command_str
        assert "exit" in command_str

    def test_build_command_line_multiple_commands(self, executor):
        """Build command line with multiple commands."""
        cmd = executor.build_command_line([
            "use exploit/multi/handler",
            "set PAYLOAD windows/meterpreter/reverse_tcp",
            "set LHOST 192.168.1.100",
        ])

        command_str = cmd[cmd.index("-x") + 1]
        assert "use" in command_str
        assert "set PAYLOAD" in command_str
        assert "set LHOST" in command_str

    def test_build_command_line_adds_exit(self, executor):
        """Exit command should be added automatically."""
        cmd = executor.build_command_line(["search smb"])
        command_str = cmd[cmd.index("-x") + 1]

        assert "exit" in command_str

    def test_build_command_line_respects_quiet(self):
        """Quiet flag should be respected."""
        executor_quiet = ScriptExecutor(quiet=True)
        executor_verbose = ScriptExecutor(quiet=False)

        cmd_quiet = executor_quiet.build_command_line(["version"])
        cmd_verbose = executor_verbose.build_command_line(["version"])

        assert "-q" in cmd_quiet
        assert "-q" not in cmd_verbose

    @patch("subprocess.run")
    def test_execute_sync_success(self, mock_run, executor, sample_search_output):
        """Test synchronous execution success."""
        mock_run.return_value = MagicMock(
            stdout=sample_search_output,
            stderr="",
            returncode=0,
        )

        result = executor.execute_sync(["search smb"])

        assert result.success is True
        assert result.exit_code == 0
        assert result.parsed is not None

    @patch("subprocess.run")
    def test_execute_sync_failure(self, mock_run, executor):
        """Test synchronous execution failure."""
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="Error: command not found",
            returncode=1,
        )

        result = executor.execute_sync(["invalid_command"])

        assert result.success is False
        assert result.exit_code == 1

    @patch("subprocess.run")
    def test_execute_sync_timeout(self, mock_run, executor):
        """Test timeout handling."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=10)

        result = executor.execute_sync(["slow_command"], timeout_sec=10)

        assert result.success is False
        assert "timed out" in result.stderr.lower() or "timeout" in result.stderr.lower()

    @patch("subprocess.run")
    def test_execute_sync_not_found(self, mock_run, executor):
        """Test handling of msfconsole not found."""
        mock_run.side_effect = FileNotFoundError()

        result = executor.execute_sync(["search smb"])

        assert result.success is False
        assert "not found" in result.stderr.lower()


class TestBuildModuleCommands:
    """Test build_module_commands helper."""

    def test_basic_module_commands(self):
        """Build basic module setup commands."""
        commands = build_module_commands(
            "exploit/windows/smb/ms17_010_eternalblue",
            execute=False,
        )

        assert commands == ["use exploit/windows/smb/ms17_010_eternalblue"]

    def test_module_with_options(self):
        """Build module commands with options."""
        commands = build_module_commands(
            "exploit/windows/smb/ms17_010_eternalblue",
            options={"RHOSTS": "192.168.1.50", "LHOST": "192.168.1.100"},
            execute=False,
        )

        assert "use exploit/windows/smb/ms17_010_eternalblue" in commands
        assert "set RHOSTS 192.168.1.50" in commands
        assert "set LHOST 192.168.1.100" in commands

    def test_module_with_execute(self):
        """Build module commands with execution."""
        commands = build_module_commands(
            "exploit/windows/smb/ms17_010_eternalblue",
            execute=True,
        )

        assert "exploit" in commands

    def test_auxiliary_module_uses_run(self):
        """Auxiliary modules should use 'run' command."""
        commands = build_module_commands(
            "auxiliary/scanner/smb/smb_ms17_010",
            execute=True,
        )

        assert "run" in commands

    def test_exploit_as_background_job(self):
        """Exploit with background job flag."""
        commands = build_module_commands(
            "exploit/multi/handler",
            execute=True,
            exploit_as_job=True,
        )

        assert "exploit -j" in commands


class TestBuildSearchCommand:
    """Test build_search_command helper."""

    def test_simple_search(self):
        """Build simple search command."""
        cmd = build_search_command("smb")

        assert cmd == "search smb"

    def test_search_with_type_filter(self):
        """Build search with type filter."""
        cmd = build_search_command("smb", module_type="exploit")

        assert "search smb" in cmd
        assert "type:exploit" in cmd

    def test_search_with_platform_filter(self):
        """Build search with platform filter."""
        cmd = build_search_command("smb", platform="windows")

        assert "platform:windows" in cmd

    def test_search_with_cve_filter(self):
        """Build search with CVE filter."""
        cmd = build_search_command("eternal", cve="2017-0143")

        assert "cve:2017-0143" in cmd


class TestScriptResult:
    """Test ScriptResult dataclass."""

    def test_result_to_dict(self, sample_search_output):
        """Test converting result to dictionary."""
        from agent.tools.exploitation_tools.metasploit.output_parser import (
            parse_msfconsole_output,
        )

        result = ScriptResult(
            success=True,
            stdout=sample_search_output,
            stderr="",
            exit_code=0,
            parsed=parse_msfconsole_output(sample_search_output),
            execution_time=1.5,
            command="msfconsole -x 'search smb'",
        )

        d = result_to_dict(result)

        assert d["success"] is True
        assert d["exit_code"] == 0
        assert d["execution_time"] == 1.5
        assert "parsed" in d


class TestGetScriptExecutor:
    """Test module-level convenience function."""

    def test_get_script_executor_returns_instance(self):
        """get_script_executor should return instance."""
        executor = get_script_executor()
        assert isinstance(executor, ScriptExecutor)

    def test_get_script_executor_is_cached(self):
        """get_script_executor should return same instance."""
        executor1 = get_script_executor()
        executor2 = get_script_executor()
        assert executor1 is executor2
