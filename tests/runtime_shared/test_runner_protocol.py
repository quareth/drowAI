"""Tests for backend-free runner control protocol DTO parsing and serialization."""

from __future__ import annotations

import pytest

from runtime_shared.runner_protocol import (
    RUNNER_TERMINAL_FRAME_MAX_BYTES,
    RUNNER_TOOL_STDIO_MAX_BYTES,
    RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE,
    RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSIONS,
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS,
    RUNNER_ARTIFACT_METADATA_MAX_BYTES,
    RunnerAckPayload,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompletePayload,
    RunnerArtifactUploadRequestPayload,
    RunnerEnvelope,
    RunnerErrorPayload,
    RunnerHeartbeatPayload,
    RunnerHelloPayload,
    RunnerRuntimeEnvironmentMetadataPayload,
    RunnerRuntimeInputResultPayload,
    RunnerRuntimeInventoryPayload,
    RunnerRuntimeLogsResultPayload,
    RunnerRuntimeMetricsResultPayload,
    RunnerRuntimeOperationPayload,
    RunnerRuntimeOperationResultPayload,
    RunnerRuntimeStartupProgressResultPayload,
    RunnerRuntimeStatusResultPayload,
    RunnerRuntimeVpnConfigResultPayload,
    RunnerRuntimeVpnRetryResultPayload,
    RunnerRuntimeVpnStatusResultPayload,
    RunnerRuntimeWorkspaceCleanupPayload,
    RunnerTerminalClosePayload,
    RunnerTerminalFramePayload,
    RunnerTerminalInputPayload,
    RunnerTerminalOpenPayload,
    RunnerTerminalResizePayload,
    RunnerTerminalResultPayload,
    RunnerToolCommandPayload,
    RunnerToolResultPayload,
    RunnerTaskStopPayload,
    RunnerMessageType,
    RunnerProtocolValidationError,
    is_runner_event_message_type,
    is_runner_executable_control_message_type,
    parse_runner_envelope,
    sanitize_log_message,
    sanitize_tool_result_payload_for_persistence,
    sanitize_tool_result_payload_for_transport,
    serialize_runner_envelope,
)
from runtime_shared.workspace_files import (
    MAX_WORKSPACE_DIRECTORIES_PER_COMMAND,
    MAX_WORKSPACE_FILES_PER_COMMAND,
)


def _base_envelope() -> dict[str, object]:
    return {
        "message_id": "msg-1",
        "type": "runner.hello",
        "schema_version": "runner_control.v1",
        "tenant_id": "tenant-1",
        "runner_id": "runner-1",
        "correlation_id": "corr-1",
        "runtime_job_id": "job-1",
        "task_id": 42,
        "created_at": "2026-05-22T10:00:00Z",
        "payload": {
            "version": "0.1.0",
            "capabilities": ["docker", "file_comm"],
            "labels": {"site": "hq"},
            "active_runtime_jobs": [
                {
                    "runtime_job_id": "job-active-1",
                    "task_id": "42",
                    "workspace_id": "task-42",
                    "status": "running",
                }
            ],
        },
    }


def _remote_runtime_operation_payload(*, operation: str, params: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "operation_id": "op-1",
        "workspace_id": "task-42",
        "runtime_image": "drowai-runtime-local:latest",
        "operation": operation,
        "params": dict(params or {}),
    }


def _remote_runtime_result_payload() -> dict[str, object]:
    return {
        "operation_id": "op-1",
        "status": "succeeded",
        "error_code": None,
        "error_message": None,
        "result": {
            "runtime_job_id": "job-1",
            "task_id": 42,
            "workspace_id": "task-42",
        },
    }


def _tooling_plane_tool_command_payload(
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": "tool-op-1",
        "workspace_id": "task-42",
        "task_runtime_job_id": "task-runtime-42",
        "runtime_image": "drowai-runtime-local:latest",
        "tool": "shell.exec",
        "command": "id",
        "cwd": "/workspace",
        "env": {},
        "command_id": "cmd-42",
        "timeout_seconds": 30.0,
        "timeout_policy": {"deadline_seconds": 30.0, "grace_seconds": 2.0},
        "route_policy": {
            "selected_lane": "container_scoped",
            "selected_authority": "container_runner_transport",
        },
        "delivery_policy": {"offline": "queue", "max_attempts": 2, "timeout_seconds": 4.0},
        "tool_call_id": "tool-call-42",
        "tool_batch_id": "tool-batch-1",
        "execution_strategy": "per_call",
        "params": {"cwd": "/workspace"},
    }
    if overrides:
        payload.update(overrides)
    return payload


def _tooling_plane_tool_result_payload(
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_id": "tool-op-1",
        "command_id": "cmd-42",
        "tool": "shell.exec",
        "status": "succeeded",
        "success": True,
        "exit_code": 0,
        "stdout": "uid=0(root) gid=0(root)",
        "stderr": "",
        "artifacts": ["artifacts/cmd-42/stdout.txt"],
        "error_code": None,
        "error_message": None,
        "result": {"duration_seconds": 0.12},
        "metadata": {"task_runtime_job_id": "task-runtime-42"},
    }
    if overrides:
        payload.update(overrides)
    return payload


def test_tool_command_payload_accepts_runtime_workspace_files() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_files": [
                {
                    "relative_path": "wordlists/ffuf.txt",
                    "content_base64": "YSJiCg==",
                    "mode": "write",
                }
            ]
        }
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerToolCommandPayload)
    assert envelope.payload.workspace_files[0].relative_path == "wordlists/ffuf.txt"
    assert envelope.payload.workspace_files[0].content_bytes() == b'a"b\n'


