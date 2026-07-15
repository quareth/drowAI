"""Tests for runner-side runner control plane ack classification behavior."""

from __future__ import annotations

import json

import pytest

from drowai_runner.protocol_handler import (
    RunnerTaskRuntimeBinding,
    RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE,
    RUNNER_DEFERRED_RUNTIME_ERROR_CODE,
    RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
    build_remote_runtime_envelope,
    build_data_plane_artifact_manifest_envelope,
    build_data_plane_artifact_upload_complete_envelope,
    classify_runner_control_inbound_ack,
    is_runner_event_message,
    is_runner_executable_control_message,
    parse_inbound_envelope,
    should_ack_inbound,
    validate_data_plane_artifact_upload_request_binding,
    validate_tooling_plane_tool_command_binding,
)
from runtime_shared.runner_protocol import (
    RunnerArtifactManifestItem,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompleteItem,
    RunnerArtifactUploadCompletePayload,
    RunnerMessageType,
    RunnerProtocolValidationError,
    RunnerRuntimeOperationPayload,
    RunnerRuntimeOperationResultPayload,
)


def _inbound_message(
    *,
    tenant_id: int = 7,
    runner_id: str = "runner-7",
    message_type: str = "runner.assignment.probe",
    message_id: str = "msg-1",
    runtime_job_id: str | None = "ba126678-37e8-4d96-aeae-274bf25a5ac6",
    task_id: int | None = 91,
) -> str:
    return json.dumps(
        {
            "message_id": message_id,
            "type": message_type,
            "schema_version": "runner_control.v1",
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": "corr-1",
            "runtime_job_id": runtime_job_id,
            "task_id": task_id,
            "created_at": "2026-05-23T12:00:00+00:00",
            "payload": {"command": "noop"},
        }
    )


def _tooling_plane_tool_command_message(
    *,
    message_type: str = "tool.command",
    tenant_id: int = 7,
    runner_id: str = "runner-7",
    runtime_job_id: str | None = "tool-command-job-91",
    task_runtime_job_id: str = "task-runtime-91",
    workspace_id: str = "task-91",
    params: dict[str, object] | None = None,
) -> str:
    return json.dumps(
        {
            "message_id": "msg-tooling-plane-tool-command",
            "type": message_type,
            "schema_version": "tooling_plane.v1",
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": "corr-tooling-plane-tool-command",
            "runtime_job_id": runtime_job_id,
            "task_id": 91,
            "created_at": "2026-05-24T10:00:00+00:00",
            "payload": {
                "operation_id": "tool-op-91",
                "workspace_id": workspace_id,
                "task_runtime_job_id": task_runtime_job_id,
                "runtime_image": "drowai-runtime-local:latest",
                "tool": "shell.exec",
                "command": "id",
                "cwd": "/workspace",
                "env": {},
                "command_id": "cmd-91",
                "timeout_seconds": 30.0,
                "timeout_policy": {"deadline_seconds": 30.0, "grace_seconds": 2.0},
                "route_policy": {
                    "selected_lane": "container_scoped",
                    "selected_authority": "container_runner_transport",
                },
                "delivery_policy": {"offline": "queue", "max_attempts": 2, "timeout_seconds": 4.0},
                "tool_call_id": "tool-call-91",
                "tool_batch_id": "tool-batch-91",
                "execution_strategy": "per_call",
                "params": dict(params or {"cwd": "/workspace"}),
            },
        }
    )


def _tooling_plane_tool_result_message(
    *,
    tenant_id: int = 7,
    runner_id: str = "runner-7",
) -> str:
    return json.dumps(
        {
            "message_id": "msg-tooling-plane-tool-result",
            "type": "tool.result",
            "schema_version": "tooling_plane.v1",
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": "corr-tooling-plane-tool-result",
            "runtime_job_id": "tool-command-job-91",
            "task_id": 91,
            "created_at": "2026-05-24T10:00:05+00:00",
            "payload": {
                "operation_id": "tool-op-91",
                "command_id": "cmd-91",
                "tool": "shell.exec",
                "status": "succeeded",
                "success": True,
                "exit_code": 0,
                "stdout": "ok",
                "stderr": "",
                "artifacts": ["artifacts/cmd-91/stdout.txt"],
                "error_code": None,
                "error_message": None,
                "result": {"duration_seconds": 0.1},
                "metadata": {"task_runtime_job_id": "task-runtime-91", "workspace_id": "task-91"},
            },
        }
    )


