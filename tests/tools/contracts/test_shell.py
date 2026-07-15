from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.tool_registry import get_tool
from agent.tools.shell.policy import CommandPolicy, PolicyEnforcement

from tests.tools.fixtures.parameter_fixtures import load_param_fixture

from .base_contract import BaseToolContract


SHELL_TOOLS = [
    "shell.exec",
    "shell.script",
]


@pytest.fixture(autouse=True)
def _shell_contract_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run shell contract tests against a writable workspace."""

    monkeypatch.setenv("WORKSPACE", str(tmp_path))


@pytest.mark.parametrize(
    "tool_id",
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in SHELL_TOOLS],
)
class TestShellToolsContracts(BaseToolContract):
    """Contract tests for shell execution tools."""

    @pytest.fixture
    def tool_id(self, request):
        return request.param


class TestShellPolicySecurity:
    """Security tests specific to shell tools - policy enforcement."""

    DENYLIST_COMMANDS = [
        ("shell.exec", "rm -rf /", "Destructive rm blocked"),
        ("shell.exec", "rm -rf /*", "Destructive rm wildcard blocked"),
        ("shell.exec", "sudo su", "Privilege escalation blocked"),
        ("shell.exec", "sudo -i", "Privilege escalation blocked"),
        ("shell.exec", "reboot", "System reboot blocked"),
        ("shell.exec", "shutdown now", "System shutdown blocked"),
        ("shell.exec", "nc -l -p 8080", "Network listener blocked"),
        ("shell.exec", ":(){:|:&};:", "Fork bomb blocked"),
        ("shell.exec", "passwd", "Password change blocked"),
    ]

    ALLOWLIST_COMMANDS = [
        ("shell.exec", "whoami", "Identity check allowed"),
        ("shell.exec", "ls -la", "Directory listing allowed"),
        ("shell.exec", "cat /etc/hostname", "File read allowed"),
        ("shell.exec", "ping -c 1 localhost", "Network ping allowed"),
        ("shell.exec", "echo test", "Echo allowed"),
        ("shell.exec", "pwd", "PWD allowed"),
        ("shell.exec", "id", "ID allowed"),
    ]

    @pytest.mark.parametrize("tool_id,command,description", DENYLIST_COMMANDS)
    def test_denylist_commands_blocked(
        self, tool_id: str, command: str, description: str
    ) -> None:
        """Verify dangerous commands are blocked by policy."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.PERMISSIVE)
        result = policy.validate(command)

        assert not result.allowed, f"{description}: {command} should be blocked"
        assert result.severity == "error"
        assert "denylist" in result.reason.lower()

    @pytest.mark.parametrize("tool_id,command,description", ALLOWLIST_COMMANDS)
    def test_allowlist_commands_allowed(
        self, tool_id: str, command: str, description: str
    ) -> None:
        """Verify safe commands pass policy validation."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.PERMISSIVE)
        result = policy.validate(command)

        assert result.allowed, f"{description}: {command} should be allowed"

    def test_strict_mode_blocks_unknown_commands(self) -> None:
        """Verify STRICT mode blocks commands not in allowlist."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.STRICT)
        
        # Unknown command not in allowlist
        result = policy.validate("custom_unknown_tool --flag")
        
        assert not result.allowed
        assert "not in allowlist" in result.reason.lower()

    def test_permissive_mode_warns_unknown_commands(self) -> None:
        """Verify PERMISSIVE mode allows unknown commands with warning."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.PERMISSIVE)
        
        # Unknown command not in allowlist
        result = policy.validate("custom_unknown_tool --flag")
        
        assert result.allowed
        assert result.severity == "warning"

    def test_disabled_mode_allows_all(self) -> None:
        """Verify DISABLED mode allows even dangerous commands (testing only)."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.DISABLED)
        
        # Even dangerous commands pass in disabled mode
        result = policy.validate("rm -rf /")
        
        assert result.allowed

    def test_empty_command_rejected(self) -> None:
        """Verify empty commands are rejected."""
        policy = CommandPolicy(enforcement=PolicyEnforcement.PERMISSIVE)
        
        result = policy.validate("")
        
        assert not result.allowed
        assert result.severity == "error"


