"""Tests for shell tool transport parameter support."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.shell.contracts import ShellExecArgs, ShellScriptArgs
from agent.tools.shell.exec import ShellExecTool
from agent.tools.shell.script import ShellScriptTool


@pytest.fixture()
def workspace(tmp_path: Path):
    """Set up a temporary workspace for tests."""
    original = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    yield tmp_path
    if original is None:
        os.environ.pop("WORKSPACE", None)
    else:
        os.environ["WORKSPACE"] = original


class TestShellTransportSchema:
    """Test transport type and schema definitions."""

    def test_shell_transport_values(self):
        """Test that ShellTransport has correct literal values."""
        # ShellTransport is a Literal type, we can test it via schema validation
        valid_values = ["file-comm", "pty"]
        
        # Test that valid values work
        for value in valid_values:
            args = ShellExecArgs(command="echo test", transport=value)
            assert args.transport == value

    def test_shell_exec_args_transport_optional(self):
        """Test that transport parameter is optional and defaults to None."""
        args = ShellExecArgs(command="echo test")
        assert args.transport is None

    def test_shell_exec_args_with_transport(self):
        """Test ShellExecArgs accepts container transport values."""
        # Test file-comm
        args_file_comm = ShellExecArgs(command="echo test", transport="file-comm")
        assert args_file_comm.transport == "file-comm"
        
        # Test pty
        args_pty = ShellExecArgs(command="echo test", transport="pty")
        assert args_pty.transport == "pty"

    def test_shell_script_args_transport_optional(self):
        """Test that transport parameter is optional in ShellScriptArgs."""
        args = ShellScriptArgs(script="#!/bin/bash\necho test")
        assert args.transport is None

    def test_shell_script_args_with_transport(self):
        """Test ShellScriptArgs accepts container transport values."""
        # Test file-comm
        args_file_comm = ShellScriptArgs(script="echo test", transport="file-comm")
        assert args_file_comm.transport == "file-comm"
        
        # Test pty
        args_pty = ShellScriptArgs(script="echo test", transport="pty")
        assert args_pty.transport == "pty"


class TestShellToolExecution:
    """Test shell tool execution with transport parameter."""

    def test_shell_exec_with_auto_select(self):
        """Test shell.exec with auto-selected transport (None)."""
        tool = ShellExecTool()
        args = ShellExecArgs(command="echo 'Hello World'")
        
        result = tool.run(args)
        
        assert result.success is True
        assert result.exit_code == 0
        assert "Hello World" in result.stdout

    def test_shell_exec_direct_run_compatibility_without_transport(self):
        """Direct tool.run remains a local compatibility path when no transport is set."""
        tool = ShellExecTool()
        args = ShellExecArgs(command="echo 'test'")
        
        result = tool.run(args)
        
        # Tool implementation uses direct execution
        assert result.success is True
        assert result.exit_code == 0

    def test_shell_exec_backward_compatibility(self):
        """Test that existing calls without transport parameter work."""
        tool = ShellExecTool()
        # Old-style call without transport
        args = ShellExecArgs(command="whoami")
        
        result = tool.run(args)
        
        # Should work as before
        assert result.success is True
        assert result.exit_code == 0
        assert result.stdout != ""

    def test_shell_script_accepts_transport_parameter(self, workspace):
        """Test that shell.script accepts transport parameter."""
        tool = ShellScriptTool()
        
        # Test with auto-select (None)
        args_auto = ShellScriptArgs(script="echo 'test'")
        assert args_auto.transport is None
        
        # Test with pty transport (accepted by schema)
        args_pty = ShellScriptArgs(script="echo 'test'", transport="pty")
        assert args_pty.transport == "pty"

    def test_shell_script_transport_in_metadata(self, workspace):
        """Test that shell.script execution includes transport in result."""
        tool = ShellScriptTool()
        # Simple script that should work cross-platform
        args = ShellScriptArgs(script="echo 'test'")
        
        result = tool.run(args)
        
        # Should have shell_script metadata
        assert "shell_script" in result.metadata
        shell_result = result.metadata["shell_script"]
        
        # Should track transport used
        assert "transport" in shell_result
        assert shell_result["transport"] == "direct"

    def test_shell_script_backward_compatibility(self):
        """Test shell.script schema backward compatibility."""
        # Old-style call without transport should work
        args = ShellScriptArgs(script="echo 'legacy'")
        assert args.transport is None  # Defaults to None (auto-select)


class TestTransportMetadata:
    """Test transport metadata in tool registry."""

    def test_shell_exec_has_transport_metadata(self):
        """Test that shell.exec has supported_transports in metadata."""
        metadata = get_enhanced_tool_metadata("shell.exec")
        
        assert metadata is not None
        assert metadata.supported_transports is not None
        assert "direct" not in metadata.supported_transports
        assert "file-comm" in metadata.supported_transports
        assert "pty" in metadata.supported_transports

    def test_shell_script_has_transport_metadata(self):
        """Test that shell.script has supported_transports in metadata."""
        metadata = get_enhanced_tool_metadata("shell.script")
        
        assert metadata is not None
        assert metadata.supported_transports is not None
        assert "direct" not in metadata.supported_transports
        assert "file-comm" in metadata.supported_transports
        assert "pty" in metadata.supported_transports

    def test_transport_metadata_completeness(self):
        """Test that shell tools have complete transport metadata."""
        for tool_id in ["shell.exec", "shell.script"]:
            metadata = get_enhanced_tool_metadata(tool_id)
            
            assert metadata is not None, f"{tool_id} not registered"
            assert metadata.supported_transports is not None, \
                f"{tool_id} missing supported_transports"
            assert len(metadata.supported_transports) == 2, \
                f"{tool_id} should support 2 transports"


class TestTransportDocumentation:
    """Test that transport parameter is well-documented."""

    def test_shell_exec_args_has_transport_docs(self):
        """Test that ShellExecArgs transport field has documentation."""
        # Check that the field has description in schema
        schema = ShellExecArgs.model_json_schema()
        transport_field = schema["properties"]["transport"]
        
        assert "description" in transport_field
        assert "direct" not in transport_field["description"]
        assert "pty" in transport_field["description"]

    def test_shell_script_args_has_transport_docs(self):
        """Test that ShellScriptArgs transport field has documentation."""
        schema = ShellScriptArgs.model_json_schema()
        transport_field = schema["properties"]["transport"]
        
        assert "description" in transport_field
        assert "direct" not in transport_field["description"]
        assert "pty" in transport_field["description"]

    def test_shell_exec_tool_has_docstring(self):
        """Test that ShellExecTool has docstring mentioning transport."""
        assert ShellExecTool.__doc__ is not None
        docstring = ShellExecTool.__doc__
        
        # Should mention transport
        assert "transport" in docstring.lower()

    def test_shell_script_tool_has_docstring(self):
        """Test that ShellScriptTool has docstring mentioning transport."""
        assert ShellScriptTool.__doc__ is not None
        docstring = ShellScriptTool.__doc__
        
        # Should mention transport
        assert "transport" in docstring.lower()


class TestTransportValidation:
    """Test transport parameter validation."""

    def test_invalid_transport_value_rejected(self):
        """Test that invalid transport values are rejected by Pydantic."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            ShellExecArgs(command="echo test", transport="invalid")

    def test_direct_transport_value_rejected(self):
        """Test that direct transport is rejected for shell container tools."""
        with pytest.raises(ValidationError):
            ShellExecArgs(command="echo test", transport="direct")
        with pytest.raises(ValidationError):
            ShellScriptArgs(script="echo test", transport="direct")

    def test_none_transport_accepted(self):
        """Test that None is accepted for transport (auto-select)."""
        args = ShellExecArgs(command="echo test", transport=None)
        assert args.transport is None

    def test_transport_field_in_result_metadata(self):
        """Test that tool execution includes transport in metadata."""
        tool = ShellExecTool()
        args = ShellExecArgs(command="echo 'metadata test'")
        
        result = tool.run(args)
        
        # Result should include shell execution metadata
        assert "shell_exec" in result.metadata
        shell_result = result.metadata["shell_exec"]
        
        # Should track which transport was used
        assert "transport" in shell_result
        assert shell_result["transport"] in ["direct", "file-comm", "pty"]


