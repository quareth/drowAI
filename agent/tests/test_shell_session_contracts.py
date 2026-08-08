"""Tests for shared shell-session contracts."""

from __future__ import annotations

from dataclasses import asdict
import inspect

import pytest
from pydantic import ValidationError

from runtime_shared.shell_session_contracts import (
    SHELL_SESSION_ERROR_CODES,
    SHELL_SESSION_MAX_ENV_ENTRIES,
    SHELL_SESSION_MAX_ENV_TOTAL_BYTES,
    SHELL_SESSION_PROTECTED_ENV_NAMES,
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionUpdate,
    ShellWriteRequest,
)


def test_shared_contract_module_is_backend_free() -> None:
    """The shared module must not import backend-owned services."""
    import runtime_shared.shell_session_contracts as contracts

    assert contracts.__doc__
    assert "purpose" in contracts.__doc__.lower()
    source = inspect.getsource(contracts)
    assert "from backend" not in source
    assert "import backend" not in source


def test_shell_session_identity_contains_runtime_authority_fields() -> None:
    identity = ShellSessionIdentity(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:turn-123",
        runtime_placement_mode="runner",
        workspace_id="workspace-abc",
        workspace_path="/workspace",
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    assert identity.tenant_id == 7
    assert identity.task_id == 11
    assert identity.execution_owner_id == "main:turn-123"
    assert identity.runtime_placement_mode == "runner"
    assert identity.workspace_id == "workspace-abc"
    assert identity.workspace_path == "/workspace"
    assert identity.runner_id == "runner-1"
    assert identity.execution_site_id == "site-1"


def test_shell_session_identity_serialization_round_trip() -> None:
    identity = ShellSessionIdentity(
        tenant_id=7,
        task_id=11,
        execution_owner_id="main:turn-123",
        runtime_placement_mode="runner",
        workspace_id="workspace-abc",
        workspace_path="/workspace",
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    restored = ShellSessionIdentity(**asdict(identity))

    assert restored == identity


def test_shell_request_contracts_json_round_trip() -> None:
    exec_request = ShellExecRequest(
        command="printf 'ok'",
        cwd="/workspace/src",
        env={"UNICODE": "caf\u00e9"},
        yield_time_ms=123,
        max_output_chars=2048,
        max_runtime_sec=5,
    )
    write_request = ShellWriteRequest(
        session_id="shs_abc123",
        chars="input\n",
        yield_time_ms=456,
        max_output_chars=4096,
    )

    assert ShellExecRequest.model_validate_json(
        exec_request.model_dump_json()
    ).model_dump(mode="json") == exec_request.model_dump(mode="json")
    assert ShellWriteRequest.model_validate_json(
        write_request.model_dump_json()
    ).model_dump(mode="json") == write_request.model_dump(mode="json")


def test_shell_session_update_supports_nullable_fields_and_deterministic_summary() -> None:
    update = ShellSessionUpdate(
        success=True,
        status="success",
        process_status=ShellProcessStatus.RUNNING,
        session_id="shs_abc123",
        stdout="progress\n",
        stderr="",
        exit_code=None,
        stdin_available=True,
        truncated=False,
        duration_ms=10_004,
    )

    assert update.process_status is ShellProcessStatus.RUNNING
    assert update.session_id == "shs_abc123"
    assert update.exit_code is None
    assert update.stdin_available is True
    assert update.truncated is False
    assert (
        update.summary
        == "Command is still running; poll session shs_abc123 for more output."
    )
    assert update.error_code is None


def test_shell_session_update_json_round_trip_preserves_enums() -> None:
    update = ShellSessionUpdate(
        success=False,
        status="error",
        process_status=ShellProcessStatus.TIMED_OUT,
        session_id=None,
        stdout="partial",
        stderr="",
        exit_code=None,
        stdin_available=False,
        truncated=False,
        duration_ms=250,
        error_code=ShellSessionErrorCode.COMMAND_TIMED_OUT,
    )

    restored = ShellSessionUpdate.model_validate_json(update.model_dump_json())

    assert restored.model_dump(mode="json") == update.model_dump(mode="json")
    assert restored.process_status is ShellProcessStatus.TIMED_OUT
    assert restored.error_code is ShellSessionErrorCode.COMMAND_TIMED_OUT


def test_shell_status_and_error_code_values_round_trip() -> None:
    for process_status in ShellProcessStatus:
        assert ShellProcessStatus(process_status.value) is process_status

    for error_code in ShellSessionErrorCode:
        assert ShellSessionErrorCode(error_code.value) is error_code


def test_shell_session_update_bounds_output_fields() -> None:
    with pytest.raises(ValidationError):
        ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.COMPLETED,
            session_id=None,
            stdout="x" * (128_000 + 1),
            stderr="",
            exit_code=0,
            stdin_available=False,
            truncated=True,
            duration_ms=1,
        )


def test_shell_exec_request_rejects_too_many_env_entries() -> None:
    env = {
        f"KEY_{index}": "value"
        for index in range(SHELL_SESSION_MAX_ENV_ENTRIES + 1)
    }

    with pytest.raises(ValidationError):
        ShellExecRequest(command="env", env=env)


def test_shell_exec_request_rejects_env_payload_over_32_kib() -> None:
    env = {"KEY": "x" * SHELL_SESSION_MAX_ENV_TOTAL_BYTES}

    with pytest.raises(ValidationError):
        ShellExecRequest(command="env", env=env)


def test_shell_exec_request_accepts_boundary_env_payload() -> None:
    env = {"KEY": "x" * (SHELL_SESSION_MAX_ENV_TOTAL_BYTES - len("KEY"))}

    request = ShellExecRequest(command="env", env=env)

    assert request.env == env


@pytest.mark.parametrize(
    "protected_name",
    [
        "WORKSPACE",
        "TASK_ID",
        "RUNTIME_PLACEMENT_MODE",
        "DROWAI_EXPECTED_RUNTIME_CONTRACT_VERSION",
        "DROWAI_TASK_ID",
        "__DROWAI_PROMPT__",
    ],
)
def test_shell_exec_request_rejects_protected_env_names(
    protected_name: str,
) -> None:
    assert protected_name in SHELL_SESSION_PROTECTED_ENV_NAMES or protected_name.startswith(
        ("DROWAI_", "__DROWAI_")
    )

    with pytest.raises(ValidationError):
        ShellExecRequest(command="env", env={protected_name: "override"})


def test_shell_exec_request_accepts_ordinary_env_additions() -> None:
    env = {"APP_MODE": "test", "PATH_SUFFIX": "/opt/example"}

    request = ShellExecRequest(command="env", env=env)

    assert request.env == env


def test_shell_write_request_models_exact_input_and_polling() -> None:
    poll = ShellWriteRequest(session_id="shs_abc123")
    interrupt = ShellWriteRequest(session_id="shs_abc123", chars="\u0003")

    assert poll.chars == ""
    assert interrupt.chars == "\u0003"
    assert poll.yield_time_ms == 10_000
    assert poll.max_output_chars == 32_000


def test_shell_session_error_codes_include_required_stable_values() -> None:
    required_codes = {
        "shell_runtime_unavailable",
        "session_limit_reached",
        "session_unavailable",
        "session_busy",
        "command_start_failed",
        "command_output_invalid",
        "command_timed_out",
        "runtime_transport_failed",
    }

    assert required_codes <= set(SHELL_SESSION_ERROR_CODES)
    assert {code.value for code in ShellSessionErrorCode} == set(
        SHELL_SESSION_ERROR_CODES
    )