def test_tool_command_payload_rejects_unsafe_runtime_workspace_file_path() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_files": [
                {
                    "relative_path": "../outside.txt",
                    "content_base64": "b2sK",
                    "mode": "write",
                }
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_tool_command_payload_rejects_too_many_runtime_workspace_files() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_files": [
                {
                    "relative_path": f"inputs/file-{index}.txt",
                    "content_base64": "b2sK",
                    "mode": "write",
                }
                for index in range(MAX_WORKSPACE_FILES_PER_COMMAND + 1)
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_tool_command_payload_accepts_runtime_workspace_directories() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_directories": [
                {
                    "relative_path": "reports/wapiti",
                    "description": "wapiti report parent",
                }
            ]
        }
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerToolCommandPayload)
    assert envelope.payload.workspace_directories[0].relative_path == "reports/wapiti"


def test_tool_command_payload_rejects_unsafe_runtime_workspace_directory_path() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_directories": [
                {
                    "relative_path": "/tmp/outside",
                }
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_tool_command_payload_rejects_too_many_runtime_workspace_directories() -> None:
    payload = _base_envelope()
    payload["type"] = "tool.command"
    payload["schema_version"] = "tooling_plane.v1"
    payload["payload"] = _tooling_plane_tool_command_payload(
        overrides={
            "workspace_directories": [
                {
                    "relative_path": f"dirs/dir-{index}",
                }
                for index in range(MAX_WORKSPACE_DIRECTORIES_PER_COMMAND + 1)
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def _data_plane_artifact_manifest_payload(
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_runtime_job_id": "task-runtime-42",
        "command_id": "cmd-42",
        "workspace_id": "task-42",
        "tool_call_id": "tool-call-42",
        "tool_batch_id": "tool-batch-1",
        "artifacts": [
            {
                "artifact_client_id": "scan-report",
                "relative_path": "/workspace/artifacts/reports/nmap.xml",
                "artifact_kind": "tool_output",
                "size_bytes": 1024,
                "content_sha256": "a" * 64,
                "content_type": "application/xml",
                "is_text": True,
                "created_at": "2026-05-22T10:00:02Z",
                "metadata": {"source": "nmap"},
            }
        ],
    }
    if overrides:
        payload.update(overrides)
    return payload


def _data_plane_artifact_upload_request_payload(
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_runtime_job_id": "task-runtime-42",
        "command_id": "cmd-42",
        "workspace_id": "task-42",
        "tool_call_id": "tool-call-42",
        "tool_batch_id": "tool-batch-1",
        "uploads": [
            {
                "artifact_id": "artifact-1",
                "artifact_client_id": "scan-report",
                "object_key": "tenant-1/task-42/execution-1/artifact-1",
                "upload_url": "https://signed.example/upload?token=abc123",
                "upload_method": "put",
                "upload_headers": {"x-amz-security-token": "token-value"},
                "size_bytes": 1024,
                "content_sha256": "a" * 64,
                "content_type": "application/xml",
                "is_text": True,
            }
        ],
    }
    if overrides:
        payload.update(overrides)
    return payload


def _data_plane_artifact_upload_complete_payload(
    *,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_runtime_job_id": "task-runtime-42",
        "command_id": "cmd-42",
        "workspace_id": "task-42",
        "tool_call_id": "tool-call-42",
        "tool_batch_id": "tool-batch-1",
        "uploads": [
            {
                "artifact_id": "artifact-1",
                "artifact_client_id": "scan-report",
                "object_key": "tenant-1/task-42/execution-1/artifact-1",
                "size_bytes": 1024,
                "content_sha256": "a" * 64,
                "uploaded_at": "2026-05-22T10:00:03Z",
            }
        ],
    }
    if overrides:
        payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "missing_key",
    ["message_id", "type", "schema_version", "tenant_id", "runner_id"],
)
def test_parse_runner_envelope_rejects_missing_required_identity_fields(missing_key: str) -> None:
    payload = _base_envelope()
    payload.pop(missing_key)

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_malformed_timestamp() -> None:
    payload = _base_envelope()
    payload["created_at"] = "not-a-timestamp"

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_marks_unknown_type_as_unsupported() -> None:
    payload = _base_envelope()
    payload["type"] = "runner.future.feature"
    payload["payload"] = {"opaque": "value"}

    envelope = parse_runner_envelope(payload)

    assert envelope.message_type is RunnerMessageType.UNSUPPORTED
    assert envelope.raw_message_type == "runner.future.feature"
    assert envelope.type == "runner.future.feature"
    assert dict(envelope.payload) == {"opaque": "value"}


def test_protocol_schema_constants_include_runner_control_remote_runtime_tooling_plane_and_data_plane() -> None:
    assert RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE == (
        "runner_control.v1",
        "remote_runtime.v1",
        "tooling_plane.v1",
        "data_plane.v1",
    )
    assert RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSIONS == frozenset(
        {"runner_control.v1", "remote_runtime.v1", "tooling_plane.v1", "data_plane.v1"}
    )


@pytest.mark.parametrize(
    "remote_runtime_type",
    [
        "task.retire",
        "runtime.input",
        "runtime.startup_progress",
        "runtime.status",
        "runtime.logs",
        "runtime.metrics",
        "runtime.vpn.status",
        "runtime.vpn.retry",
        "runtime.vpn.config",
    ],
)
def test_parse_runner_envelope_returns_typed_remote_runtime_operation_payloads(remote_runtime_type: str) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_operation_payload(operation=remote_runtime_type)

    envelope = parse_runner_envelope(payload)

    assert envelope.message_type is not RunnerMessageType.UNSUPPORTED
    assert envelope.type == remote_runtime_type
    assert isinstance(envelope.payload, RunnerRuntimeOperationPayload)
    assert envelope.payload.operation_id == "op-1"


def test_parse_runner_envelope_accepts_runtime_vpn_config_request_with_secret_material() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.vpn.config"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.vpn.config",
        params={
            "vpn_config": {
                "config_data": "[Interface]\nPrivateKey=super-secret\n",
                "private_key": "embedded-private-key",
                "file_name": "task-42.ovpn",
            }
        },
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeOperationPayload)
    assert dict(envelope.payload.params)["vpn_config"]["file_name"] == "task-42.ovpn"


@pytest.mark.parametrize(
    ("remote_runtime_type", "params", "expected_class"),
    [
        (
            "terminal.open",
            {"session_name": "default", "cols": 120, "rows": 32},
            RunnerTerminalOpenPayload,
        ),
        (
            "terminal.input",
            {"session_id": "task-42-main", "data": "ls\n"},
            RunnerTerminalInputPayload,
        ),
        (
            "terminal.resize",
            {"session_id": "task-42-main", "cols": 140, "rows": 40},
            RunnerTerminalResizePayload,
        ),
        (
            "terminal.close",
            {"session_id": "task-42-main"},
            RunnerTerminalClosePayload,
        ),
    ],
)
def test_parse_runner_envelope_returns_typed_terminal_request_payloads(
    remote_runtime_type: str,
    params: dict[str, object],
    expected_class: type[object],
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_operation_payload(operation=remote_runtime_type, params=params)

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, expected_class)
    assert envelope.payload.operation_id == "op-1"


def test_parse_runner_envelope_rejects_terminal_open_without_terminal_dimensions() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.open"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="terminal.open",
        params={"session_name": "default", "rows": 24},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_terminal_input_without_data() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.input"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="terminal.input",
        params={"session_id": "task-42-main"},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_terminal_resize_without_session_id() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.resize"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="terminal.resize",
        params={"cols": 100, "rows": 30},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_terminal_close_without_session_id() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.close"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="terminal.close",
        params={},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_remote_runtime_message_family_with_runner_control_schema() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "runner_control.v1"
    payload["type"] = "runtime.started"
    payload["payload"] = _remote_runtime_result_payload()

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


@pytest.mark.parametrize(
    "remote_runtime_type",
    [
        "runtime.started",
        "runtime.paused",
        "runtime.resumed",
        "runtime.stopped",
        "runtime.retired",
        "runtime.failed",
    ],
)
def test_parse_runner_envelope_returns_typed_remote_runtime_result_payloads(remote_runtime_type: str) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_result_payload()

    envelope = parse_runner_envelope(payload)

    assert envelope.message_type is not RunnerMessageType.UNSUPPORTED
    assert envelope.type == remote_runtime_type
    assert isinstance(envelope.payload, RunnerRuntimeOperationResultPayload)
    assert envelope.payload.status == "succeeded"
    assert dict(envelope.payload.result)["workspace_id"] == "task-42"


@pytest.mark.parametrize(
    ("remote_runtime_type", "expected_class"),
    [
        ("runtime.input", RunnerRuntimeInputResultPayload),
        ("runtime.startup_progress", RunnerRuntimeStartupProgressResultPayload),
    ],
)
def test_parse_runner_envelope_accepts_runtime_input_and_startup_progress_result_payloads(
    remote_runtime_type: str,
    expected_class: type[object],
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_result_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, expected_class)
    assert envelope.payload.status == "succeeded"
    assert dict(envelope.payload.result)["workspace_id"] == "task-42"


@pytest.mark.parametrize(
    ("remote_runtime_type", "expected_class"),
    [
        ("runtime.status", RunnerRuntimeStatusResultPayload),
        ("runtime.logs", RunnerRuntimeLogsResultPayload),
        ("runtime.metrics", RunnerRuntimeMetricsResultPayload),
        ("runtime.vpn.status", RunnerRuntimeVpnStatusResultPayload),
        ("runtime.vpn.retry", RunnerRuntimeVpnRetryResultPayload),
        ("runtime.vpn.config", RunnerRuntimeVpnConfigResultPayload),
    ],
)
def test_parse_runner_envelope_returns_type_specific_remote_runtime_snapshot_result_payloads(
    remote_runtime_type: str,
    expected_class: type[object],
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_result_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, expected_class)
    assert envelope.payload.status == "succeeded"
    assert dict(envelope.payload.result)["workspace_id"] == "task-42"


def test_parse_runner_envelope_rejects_malformed_remote_runtime_snapshot_result_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.logs"
    payload["payload"] = {
        "operation_id": "op-1",
        "status": "succeeded",
        "result": "not-a-mapping",
    }

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


@pytest.mark.parametrize("remote_runtime_type", ["runtime.input", "runtime.startup_progress"])
def test_parse_runner_envelope_rejects_malformed_runtime_input_or_startup_progress_result_payload(
    remote_runtime_type: str,
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = {
        "operation_id": "op-1",
        "status": "succeeded",
        "result": "not-a-mapping",
    }

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_task_stop_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "task.stop"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="task.stop",
        params={"lifecycle_intent": "cancel"},
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerTaskStopPayload)
    assert envelope.payload.lifecycle_intent == "cancel"


def test_parse_runner_envelope_rejects_task_stop_without_lifecycle_intent() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "task.stop"
    payload["payload"] = _remote_runtime_operation_payload(operation="task.stop")

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_runtime_inventory_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.inventory"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.inventory",
        params={"scope": "task", "filters": {"kind": "container"}},
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeInventoryPayload)
    assert envelope.payload.scope == "task"
    assert dict(envelope.payload.filters) == {"kind": "container"}