class TestPTYTransportHandling:
    """Test PTY transport handling (executor routing)."""

    def test_pty_transport_not_handled_by_tool(self):
        """
        Test that PTY transport is routed by executor, not tool.
        
        When transport="pty", the executor should intercept the call
        and execute it via PTY, so the tool's run() method should
        not be invoked with PTY transport in practice.
        
        However, if it is called directly (bypassing executor),
        it should execute with direct transport (current implementation).
        """
        tool = ShellExecTool()
        # Direct tool call with PTY (bypassing executor)
        args = ShellExecArgs(command="echo 'pty test'", transport="pty")
        
        # Tool will execute with direct transport since executor routing
        # is not involved in this unit test
        result = tool.run(args)
        
        # Should still work (fallback to direct)
        assert result.success is True


# Phase 3.1 Implementation Verification
class TestPhase3_1_Implementation:
    """Verify all Phase 3.1 requirements are met."""

    def test_3_1_1_shell_transport_updated(self):
        """Verify shell schemas support file-comm and pty."""
        for transport in ["file-comm", "pty"]:
            args = ShellExecArgs(command="echo test", transport=transport)
            assert args.transport == transport

    def test_3_1_2_shell_exec_args_enhanced(self):
        """Verify ShellExecArgs has enhanced transport documentation."""
        schema = ShellExecArgs.model_json_schema()
        assert "transport" in schema["properties"]
        assert schema["properties"]["transport"].get("anyOf") or \
               schema["properties"]["transport"].get("enum")

    def test_3_1_3_shell_script_args_enhanced(self):
        """Verify ShellScriptArgs has enhanced transport documentation."""
        schema = ShellScriptArgs.model_json_schema()
        assert "transport" in schema["properties"]

    def test_3_1_4_transport_metadata_registered(self):
        """Verify transport metadata in tool registry."""
        shell_exec_metadata = get_enhanced_tool_metadata("shell.exec")
        shell_script_metadata = get_enhanced_tool_metadata("shell.script")
        
        assert shell_exec_metadata.supported_transports == ["file-comm", "pty"]
        assert shell_script_metadata.supported_transports == ["file-comm", "pty"]

    def test_3_1_5_tool_docstrings_updated(self):
        """Verify tool docstrings mention transport."""
        exec_doc = ShellExecTool.__doc__
        script_doc = ShellScriptTool.__doc__
        
        assert exec_doc and "transport" in exec_doc.lower()
        assert script_doc and "transport" in script_doc.lower()

    def test_backward_compatibility_maintained(self):
        """Verify existing calls without transport work unchanged."""
        # Test shell.exec schema
        exec_args = ShellExecArgs(command="echo 'test'")
        assert exec_args.transport is None  # Auto-select
        
        # Test shell.script schema
        script_args = ShellScriptArgs(script="echo 'test'")
        assert script_args.transport is None  # Auto-select
        
        # Verify tools can be instantiated
        exec_tool = ShellExecTool()
        script_tool = ShellScriptTool()
        assert exec_tool is not None
        assert script_tool is not None