def _data_plane_artifact_upload_request_message(
    *,
    tenant_id: int = 7,
    runner_id: str = "runner-7",
    runtime_job_id: str | None = "tool-command-job-91",
) -> str:
    return json.dumps(
        {
            "message_id": "msg-data-plane-upload-request",
            "type": "artifact.upload.request",
            "schema_version": "data_plane.v1",
            "tenant_id": str(tenant_id),
            "runner_id": runner_id,
            "correlation_id": "corr-data-plane-upload-request",
            "runtime_job_id": runtime_job_id,
            "task_id": 91,
            "created_at": "2026-05-25T10:00:00+00:00",
            "payload": {
                "task_runtime_job_id": "task-runtime-91",
                "command_id": "cmd-91",
                "workspace_id": "task-91",
                "tool_call_id": "tool-call-91",
                "tool_batch_id": "tool-batch-91",
                "uploads": [
                    {
                        "artifact_id": "11111111-1111-1111-1111-111111111111",
                        "artifact_client_id": "artifact-1",
                        "object_key": "tenant-7/task-91/artifact-1",
                        "upload_url": "https://object.example.test/upload",
                        "upload_method": "PUT",
                        "upload_headers": {"x-test-signed": "1"},
                        "size_bytes": 3,
                        "content_sha256": "a" * 64,
                        "content_type": "text/plain",
                        "is_text": True,
                    }
                ],
            },
        }
    )


def test_assignment_probe_wire_type_is_ackable() -> None:
    envelope = parse_inbound_envelope(_inbound_message(message_type="runner.assignment.probe"))

    assert should_ack_inbound(envelope) is True
    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
    )
    assert decision.should_ack is True
    assert decision.status == "accepted"
    assert decision.error_code is None


def test_task_runtime_message_not_assigned_to_runner_is_rejected() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            runner_id="runner-8",
            message_id="msg-task-not-assigned",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
    )
    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE


def test_unsupported_runner_control_runtime_command_returns_stable_error_code() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            runner_id="runner-7",
            message_id="msg-task-deferred",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": 91},
    )
    assert decision.should_ack is True
    assert decision.status == "failed"
    assert decision.error_code == RUNNER_DEFERRED_RUNTIME_ERROR_CODE


def test_remote_runtime_runtime_command_is_still_rejected_in_runner_control_ack_classifier() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="runtime.status",
            runner_id="runner-7",
            message_id="msg-runtime-status-deferred",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": 91},
    )
    assert decision.should_ack is True
    assert decision.status == "failed"
    assert decision.error_code == RUNNER_DEFERRED_RUNTIME_ERROR_CODE


def test_runtime_command_missing_runtime_job_id_is_rejected_with_stable_error() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            runtime_job_id=None,
            message_id="msg-task-missing-runtime-job",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": 91},
    )
    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE


def test_runtime_command_unknown_runtime_job_id_is_rejected() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            runtime_job_id="c5970d91-c842-4fcf-af7e-c7de83cf7050",
            message_id="msg-task-unknown-runtime-job",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": 91},
    )
    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE


def test_runtime_command_task_mismatch_is_rejected() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            task_id=92,
            message_id="msg-task-mismatch-assignment",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": 91},
    )
    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE


def test_runtime_command_after_taskless_probe_is_rejected() -> None:
    envelope = parse_inbound_envelope(
        _inbound_message(
            message_type="task.start",
            task_id=91,
            message_id="msg-task-after-taskless-probe",
        )
    )

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        assigned_runtime_jobs={"ba126678-37e8-4d96-aeae-274bf25a5ac6": None},
    )
    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE


def test_build_remote_runtime_runtime_envelope_defaults_to_remote_runtime_schema_version() -> None:
    envelope = build_remote_runtime_envelope(
        tenant_id=7,
        runner_id="runner-7",
        message_type=RunnerMessageType.RUNTIME_STATUS,
        payload=RunnerRuntimeOperationPayload(
            operation_id="op-1",
            workspace_id="task-91",
            runtime_image="drowai-runtime-local:latest",
            operation="runtime.status",
            params={},
        ),
        correlation_id="corr-1",
        runtime_job_id="job-1",
        task_id=91,
    )

    assert envelope.schema_version == "remote_runtime.v1"
    assert envelope.message_type is RunnerMessageType.RUNTIME_STATUS
    assert envelope.runtime_job_id == "job-1"
    assert envelope.task_id == 91


def test_build_remote_runtime_runtime_envelope_rejects_runner_control_schema_override() -> None:
    with pytest.raises(RunnerProtocolValidationError):
        build_remote_runtime_envelope(
            tenant_id=7,
            runner_id="runner-7",
            message_type=RunnerMessageType.RUNTIME_STATUS,
            payload=RunnerRuntimeOperationPayload(
                operation_id="op-1",
                workspace_id="task-91",
                runtime_image="drowai-runtime-local:latest",
                operation="runtime.status",
                params={},
            ),
            protocol_version="runner_control.v1",
        )