def test_parse_runner_envelope_returns_typed_runtime_workspace_cleanup_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.workspace.cleanup"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.workspace.cleanup",
        params={"cleanup_scope": "workspace", "retain_outputs": True},
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeWorkspaceCleanupPayload)
    assert envelope.payload.cleanup_scope == "workspace"
    assert envelope.payload.retain_outputs is True


@pytest.mark.parametrize(
    ("message_type", "params"),
    [
        ("runtime.workspace.query", {"prefix": "reports"}),
        ("runtime.workspace.read", {"artifact_path": "reports/a.txt", "binary": True}),
    ],
)
def test_parse_runner_envelope_accepts_live_workspace_operation_payloads(
    message_type: str,
    params: dict[str, object],
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = message_type
    payload["payload"] = _remote_runtime_operation_payload(operation=message_type, params=params)

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeOperationPayload)
    assert envelope.payload.operation == message_type
    assert dict(envelope.payload.params) == params


def test_parse_runner_envelope_rejects_runtime_workspace_cleanup_with_invalid_scope() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.workspace.cleanup"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.workspace.cleanup",
        params={"cleanup_scope": "host", "retain_outputs": True},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_runtime_environment_metadata_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.environment.metadata"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.environment.metadata",
        params={"action": "write", "key": "timezone", "value": "UTC"},
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeEnvironmentMetadataPayload)
    assert envelope.payload.action == "write"
    assert envelope.payload.key == "timezone"
    assert envelope.payload.value == "UTC"


