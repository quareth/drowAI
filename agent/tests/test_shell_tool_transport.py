"""Regression tests for shell tool transport boundaries."""

from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from agent.tools.enhanced_metadata import ToolCatalogRole
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.shell.contracts import ShellExecArgs, ShellScriptArgs
from agent.tools.shell.exec import ShellExecTool


def test_shell_exec_rejects_transport_selection() -> None:
    """The model-facing shell.exec contract exposes no transport selector."""
    schema = ShellExecArgs.model_json_schema()

    assert set(schema["properties"]) == {
        "command",
        "cwd",
        "env",
        "yield_time_ms",
        "max_output_chars",
        "max_runtime_sec",
    }
    assert "transport" not in schema["properties"]
    assert schema["additionalProperties"] is False

    for transport in ("file-comm", "pty", "direct", None):
        with pytest.raises(ValidationError):
            ShellExecArgs(command="echo test", transport=transport)


def test_shell_exec_direct_run_fails_closed_without_host_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct shell.exec calls validate policy but do not run on the host."""

    def fail_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("shell.exec must not invoke host subprocesses")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)

    result = ShellExecTool().run(ShellExecArgs(command="echo test"))

    assert result.success is False
    assert result.exit_code == -1
    assert "runtime-session dispatch" in result.stderr
    assert result.metadata["shell_exec"]["transport"] == "runtime-session"
    assert result.metadata["shell_exec"]["error_code"] == "shell_runtime_unavailable"


def test_shell_exec_policy_gate_runs_before_runtime_session_failure() -> None:
    result = ShellExecTool().run(ShellExecArgs(command="rm -rf /"))

    assert result.success is False
    assert "Policy violation" in result.stderr
    assert result.metadata["policy_violation"]["severity"] == "error"
    assert "shell_runtime_unavailable" not in result.stderr


def test_shell_exec_metadata_is_pty_only_session_utility() -> None:
    metadata = get_enhanced_tool_metadata("shell.exec")

    assert metadata is not None
    assert metadata.catalog_role == ToolCatalogRole.UTILITY
    assert metadata.supported_transports == ["pty"]
    assert metadata.__dict__["pty_support"] is True
    assert metadata.__dict__["pty_session_only"] is True
    assert "session_id" in metadata.capabilities[0].output_indicators


def test_shell_script_keeps_legacy_transport_compatibility() -> None:
    schema = ShellScriptArgs.model_json_schema()
    metadata = get_enhanced_tool_metadata("shell.script")

    assert "transport" in schema["properties"]
    assert ShellScriptArgs(script="echo test").transport is None
    assert (
        ShellScriptArgs(script="echo test", transport="file-comm").transport
        == "file-comm"
    )
    assert ShellScriptArgs(script="echo test", transport="pty").transport == "pty"
    with pytest.raises(ValidationError):
        ShellScriptArgs(script="echo test", transport="direct")

    assert metadata is not None
    assert metadata.supported_transports == ["file-comm", "pty"]
