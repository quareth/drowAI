"""Tests for model-facing shell tool argument contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.tools.catalog_visibility import is_tool_hidden_from_catalog
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.enhanced_metadata import ToolCatalogRole
from agent.tools.categories import ToolCategory
from agent.tools.shell.contracts import (
    ShellExecArgs,
    ShellScriptArgs,
    ShellWriteStdinArgs,
)
from agent.tools.shell.exec import ShellExecTool
from agent.tools.shell.policy import validate_shell_exec_command
from agent.tools.shell.write_stdin import ShellWriteStdinTool
from runtime_shared.shell_session_contracts import (
    SHELL_SESSION_MAX_ENV_ENTRIES,
    SHELL_SESSION_MAX_ENV_TOTAL_BYTES,
)


def _schema_fields(model: type[ShellExecArgs] | type[ShellWriteStdinArgs]) -> set[str]:
    return set(model.model_json_schema()["properties"])


def test_shell_exec_schema_matches_session_contract_surface() -> None:
    schema = ShellExecArgs.model_json_schema()

    assert _schema_fields(ShellExecArgs) == {
        "command",
        "cwd",
        "env",
        "yield_time_ms",
        "max_output_chars",
        "max_runtime_sec",
    }
    assert schema["additionalProperties"] is False
    assert not {
        "transport",
        "timeout_sec",
        "idempotent",
        "redact_output",
    } & _schema_fields(ShellExecArgs)


def test_shell_exec_uses_shared_env_limits() -> None:
    too_many_entries = {
        f"KEY_{index}": "value"
        for index in range(SHELL_SESSION_MAX_ENV_ENTRIES + 1)
    }
    too_many_bytes = {"KEY": "x" * SHELL_SESSION_MAX_ENV_TOTAL_BYTES}
    boundary_env = {
        "KEY": "x" * (SHELL_SESSION_MAX_ENV_TOTAL_BYTES - len("KEY"))
    }

    with pytest.raises(ValidationError):
        ShellExecArgs(command="env", env=too_many_entries)
    with pytest.raises(ValidationError):
        ShellExecArgs(command="env", env=too_many_bytes)

    assert ShellExecArgs(command="env", env=boundary_env).env == boundary_env


def test_shell_exec_rejects_removed_legacy_fields() -> None:
    with pytest.raises(ValidationError):
        ShellExecArgs(command="echo test", transport="pty")
    with pytest.raises(ValidationError):
        ShellExecArgs(command="echo test", timeout_sec=5)
    with pytest.raises(ValidationError):
        ShellExecArgs(command="echo test", idempotent=False)
    with pytest.raises(ValidationError):
        ShellExecArgs(command="echo test", redact_output=False)


def test_shell_write_stdin_schema_and_input_forms() -> None:
    schema = ShellWriteStdinArgs.model_json_schema()

    assert _schema_fields(ShellWriteStdinArgs) == {
        "session_id",
        "chars",
        "yield_time_ms",
        "max_output_chars",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["session_id"]["maxLength"] == 128
    assert schema["properties"]["chars"]["maxLength"] == 16_384
    assert schema["properties"]["yield_time_ms"]["maximum"] == 30_000
    assert schema["properties"]["max_output_chars"]["minimum"] == 1_024
    assert schema["properties"]["max_output_chars"]["maximum"] == 128_000

    assert ShellWriteStdinArgs(session_id="shs_123").chars == ""
    assert (
        ShellWriteStdinArgs(session_id="shs_123", chars="\u0003").chars
        == "\u0003"
    )


def test_shell_script_schema_keeps_legacy_fields() -> None:
    assert _schema_fields(ShellScriptArgs) == {
        "script",
        "interpreter",
        "cwd",
        "env",
        "timeout_sec",
        "transport",
        "strict_mode",
    }


def test_shell_exec_direct_run_fails_closed_without_host_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("direct host subprocess must not be called")

    monkeypatch.setattr(subprocess, "run", fail_subprocess)

    result = ShellExecTool().run(ShellExecArgs(command="echo should-not-run"))

    assert result.success is False
    assert result.exit_code == -1
    assert result.artifacts == []
    assert not (tmp_path / "artifacts").exists()
    assert "runtime-session dispatch" in result.stderr
    assert result.metadata["shell_exec"]["error_code"] == "shell_runtime_unavailable"


def test_shell_exec_direct_run_policy_gate_remains_first() -> None:
    result = ShellExecTool().run(ShellExecArgs(command="rm -rf /"))

    assert result.success is False
    assert "Policy violation" in result.stderr
    assert "shell_runtime_unavailable" not in result.stderr
    assert "policy_violation" in result.metadata


def test_shell_write_stdin_adapter_builds_exact_service_request() -> None:
    args = ShellWriteStdinArgs(
        session_id="shs_123",
        chars="answer without adapter newline",
        yield_time_ms=250,
        max_output_chars=4096,
    )

    request = ShellWriteStdinTool().build_request(args)

    assert request.session_id == "shs_123"
    assert request.chars == "answer without adapter newline"
    assert not request.chars.endswith("\n")
    assert request.yield_time_ms == 250
    assert request.max_output_chars == 4096


def test_shell_write_stdin_direct_run_fails_closed() -> None:
    result = ShellWriteStdinTool().run(
        ShellWriteStdinArgs(session_id="shs_123", chars="\u0003")
    )

    assert result.success is False
    assert result.exit_code == -1
    assert result.artifacts == []
    assert "runtime-session dispatch" in result.stderr
    assert result.metadata["shell_write_stdin"]["session_id"] == "shs_123"
    assert (
        result.metadata["shell_write_stdin"]["error_code"]
        == "shell_runtime_unavailable"
    )
    assert "chars" not in result.metadata["shell_write_stdin"]


def test_shell_exec_policy_no_longer_rejects_length_only() -> None:
    command = "echo " + ("a" * 1_000)

    assert validate_shell_exec_command(command) == []


def test_shell_exec_policy_keeps_chained_removal_protection() -> None:
    errors = validate_shell_exec_command("echo safe; rm ./target")

    assert errors
    assert any("Chained removal blocked" in error["error"] for error in errors)


def test_shell_metadata_registers_exec_as_pty_only_session_utility() -> None:
    metadata = get_enhanced_tool_metadata("shell.exec")

    assert metadata is not None
    assert metadata.category == ToolCategory.SHELL
    assert metadata.catalog_role == ToolCatalogRole.UTILITY
    assert metadata.supported_transports == ["pty"]
    assert metadata.__dict__["pty_support"] is True
    assert metadata.__dict__["pty_session_only"] is True
    assert "session_id" in metadata.capabilities[0].output_indicators


def test_shell_metadata_registers_write_stdin_as_pty_only_session_utility() -> None:
    metadata = get_enhanced_tool_metadata("shell.write_stdin")

    assert metadata is not None
    assert metadata.category == ToolCategory.SHELL
    assert metadata.catalog_role == ToolCatalogRole.UTILITY
    assert metadata.supported_transports == ["pty"]
    assert metadata.__dict__["pty_support"] is True
    assert metadata.__dict__["pty_session_only"] is True
    assert "process_status" in metadata.capabilities[0].output_indicators


def test_shell_script_metadata_remains_registered_and_hidden() -> None:
    metadata = get_enhanced_tool_metadata("shell.script")

    assert metadata is not None
    assert metadata.category == ToolCategory.SHELL
    assert metadata.catalog_role == ToolCatalogRole.UTILITY
    assert metadata.supported_transports == ["file-comm", "pty"]
    assert is_tool_hidden_from_catalog("shell.script") is True