def test_parse_runner_envelope_rejects_runtime_environment_metadata_without_required_key() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "runtime.environment.metadata"
    payload["payload"] = _remote_runtime_operation_payload(
        operation="runtime.environment.metadata",
        params={"action": "read"},
    )

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


@pytest.mark.parametrize(
    "remote_runtime_type",
    [
        "runtime.inventory",
        "runtime.workspace.query",
        "runtime.workspace.read",
        "runtime.workspace.cleanup",
        "runtime.environment.metadata",
    ],
)
def test_parse_runner_envelope_accepts_runtime_compatibility_result_payloads(remote_runtime_type: str) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = remote_runtime_type
    payload["payload"] = _remote_runtime_result_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerRuntimeOperationResultPayload)
    assert dict(envelope.payload.result)["runtime_job_id"] == "job-1"


def test_parse_runner_envelope_returns_typed_terminal_result_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.result"
    payload["payload"] = {
        "operation_id": "terminal-open-1",
        "terminal_operation": "open",
        "session_id": "task-42-main",
        "status": "succeeded",
        "sequence": 0,
        "error_code": None,
        "error_message": None,
        "result": {"session_name": "default", "cols": 120, "rows": 32},
    }

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerTerminalResultPayload)
    assert envelope.payload.terminal_operation == "open"
    assert envelope.payload.session_id == "task-42-main"
    assert envelope.payload.sequence == 0


def test_parse_runner_envelope_rejects_terminal_result_without_required_terminal_fields() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.result"
    payload["payload"] = _remote_runtime_result_payload()

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_terminal_frame_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.frame"
    payload["payload"] = {
        "session_id": "task-42-main",
        "sequence": 7,
        "stream": "stdout",
        "data": "root@kali:/workspace# ",
    }

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerTerminalFramePayload)
    assert envelope.payload.session_id == "task-42-main"
    assert envelope.payload.sequence == 7
    assert envelope.payload.stream == "stdout"


def test_parse_runner_envelope_rejects_terminal_frame_with_negative_sequence() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.frame"
    payload["payload"] = {
        "session_id": "task-42-main",
        "sequence": -1,
        "stream": "stdout",
        "data": "frame",
    }

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_terminal_frame_with_oversized_data() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = "terminal.frame"
    payload["payload"] = {
        "session_id": "task-42-main",
        "sequence": 1,
        "stream": "stdout",
        "data": "x" * (RUNNER_TERMINAL_FRAME_MAX_BYTES + 1),
    }

    with pytest.raises(RunnerProtocolValidationError, match="terminal.frame data must be <="):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_tool_command_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = _tooling_plane_tool_command_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerToolCommandPayload)
    assert envelope.payload.tool == "shell.exec"
    assert envelope.payload.command == "id"
    assert envelope.payload.cwd == "/workspace"
    assert dict(envelope.payload.env) == {}
    assert envelope.payload.command_id == "cmd-42"
    assert envelope.payload.timeout_seconds == 30.0
    assert dict(envelope.payload.timeout_policy) == {"deadline_seconds": 30.0, "grace_seconds": 2.0}
    assert dict(envelope.payload.route_policy) == {
        "selected_lane": "container_scoped",
        "selected_authority": "container_runner_transport",
    }
    assert dict(envelope.payload.delivery_policy) == {
        "offline": "queue",
        "max_attempts": 2,
        "timeout_seconds": 4.0,
    }
    assert envelope.payload.tool_call_id == "tool-call-42"