def test_build_remote_runtime_runtime_workspace_write_result_envelope_round_trips() -> None:
    payload = RunnerRuntimeOperationResultPayload(
        operation_id="op-write-1",
        status="succeeded",
        error_code=None,
        error_message=None,
        result={
            "runtime_job_id": "job-write-1",
            "task_id": 77,
            "workspace_id": "task-77",
            "path": "artifacts/out.txt",
            "size": 3,
        },
    )
    envelope = build_remote_runtime_envelope(
        tenant_id=3,
        runner_id="runner-3",
        message_type=RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        payload=payload,
        correlation_id="corr-write-1",
        runtime_job_id="job-write-1",
        task_id=77,
    )

    assert envelope.schema_version == "remote_runtime.v1"
    assert envelope.message_type is RunnerMessageType.RUNTIME_WORKSPACE_WRITE
    assert envelope.runtime_job_id == "job-write-1"
    assert envelope.task_id == 77

    wire = json.dumps(envelope.to_dict())
    parsed = parse_inbound_envelope(wire)

    assert parsed.message_type is RunnerMessageType.RUNTIME_WORKSPACE_WRITE
    assert parsed.schema_version == "remote_runtime.v1"
    assert parsed.runtime_job_id == "job-write-1"
    assert parsed.task_id == 77
    assert isinstance(parsed.payload, RunnerRuntimeOperationResultPayload)
    assert parsed.payload.operation_id == "op-write-1"
    assert parsed.payload.status == "succeeded"
    assert parsed.payload.error_code is None
    assert parsed.payload.result["path"] == "artifacts/out.txt"


def test_build_remote_runtime_runtime_envelope_rejects_non_remote_runtime_message_types() -> None:
    with pytest.raises(RunnerProtocolValidationError):
        build_remote_runtime_envelope(
            tenant_id=7,
            runner_id="runner-7",
            message_type=RunnerMessageType.RUNNER_HELLO,
            payload={"version": "0.1.0"},
        )


def test_build_remote_runtime_runtime_envelope_rejects_secret_like_payload_on_serialization() -> None:
    envelope = build_remote_runtime_envelope(
        tenant_id=7,
        runner_id="runner-7",
        message_type=RunnerMessageType.RUNTIME_STATUS,
        payload={
            "operation_id": "op-1",
            "workspace_id": "task-91",
            "runtime_image": "drowai-runtime-local:latest",
            "operation": "runtime.status",
            "params": {"api_key": "super-secret"},
        },
    )

    with pytest.raises(RunnerProtocolValidationError):
        envelope.to_dict()


def test_tooling_plane_tool_command_is_classified_as_runner_executable_control_message() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message(message_type="tool.command"))
    tool_result = parse_inbound_envelope(_tooling_plane_tool_result_message())

    assert is_runner_executable_control_message(envelope) is True
    assert is_runner_event_message(envelope) is False
    assert is_runner_executable_control_message(tool_result) is False
    assert is_runner_event_message(tool_result) is True


def test_tooling_plane_tool_command_ack_decision_accepts_valid_task_runtime_binding() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message())

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="7",
            task_id="91",
            workspace_id="task-91",
        ),
    )

    assert decision.should_ack is True
    assert decision.status == "accepted"
    assert decision.error_code is None


def test_tooling_plane_tool_command_ack_decision_rejects_invalid_binding_without_deferred_code() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message(workspace_id="task-92"))

    decision = classify_runner_control_inbound_ack(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="7",
            task_id="91",
            workspace_id="task-91",
        ),
    )

    assert decision.should_ack is True
    assert decision.status == "rejected"
    assert decision.error_code == RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE
    assert decision.error_code != RUNNER_DEFERRED_RUNTIME_ERROR_CODE


def test_validate_tooling_plane_tool_command_binding_accepts_known_task_runtime_binding() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message())
    binding = validate_tooling_plane_tool_command_binding(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
        task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="7",
            task_id="91",
            workspace_id="task-91",
        ),
    )

    assert binding.runtime_job_id == "task-runtime-91"
    assert binding.tenant_id == "7"
    assert binding.task_id == "91"
    assert binding.workspace_id == "task-91"


def test_validate_tooling_plane_tool_command_binding_rejects_unknown_task_runtime_job_id() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message(task_runtime_job_id="task-runtime-missing"))

    with pytest.raises(RunnerProtocolValidationError, match="missing or not present"):
        validate_tooling_plane_tool_command_binding(
            envelope,
            expected_tenant_id=7,
            expected_runner_id="runner-7",
            task_runtime_binding_lookup=lambda _runtime_job_id: None,
        )


def test_validate_tooling_plane_tool_command_binding_rejects_workspace_mismatch() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message(workspace_id="task-92"))

    with pytest.raises(RunnerProtocolValidationError, match="workspace_id does not match"):
        validate_tooling_plane_tool_command_binding(
            envelope,
            expected_tenant_id=7,
            expected_runner_id="runner-7",
            task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
                runtime_job_id=runtime_job_id,
                tenant_id="7",
                task_id="91",
                workspace_id="task-91",
            ),
        )