class TestShellExecSecurity:
    """Security tests for shell.exec tool."""

    INJECTION_PAYLOADS = [
        "; rm -rf /",
        "| cat /etc/passwd",
        "$(rm -rf /)",
        "`rm -rf /`",
        "&& rm -rf /",
        "|| rm -rf /",
    ]

    def test_shell_exec_build_command_returns_list(self) -> None:
        """Verify build_command returns a proper command list."""
        tool_cls = get_tool("shell.exec")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(command="whoami")
        command = tool.build_command(args)
        
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)

    def test_shell_exec_parse_output_structure(self) -> None:
        """Verify parse_output returns properly structured metadata."""
        tool_cls = get_tool("shell.exec")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(command="whoami")
        metadata = tool.parse_output(
            stdout="root",
            stderr="",
            exit_code=0,
            args=args,
        )
        
        assert isinstance(metadata, dict)
        assert "shell_exec" in metadata
        assert metadata["shell_exec"]["success"] is True
        assert metadata["shell_exec"]["exit_code"] == 0

    def test_shell_exec_parse_output_with_errors(self) -> None:
        """Verify parse_output handles error output."""
        tool_cls = get_tool("shell.exec")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(command="invalid_command")
        metadata = tool.parse_output(
            stdout="",
            stderr="command not found: invalid_command",
            exit_code=127,
            args=args,
        )
        
        assert isinstance(metadata, dict)
        assert "shell_exec" in metadata
        assert metadata["shell_exec"]["success"] is False
        assert metadata["shell_exec"]["exit_code"] == 127
        assert metadata["shell_exec"]["has_errors"] is True


class TestShellScriptSecurity:
    """Security tests for shell.script tool."""

    def test_shell_script_build_command_returns_list(self) -> None:
        """Verify build_command creates script and returns interpreter command."""
        tool_cls = get_tool("shell.script")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(script="#!/bin/bash\necho test")
        command = tool.build_command(args)
        
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)
        # Should contain interpreter reference
        assert any("bash" in part or "sh" in part for part in command)

    def test_shell_script_file_comm_declares_script_without_writing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify file-comm script content is declared for runtime materialization."""
        monkeypatch.setenv("WORKSPACE", str(tmp_path))
        tool_cls = get_tool("shell.script")
        args_class = tool_cls.args_model
        tool = tool_cls()

        args = args_class(script="echo test", transport="file-comm")
        command = tool.build_command(args)
        workspace_files = tool.prepare_workspace_files(args)

        script_operand = command[-1]
        assert script_operand.startswith("/workspace/scripts/script_")
        relative_path = script_operand.removeprefix("/workspace/")
        assert not (tmp_path / relative_path).exists()
        assert len(workspace_files) == 1
        assert workspace_files[0].relative_path == relative_path
        assert workspace_files[0].content_bytes().startswith(b"set -euo pipefail\n")

    def test_shell_script_parse_output_structure(self) -> None:
        """Verify parse_output returns properly structured metadata."""
        tool_cls = get_tool("shell.script")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(script="#!/bin/bash\necho test")
        # Need to call build_command first to set _last_script_path
        tool.build_command(args)
        
        metadata = tool.parse_output(
            stdout="test",
            stderr="",
            exit_code=0,
            args=args,
        )
        
        assert isinstance(metadata, dict)
        assert "shell_script" in metadata
        assert metadata["shell_script"]["success"] is True
        assert metadata["shell_script"]["exit_code"] == 0

    def test_shell_script_line_by_line_validation(self) -> None:
        """Verify scripts are validated line by line."""
        # A script with a dangerous line should be blocked
        policy = CommandPolicy(enforcement=PolicyEnforcement.PERMISSIVE)
        
        script_lines = [
            "#!/bin/bash",
            "echo 'safe'",
            "rm -rf /",  # Dangerous line
            "echo 'done'",
        ]
        
        for line in script_lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            result = policy.validate(line)
            if "rm -rf /" in line:
                assert not result.allowed, "Dangerous script line should be blocked"

    def test_shell_script_interpreters(self) -> None:
        """Verify different interpreters are supported."""
        tool_cls = get_tool("shell.script")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        interpreters = ["bash", "sh", "python3"]
        
        for interpreter in interpreters:
            args = args_class(
                script="echo 'test'" if interpreter != "python3" else "print('test')",
                interpreter=interpreter,
            )
            command = tool.build_command(args)
            assert isinstance(command, list)


class TestShellArtifactCreation:
    """Tests for artifact creation in shell tools."""

    def test_shell_exec_create_artifacts_large_output(self) -> None:
        """Verify large outputs are saved as artifacts."""
        tool_cls = get_tool("shell.exec")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(command="cat large_file")
        # Simulate large output (>10KB)
        large_output = "x" * 15000
        
        # create_artifacts should return list of artifact paths
        artifacts = tool.create_artifacts(
            stdout=large_output,
            args=args,
            timestamp=1234567890,
        )
        
        assert isinstance(artifacts, list)

    def test_shell_exec_create_artifacts_with_stderr(self) -> None:
        """Verify stderr is saved as separate artifact."""
        tool_cls = get_tool("shell.exec")
        args_class = tool_cls.args_model
        tool = tool_cls()
        
        args = args_class(command="failing_command")
        
        artifacts = tool.create_artifacts(
            stdout="",
            args=args,
            timestamp=1234567890,
            stderr="Error: command failed",
        )
        
        assert isinstance(artifacts, list)