def test_parse_runner_envelope_returns_typed_tool_result_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.result"
    payload["payload"] = _tooling_plane_tool_result_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerToolResultPayload)
    assert envelope.payload.command_id == "cmd-42"
    assert envelope.payload.success is True
    assert envelope.payload.exit_code == 0
    assert envelope.payload.artifacts == ("artifacts/cmd-42/stdout.txt",)
    assert dict(envelope.payload.metadata) == {"task_runtime_job_id": "task-runtime-42"}


def test_parse_runner_envelope_accepts_tool_result_with_existing_metadata_keys() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.result"
    payload["payload"] = _tooling_plane_tool_result_payload(
        overrides={
            "result": {
                "duration_seconds": 0.12,
                "session_token_seen": True,
            },
            "metadata": {
                "task_runtime_job_id": "task-runtime-42",
                "session_cookie_source": "response_header",
                "cookies_persisted": True,
            },
        }
    )

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerToolResultPayload)
    assert dict(envelope.payload.result)["session_token_seen"] is True
    assert dict(envelope.payload.metadata)["session_cookie_source"] == "response_header"
    assert dict(envelope.payload.metadata)["cookies_persisted"] is True


def test_tooling_plane_message_directionality_helpers_identify_tool_command_and_tool_result() -> None:
    assert is_runner_executable_control_message_type(RunnerMessageType.TOOL_COMMAND) is True
    assert is_runner_executable_control_message_type(RunnerMessageType.TOOL_RESULT) is False
    assert is_runner_event_message_type(RunnerMessageType.TOOL_RESULT) is True
    assert is_runner_event_message_type(RunnerMessageType.TOOL_COMMAND) is False


def test_data_plane_directionality_helpers_identify_artifact_message_types() -> None:
    assert is_runner_event_message_type(RunnerMessageType.ARTIFACT_MANIFEST) is True
    assert is_runner_event_message_type(RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE) is True
    assert is_runner_event_message_type(RunnerMessageType.ARTIFACT_UPLOAD_REQUEST) is False
    assert is_runner_executable_control_message_type(RunnerMessageType.ARTIFACT_UPLOAD_REQUEST) is True
    assert is_runner_executable_control_message_type(RunnerMessageType.ARTIFACT_MANIFEST) is False


def test_parse_runner_envelope_returns_typed_data_plane_artifact_manifest_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerArtifactManifestPayload)
    assert envelope.payload.command_id == "cmd-42"
    assert envelope.payload.task_runtime_job_id == "task-runtime-42"
    assert envelope.payload.artifacts[0].relative_path == "artifacts/reports/nmap.xml"


def test_parse_runner_envelope_returns_typed_data_plane_upload_request_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.upload.request"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_upload_request_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerArtifactUploadRequestPayload)
    assert envelope.payload.uploads[0].upload_method == "PUT"
    assert envelope.payload.uploads[0].upload_url.endswith("token=abc123")
    assert dict(envelope.payload.uploads[0].upload_headers)["x-amz-security-token"] == "token-value"


def test_parse_runner_envelope_returns_typed_data_plane_upload_complete_payload() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.upload.complete"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_upload_complete_payload()

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerArtifactUploadCompletePayload)
    assert envelope.payload.uploads[0].artifact_id == "artifact-1"