def test_validate_tooling_plane_tool_command_binding_rejects_runtime_identity_override_params() -> None:
    envelope = parse_inbound_envelope(
        _tooling_plane_tool_command_message(params={"cwd": "/workspace", "task_runtime_job_id": "override"})
    )

    with pytest.raises(RunnerProtocolValidationError, match="must not override runtime identity"):
        validate_tooling_plane_tool_command_binding(
            envelope,
            expected_tenant_id=7,
            expected_runner_id="runner-7",
            task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
                runtime_job_id=runtime_job_id,
                tenant_id="7",
                task_id="91",
                workspace_id="task-91",
            ),
        )


def test_validate_tooling_plane_tool_command_binding_rejects_missing_envelope_runtime_job_id() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message(runtime_job_id=None))

    with pytest.raises(RunnerProtocolValidationError, match="runtime_job_id is required"):
        validate_tooling_plane_tool_command_binding(
            envelope,
            expected_tenant_id=7,
            expected_runner_id="runner-7",
            task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
                runtime_job_id=runtime_job_id,
                tenant_id="7",
                task_id="91",
                workspace_id="task-91",
            ),
        )


def test_validate_tooling_plane_tool_command_binding_rejects_task_id_mismatch() -> None:
    envelope = parse_inbound_envelope(_tooling_plane_tool_command_message())

    with pytest.raises(RunnerProtocolValidationError, match="task_id does not match"):
        validate_tooling_plane_tool_command_binding(
            envelope,
            expected_tenant_id=7,
            expected_runner_id="runner-7",
            task_runtime_binding_lookup=lambda runtime_job_id: RunnerTaskRuntimeBinding(
                runtime_job_id=runtime_job_id,
                tenant_id="7",
                task_id="92",
                workspace_id="task-91",
            ),
        )


def test_build_data_plane_manifest_and_upload_complete_envelopes_use_data_plane_schema() -> None:
    manifest_payload = RunnerArtifactManifestPayload(
        task_runtime_job_id="task-runtime-91",
        command_id="cmd-91",
        workspace_id="task-91",
        tool_call_id="tool-call-91",
        tool_batch_id="tool-batch-91",
        artifacts=(
            RunnerArtifactManifestItem(
                artifact_client_id="artifact-1",
                relative_path="artifacts/cmd-91/stdout.txt",
                artifact_kind="file",
                size_bytes=3,
                content_sha256="a" * 64,
                content_type="text/plain",
                is_text=True,
                created_at=None,
                metadata={},
            ),
        ),
    )
    manifest = build_data_plane_artifact_manifest_envelope(
        tenant_id=7,
        runner_id="runner-7",
        payload=manifest_payload,
        runtime_job_id="tool-command-job-91",
        task_id=91,
    )
    assert manifest.schema_version == "data_plane.v1"
    assert manifest.message_type is RunnerMessageType.ARTIFACT_MANIFEST

    upload_complete = build_data_plane_artifact_upload_complete_envelope(
        tenant_id=7,
        runner_id="runner-7",
        payload=RunnerArtifactUploadCompletePayload(
            task_runtime_job_id="task-runtime-91",
            command_id="cmd-91",
            workspace_id="task-91",
            tool_call_id="tool-call-91",
            tool_batch_id="tool-batch-91",
            uploads=(
                RunnerArtifactUploadCompleteItem(
                    artifact_id="11111111-1111-1111-1111-111111111111",
                    artifact_client_id="artifact-1",
                    object_key="tenant-7/task-91/artifact-1",
                    size_bytes=3,
                    content_sha256="a" * 64,
                    uploaded_at="2026-05-25T10:00:01+00:00",
                ),
            ),
        ),
        runtime_job_id="tool-command-job-91",
        task_id=91,
    )
    assert upload_complete.schema_version == "data_plane.v1"
    assert upload_complete.message_type is RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE


def test_validate_data_plane_artifact_upload_request_binding_accepts_matching_identity() -> None:
    envelope = parse_inbound_envelope(_data_plane_artifact_upload_request_message())
    payload = validate_data_plane_artifact_upload_request_binding(
        envelope,
        expected_tenant_id=7,
        expected_runner_id="runner-7",
    )
    assert payload.command_id == "cmd-91"
    assert payload.workspace_id == "task-91"
    assert payload.uploads[0].artifact_client_id == "artifact-1"


def test_validate_data_plane_artifact_upload_request_binding_rejects_missing_runtime_job_id() -> None:
    with pytest.raises(RunnerProtocolValidationError, match="runtime_job_id is required"):
        parse_inbound_envelope(_data_plane_artifact_upload_request_message(runtime_job_id=None))