@pytest.mark.parametrize("missing_field", ["runtime_job_id", "task_id"])
def test_parse_runner_envelope_rejects_data_plane_artifact_messages_without_required_envelope_context(
    missing_field: str,
) -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload()
    payload.pop(missing_field)

    with pytest.raises(RunnerProtocolValidationError):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_data_plane_artifact_messages_with_non_data_plane_schema() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload()

    with pytest.raises(RunnerProtocolValidationError, match="Unsupported schema version"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_artifact_manifest_with_unknown_fields() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload(overrides={"tenant_id": "duplicate-tenant"})

    with pytest.raises(RunnerProtocolValidationError, match="unknown fields"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_artifact_manifest_outside_workspace() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload(
        overrides={
            "artifacts": [
                {
                    "artifact_client_id": "scan-report",
                    "relative_path": "/etc/passwd",
                    "artifact_kind": "tool_output",
                    "size_bytes": 10,
                    "content_sha256": "a" * 64,
                    "content_type": "text/plain",
                    "is_text": True,
                    "created_at": None,
                    "metadata": {},
                }
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError, match="workspace-relative"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_artifact_manifest_when_item_count_exceeds_bound() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload(
        overrides={
            "artifacts": [
                {
                    "artifact_client_id": f"artifact-{index}",
                    "relative_path": f"artifacts/{index}.txt",
                    "artifact_kind": "tool_output",
                    "size_bytes": 1,
                    "content_sha256": "a" * 64,
                    "content_type": "text/plain",
                    "is_text": True,
                    "created_at": None,
                    "metadata": {},
                }
                for index in range(RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS + 1)
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError, match="must not exceed"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_artifact_manifest_with_non_json_safe_metadata() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload(
        overrides={
            "artifacts": [
                {
                    "artifact_client_id": "scan-report",
                    "relative_path": "artifacts/report.json",
                    "artifact_kind": "tool_output",
                    "size_bytes": 10,
                    "content_sha256": "a" * 64,
                    "content_type": "application/json",
                    "is_text": True,
                    "created_at": None,
                    "metadata": {"bad": object()},
                }
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError, match="must be JSON-safe"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_artifact_manifest_with_oversized_metadata() -> None:
    payload = _base_envelope()
    payload["schema_version"] = RUNNER_PROTOCOL_DATA_PLANE_VERSION
    payload["type"] = "artifact.manifest"
    payload["runtime_job_id"] = "tool-runtime-42"
    payload["task_id"] = 42
    payload["payload"] = _data_plane_artifact_manifest_payload(
        overrides={
            "artifacts": [
                {
                    "artifact_client_id": "scan-report",
                    "relative_path": "artifacts/report.txt",
                    "artifact_kind": "tool_output",
                    "size_bytes": 10,
                    "content_sha256": "a" * 64,
                    "content_type": "text/plain",
                    "is_text": True,
                    "created_at": None,
                    "metadata": {"oversized": "x" * (RUNNER_ARTIFACT_METADATA_MAX_BYTES + 1)},
                }
            ]
        }
    )

    with pytest.raises(RunnerProtocolValidationError, match="must be <="):
        parse_runner_envelope(payload)

@pytest.mark.parametrize("tooling_plane_type", ["tool.command", "tool.result"])
def test_parse_runner_envelope_rejects_tooling_plane_tool_messages_with_non_tooling_plane_schema(tooling_plane_type: str) -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    payload["type"] = tooling_plane_type
    payload["payload"] = (
        _tooling_plane_tool_command_payload() if tooling_plane_type == "tool.command" else _tooling_plane_tool_result_payload()
    )

    with pytest.raises(RunnerProtocolValidationError, match="Unsupported schema version"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_command_without_tool() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = _tooling_plane_tool_command_payload()
    del payload["payload"]["tool"]

    with pytest.raises(RunnerProtocolValidationError, match="tool is required"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_command_without_command_id() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = _tooling_plane_tool_command_payload()
    del payload["payload"]["command_id"]

    with pytest.raises(RunnerProtocolValidationError, match="command_id is required"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_command_with_non_positive_timeout() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = _tooling_plane_tool_command_payload(overrides={"timeout_seconds": 0})

    with pytest.raises(RunnerProtocolValidationError, match="timeout_seconds is required and must be a positive number"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_command_with_secret_reference_fields() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = _tooling_plane_tool_command_payload(overrides={"secret_refs": ["vault://tenant/secret"]})

    with pytest.raises(RunnerProtocolValidationError, match="unknown fields: secret_refs"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_result_with_invalid_status() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.result"
    payload["payload"] = _tooling_plane_tool_result_payload(overrides={"status": "queued"})

    with pytest.raises(RunnerProtocolValidationError, match="status is required and must be one of"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_rejects_tool_result_with_oversized_stdout() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.result"
    payload["payload"] = _tooling_plane_tool_result_payload(
        overrides={"stdout": "x" * (RUNNER_TOOL_STDIO_MAX_BYTES + 1)}
    )

    with pytest.raises(RunnerProtocolValidationError, match="stdout must be <="):
        parse_runner_envelope(payload)


def test_sanitize_tool_result_payload_for_transport_redacts_metadata_and_caps_stdio() -> None:
    payload = _tooling_plane_tool_result_payload(
        overrides={
            "stdout": "a" * (RUNNER_TOOL_STDIO_MAX_BYTES + 32),
            "stderr": "authorization: Bearer TOP_SECRET_TOKEN\n" + ("b" * (RUNNER_TOOL_STDIO_MAX_BYTES + 32)),
            "metadata": {
                "task_runtime_job_id": "task-runtime-42",
                "api_key": "sensitive-value",
                "nested": {
                    "set_cookie": "session=abc123",
                    "safe": "ok",
                },
            },
        }
    )

    sanitized = sanitize_tool_result_payload_for_transport(payload)

    assert len(str(sanitized["stdout"]).encode("utf-8")) <= RUNNER_TOOL_STDIO_MAX_BYTES
    assert len(str(sanitized["stderr"]).encode("utf-8")) <= RUNNER_TOOL_STDIO_MAX_BYTES
    assert "TOP_SECRET_TOKEN" not in str(sanitized["stderr"])
    assert "<DURABLE_SECRET_MASK:token>" in str(sanitized["stderr"])
    assert sanitized["metadata"]["api_key"] == "<DURABLE_SECRET_MASK:secret>"
    assert sanitized["metadata"]["nested"]["set_cookie"] == "<DURABLE_SECRET_MASK:secret>"
    assert sanitized["metadata"]["nested"]["safe"] == "ok"
    assert sanitized["metadata"]["runner_transport_stdout_truncated"] is True
    assert sanitized["metadata"]["runner_transport_stdout_original_bytes"] > RUNNER_TOOL_STDIO_MAX_BYTES
    assert sanitized["metadata"]["runner_transport_stderr_truncated"] is True
    assert sanitized["metadata"]["runner_transport_stderr_original_bytes"] > RUNNER_TOOL_STDIO_MAX_BYTES


def test_sanitize_tool_result_payload_for_persistence_masks_bare_tshark_proof_excerpt() -> None:
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    payload = _tooling_plane_tool_result_payload(
        overrides={
            "metadata": {
                "secret_exposure": [
                    {
                        "field": "ftp.request.command_parameter",
                        "kind": "protocol_auth_argument",
                        "proof_mode": "proof_excerpt",
                        "proof_excerpt": raw_secret,
                    }
                ]
            }
        }
    )

    sanitized = sanitize_tool_result_payload_for_persistence(payload)

    assert raw_secret not in str(sanitized)
    assert sanitized["metadata"]["secret_exposure"][0]["proof_excerpt"].startswith(
        "<DURABLE_SECRET_MASK:"
    )


def test_sanitize_log_message_redacts_and_truncates() -> None:
    message = sanitize_log_message(
        "authorization: Bearer TOP_SECRET_TOKEN\n" + ("x" * 300),
        max_chars=80,
    )
    assert "TOP_SECRET_TOKEN" not in message
    assert "<redacted>" in message
    assert len(message) <= 83


def test_serialize_runner_envelope_round_trips_tool_payloads() -> None:
    command_envelope = RunnerEnvelope(
        message_id="msg-tool-command",
        message_type=RunnerMessageType.TOOL_COMMAND,
        schema_version="tooling_plane.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-tool-command",
        runtime_job_id="tool-command-runtime-job-1",
        task_id=42,
        created_at="2026-05-22T10:00:00Z",
        payload=RunnerToolCommandPayload(
            operation_id="tool-op-1",
            workspace_id="task-42",
            task_runtime_job_id="task-runtime-42",
            runtime_image="drowai-runtime-local:latest",
            tool="shell.exec",
            command="id",
            cwd="/workspace",
            env={},
            command_id="cmd-42",
            timeout_seconds=45.0,
            timeout_policy={"deadline_seconds": 45.0, "grace_seconds": 3.0},
            route_policy={
                "selected_lane": "container_scoped",
                "selected_authority": "container_runner_transport",
            },
            delivery_policy={"offline": "queue", "max_attempts": 2, "timeout_seconds": 4.0},
            tool_call_id="tool-call-42",
            tool_batch_id="tool-batch-1",
            execution_strategy="per_call",
            params={"cwd": "/workspace"},
        ),
        raw_message_type="tool.command",
    )
    result_envelope = RunnerEnvelope(
        message_id="msg-tool-result",
        message_type=RunnerMessageType.TOOL_RESULT,
        schema_version="tooling_plane.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-tool-result",
        runtime_job_id="tool-command-runtime-job-1",
        task_id=42,
        created_at="2026-05-22T10:00:00Z",
        payload=RunnerToolResultPayload(
            operation_id="tool-op-1",
            command_id="cmd-42",
            tool="shell.exec",
            status="succeeded",
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            artifacts=("artifacts/cmd-42/stdout.txt",),
            error_code=None,
            error_message=None,
            result={"duration_seconds": 0.12},
            metadata={"task_runtime_job_id": "task-runtime-42"},
        ),
        raw_message_type="tool.result",
    )

    serialized_command = serialize_runner_envelope(command_envelope)
    serialized_result = serialize_runner_envelope(result_envelope)
    parsed_command = parse_runner_envelope(serialized_command)
    parsed_result = parse_runner_envelope(serialized_result)

    assert isinstance(parsed_command.payload, RunnerToolCommandPayload)
    assert parsed_command.payload.command_id == "cmd-42"
    assert parsed_command.payload.tool_call_id == "tool-call-42"
    assert parsed_command.payload.timeout_seconds == 45.0
    assert dict(parsed_command.payload.timeout_policy) == {
        "deadline_seconds": 45.0,
        "grace_seconds": 3.0,
    }
    assert dict(parsed_command.payload.route_policy) == {
        "selected_lane": "container_scoped",
        "selected_authority": "container_runner_transport",
    }
    assert dict(parsed_command.payload.delivery_policy) == {
        "offline": "queue",
        "max_attempts": 2,
        "timeout_seconds": 4.0,
    }
    assert isinstance(parsed_result.payload, RunnerToolResultPayload)
    assert parsed_result.payload.success is True
    assert parsed_result.payload.exit_code == 0
    assert parsed_result.payload.artifacts == ("artifacts/cmd-42/stdout.txt",)
    assert dict(parsed_result.payload.metadata) == {"task_runtime_job_id": "task-runtime-42"}


def test_serialize_runner_envelope_accepts_tool_result_with_cookie_like_metadata_keys() -> None:
    envelope = RunnerEnvelope(
        message_id="msg-tool-result-cookies",
        message_type=RunnerMessageType.TOOL_RESULT,
        schema_version="tooling_plane.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-tool-result",
        runtime_job_id="tool-command-runtime-job-1",
        task_id=42,
        created_at="2026-05-22T10:00:00Z",
        payload=RunnerToolResultPayload(
            operation_id="tool-op-1",
            command_id="cmd-42",
            tool="shell.exec",
            status="succeeded",
            success=True,
            exit_code=0,
            stdout="ok",
            stderr="",
            artifacts=("artifacts/cmd-42/stdout.txt",),
            error_code=None,
            error_message=None,
            result={"session_token_seen": True},
            metadata={
                "task_runtime_job_id": "task-runtime-42",
                "session_cookie_source": "response_header",
                "cookies_persisted": True,
            },
        ),
        raw_message_type="tool.result",
    )

    serialized = serialize_runner_envelope(envelope)
    parsed = parse_runner_envelope(serialized)

    assert isinstance(parsed.payload, RunnerToolResultPayload)
    assert dict(parsed.payload.result)["session_token_seen"] is True
    assert dict(parsed.payload.metadata)["session_cookie_source"] == "response_header"
    assert dict(parsed.payload.metadata)["cookies_persisted"] is True


def test_parse_runner_envelope_returns_typed_hello_payload() -> None:
    envelope = parse_runner_envelope(_base_envelope())

    assert isinstance(envelope.payload, RunnerHelloPayload)
    assert envelope.payload.capabilities == ("docker", "file_comm")
    assert dict(envelope.payload.labels) == {"site": "hq"}


def test_parse_runner_envelope_returns_typed_heartbeat_payload() -> None:
    payload = _base_envelope()
    payload["type"] = "runner.heartbeat"
    payload["payload"] = {
        "capacity": {
            "active_tasks": 2,
            "max_active_tasks": 5,
            "available_tasks": 3,
            "max_parallel_commands_per_task": 4,
            "docker_available": True,
            "runtime_image": "drowai-runtime-local:latest",
            "runtime_image_available": True,
            "version": "0.1.0",
            "capabilities": ["docker", "file_comm"],
            "labels": {"site": "hq"},
            "active_runtime_jobs": [
                {
                    "runtime_job_id": "job-active-1",
                    "task_id": "42",
                    "workspace_id": "task-42",
                    "status": "running",
                }
            ],
        },
    }

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerHeartbeatPayload)
    assert envelope.payload.capacity.active_tasks == 2
    assert envelope.payload.capacity.max_active_tasks == 5
    assert envelope.payload.capacity.available_tasks == 3
    assert envelope.payload.capacity.max_parallel_commands_per_task == 4
    assert envelope.payload.capacity.runtime_image == "drowai-runtime-local:latest"
    assert envelope.payload.capacity.active_runtime_jobs[0].runtime_job_id == "job-active-1"
    assert envelope.payload.capacity.active_runtime_jobs[0].task_id == "42"


def test_parse_runner_envelope_rejects_oversized_active_runtime_jobs() -> None:
    payload = _base_envelope()
    payload["type"] = "runner.heartbeat"
    payload["payload"] = {
        "capacity": {
            "active_tasks": 0,
            "max_active_tasks": 5,
            "available_tasks": 5,
            "max_parallel_commands_per_task": 4,
            "docker_available": True,
            "runtime_image": "drowai-runtime-local:latest",
            "runtime_image_available": True,
            "version": "0.1.0",
            "capabilities": ["docker", "file_comm"],
            "labels": {"site": "hq"},
            "active_runtime_jobs": [
                {
                    "runtime_job_id": f"job-{index}",
                    "task_id": str(index),
                    "workspace_id": f"task-{index}",
                    "status": "running",
                }
                for index in range(129)
            ],
        },
    }

    with pytest.raises(RunnerProtocolValidationError, match="active_runtime_jobs length"):
        parse_runner_envelope(payload)


def test_parse_runner_envelope_returns_typed_ack_payload() -> None:
    payload = _base_envelope()
    payload["type"] = "runner.ack"
    payload["payload"] = {
        "acked_message_id": "msg-upstream",
        "status": "accepted",
        "error_code": None,
    }

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerAckPayload)
    assert envelope.payload.acked_message_id == "msg-upstream"
    assert envelope.payload.status == "accepted"


def test_parse_runner_envelope_returns_typed_error_payload() -> None:
    payload = _base_envelope()
    payload["type"] = "error"
    payload["payload"] = {
        "error_code": "RUNNER_PROTOCOL_UNSUPPORTED",
        "message": "Unsupported message type.",
        "retryable": False,
    }

    envelope = parse_runner_envelope(payload)

    assert isinstance(envelope.payload, RunnerErrorPayload)
    assert envelope.payload.error_code == "RUNNER_PROTOCOL_UNSUPPORTED"


def test_serialize_runner_envelope_preserves_stable_wire_field_names() -> None:
    envelope = RunnerEnvelope(
        message_id="msg-9",
        message_type=RunnerMessageType.RUNNER_ACK,
        schema_version="runner_control.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-9",
        runtime_job_id="job-9",
        task_id=7,
        created_at="2026-05-22T10:00:00Z",
        payload=RunnerAckPayload(
            acked_message_id="msg-1",
            status="accepted",
            error_code=None,
        ),
        raw_message_type="runner.ack",
    )

    serialized = serialize_runner_envelope(envelope)

    assert set(serialized) == {
        "message_id",
        "type",
        "schema_version",
        "tenant_id",
        "runner_id",
        "correlation_id",
        "runtime_job_id",
        "task_id",
        "created_at",
        "payload",
    }
    assert serialized["type"] == "runner.ack"


def test_serialize_runner_envelope_rejects_remote_runtime_secret_like_payload() -> None:
    envelope = RunnerEnvelope(
        message_id="msg-remote-runtime-secret",
        message_type=RunnerMessageType.RUNTIME_STATUS,
        schema_version="remote_runtime.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-9",
        runtime_job_id="job-9",
        task_id=7,
        created_at="2026-05-22T10:00:00Z",
        payload={
            "operation_id": "op-9",
            "workspace_id": "task-7",
            "runtime_image": "drowai-runtime-local:latest",
            "operation": "runtime.status",
            "params": {"secret_token": "top-secret"},
        },
        raw_message_type="runtime.status",
    )

    with pytest.raises(RunnerProtocolValidationError):
        serialize_runner_envelope(envelope)


def test_serialize_runner_envelope_rejects_remote_runtime_non_json_safe_payload() -> None:
    envelope = RunnerEnvelope(
        message_id="msg-remote-runtime-json",
        message_type=RunnerMessageType.RUNTIME_STATUS,
        schema_version="remote_runtime.v1",
        tenant_id="tenant-1",
        runner_id="runner-1",
        correlation_id="corr-9",
        runtime_job_id="job-9",
        task_id=7,
        created_at="2026-05-22T10:00:00Z",
        payload={
            "operation_id": "op-9",
            "workspace_id": "task-7",
            "runtime_image": "drowai-runtime-local:latest",
            "operation": "runtime.status",
            "params": {"values": [object()]},
        },
        raw_message_type="runtime.status",
    )

    with pytest.raises(RunnerProtocolValidationError):
        serialize_runner_envelope(envelope)
