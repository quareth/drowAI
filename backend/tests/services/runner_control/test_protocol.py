"""Tests for backend runner-control protocol validation service."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Task, User
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.runner_control.channel.binding_queries import _lookup_runtime_job_binding
from backend.services.runner_control.protocol import (
    RunnerChannelIdentity,
    RunnerProtocolValidationError,
    RunnerProtocolValidator,
    RunnerRuntimeJobBinding,
)
from runtime_shared.runner_protocol import RunnerProtocolValidationError as SharedRunnerProtocolValidationError
from runtime_shared.runner_protocol import parse_runner_envelope


def _base_envelope() -> dict[str, object]:
    return {
        "message_id": "msg-1",
        "type": "runner.heartbeat",
        "schema_version": "runner_control.v1",
        "tenant_id": "tenant-1",
        "runner_id": "runner-1",
        "correlation_id": "corr-1",
        "runtime_job_id": "job-1",
        "task_id": 42,
        "created_at": "2026-05-22T10:00:00Z",
        "payload": {
            "capacity": {
                "active_tasks": 1,
                "max_active_tasks": 2,
                "available_tasks": 1,
                "max_parallel_commands_per_task": 4,
                "docker_available": True,
                "runtime_image": "drowai-runtime-local:latest",
                "runtime_image_available": True,
                "version": "1.0.0",
                "capabilities": ["docker"],
                "labels": {"site": "hq"},
            }
        },
    }


def _base_identity() -> RunnerChannelIdentity:
    return RunnerChannelIdentity(
        tenant_id="tenant-1",
        runner_id="runner-1",
        runner_status="active",
        credential_status="active",
    )


def _tooling_plane_tool_result_envelope(
    *,
    runtime_job_id: str = "tool-runtime-1",
    task_id: int | None = 42,
    command_id: str = "cmd-42",
    metadata_overrides: dict[str, object] | None = None,
    payload_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "task_runtime_job_id": "task-runtime-1",
        "workspace_id": "task-42",
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    payload: dict[str, object] = {
        "operation_id": "tool-op-1",
        "command_id": command_id,
        "tool": "shell.exec",
        "status": "succeeded",
        "success": True,
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "artifacts": ["artifacts/cmd-42/stdout.txt"],
        "error_code": None,
        "error_message": None,
        "result": {"duration_seconds": 0.1},
        "metadata": metadata,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return {
        "message_id": "msg-tool-result-1",
        "type": "tool.result",
        "schema_version": "tooling_plane.v1",
        "tenant_id": "tenant-1",
        "runner_id": "runner-1",
        "correlation_id": "corr-tool-result-1",
        "runtime_job_id": runtime_job_id,
        "task_id": task_id,
        "created_at": "2026-05-24T10:00:00Z",
        "payload": payload,
    }


def _runtime_job_lookup(bindings: dict[str, RunnerRuntimeJobBinding]):
    def _lookup(runtime_job_id: str) -> RunnerRuntimeJobBinding | None:
        return bindings.get(runtime_job_id)

    return _lookup


def _data_plane_artifact_manifest_envelope(
    *,
    runtime_job_id: str = "tool-runtime-1",
    task_id: int | None = 42,
    tenant_id: str = "tenant-1",
    runner_id: str = "runner-1",
    command_id: str = "cmd-42",
    task_runtime_job_id: str = "task-runtime-1",
    workspace_id: str = "task-42",
    artifact_path: str = "artifacts/cmd-42/stdout.txt",
    artifacts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "message_id": "msg-artifact-manifest-1",
        "type": "artifact.manifest",
        "schema_version": "data_plane.v1",
        "tenant_id": tenant_id,
        "runner_id": runner_id,
        "correlation_id": "corr-artifact-manifest-1",
        "runtime_job_id": runtime_job_id,
        "task_id": task_id,
        "created_at": "2026-05-25T10:00:00Z",
        "payload": {
            "task_runtime_job_id": task_runtime_job_id,
            "command_id": command_id,
            "workspace_id": workspace_id,
            "tool_call_id": "tool-call-42",
            "tool_batch_id": "tool-batch-1",
            "artifacts": artifacts
            or [
                {
                    "artifact_client_id": "artifact-client-1",
                    "relative_path": artifact_path,
                    "artifact_kind": "stdout",
                    "size_bytes": 12,
                    "content_sha256": "a" * 64,
                    "content_type": "text/plain",
                    "is_text": True,
                    "created_at": "2026-05-25T10:00:00Z",
                    "metadata": {"origin": "kali"},
                }
            ],
        },
    }


def _data_plane_artifact_upload_complete_envelope(
    *,
    runtime_job_id: str = "tool-runtime-1",
    task_id: int | None = 42,
    tenant_id: str = "tenant-1",
    runner_id: str = "runner-1",
    command_id: str = "cmd-42",
    task_runtime_job_id: str = "task-runtime-1",
    workspace_id: str = "task-42",
    artifact_id: str = "artifact-1",
    object_key: str = "data-plane-prefix/tenants/1/tasks/42/executions/exe-1/artifacts/artifact-1/stdout.txt",
) -> dict[str, object]:
    return {
        "message_id": "msg-artifact-upload-complete-1",
        "type": "artifact.upload.complete",
        "schema_version": "data_plane.v1",
        "tenant_id": tenant_id,
        "runner_id": runner_id,
        "correlation_id": "corr-artifact-upload-complete-1",
        "runtime_job_id": runtime_job_id,
        "task_id": task_id,
        "created_at": "2026-05-25T10:00:00Z",
        "payload": {
            "task_runtime_job_id": task_runtime_job_id,
            "command_id": command_id,
            "workspace_id": workspace_id,
            "tool_call_id": "tool-call-42",
            "tool_batch_id": "tool-batch-1",
            "uploads": [
                {
                    "artifact_id": artifact_id,
                    "artifact_client_id": "artifact-client-1",
                    "object_key": object_key,
                    "size_bytes": 12,
                    "content_sha256": "a" * 64,
                    "uploaded_at": "2026-05-25T10:01:00Z",
                }
            ],
        },
    }


def _build_channel_manager_test_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
            RunnerControlMessage.__table__,
            RuntimeJob.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_validate_inbound_message_accepts_matching_context_and_returns_idempotency_key() -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=lambda runtime_job_id: RunnerRuntimeJobBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="tenant-1",
            runner_id="runner-1",
            task_id=42,
        )
    )

    validated = validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert validated.idempotency_key == "tenant-1:runner-1:msg-1"
    assert validated.runtime_job_binding is not None
    assert validated.runtime_job_binding.runtime_job_id == "job-1"


def test_validate_inbound_message_rejects_runner_identity_mismatch() -> None:
    payload = _base_envelope()
    payload["runner_id"] = "runner-2"
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_IDENTITY_MISMATCH"


def test_validate_inbound_message_rejects_tenant_mismatch() -> None:
    payload = _base_envelope()
    payload["tenant_id"] = "tenant-2"
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TENANT_MISMATCH"


def test_validate_inbound_message_rejects_unsupported_schema_version() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "runner_control.v0"
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_PROTOCOL_UNSUPPORTED"


def test_validate_inbound_message_accepts_remote_runtime_schema_version() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "remote_runtime.v1"
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator(
        runtime_job_lookup=lambda runtime_job_id: RunnerRuntimeJobBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="tenant-1",
            runner_id="runner-1",
            task_id=42,
        )
    )

    validated = validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert validated.idempotency_key == "tenant-1:runner-1:msg-1"


def test_validate_inbound_message_rejects_duplicate_idempotency_key() -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator(duplicate_checker=lambda key: key == "tenant-1:runner-1:msg-1")

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_MESSAGE_DUPLICATE"


def test_validate_inbound_message_rejects_unassigned_runtime_job() -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator(runtime_job_lookup=lambda _runtime_job_id: None)

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_rejects_runner_originated_tool_command_directionality() -> None:
    payload = _base_envelope()
    payload["runtime_job_id"] = None
    payload["schema_version"] = "tooling_plane.v1"
    payload["type"] = "tool.command"
    payload["payload"] = {
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
        "params": {},
    }
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator(
        task_assignment_checker=lambda _tenant_id, _runner_id, _task_id: False
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_DIRECTION_INVALID"


def test_validate_inbound_message_rejects_runner_originated_artifact_upload_request_directionality() -> None:
    payload = _base_envelope()
    payload["schema_version"] = "data_plane.v1"
    payload["type"] = "artifact.upload.request"
    payload["payload"] = {
        "task_runtime_job_id": "task-runtime-42",
        "command_id": "cmd-42",
        "workspace_id": "task-42",
        "tool_call_id": "tool-call-42",
        "tool_batch_id": "tool-batch-1",
        "uploads": [
            {
                "artifact_id": "artifact-1",
                "artifact_client_id": "artifact-client-1",
                "object_key": "data-plane/tenant-1/task-42/artifact-1/stdout.txt",
                "upload_url": "https://example.test/upload/artifact-1",
                "upload_method": "PUT",
                "upload_headers": {"x-upload-token": "opaque"},
                "size_bytes": 12,
                "content_sha256": "a" * 64,
                "content_type": "text/plain",
                "is_text": True,
            }
        ],
    }
    envelope = parse_runner_envelope(payload)
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_DIRECTION_INVALID"


def test_validate_inbound_message_rejects_inactive_runner_status() -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator()
    identity = RunnerChannelIdentity(
        tenant_id="tenant-1",
        runner_id="runner-1",
        runner_status="offline",
        credential_status="active",
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=identity, envelope=envelope)

    assert error.value.error_code == "RUNNER_OFFLINE"


@pytest.mark.parametrize(
    ("credential_status", "expected_error_code"),
    (
        ("revoked", "RUNNER_CREDENTIAL_REVOKED"),
        ("expired", "RUNNER_CREDENTIAL_EXPIRED"),
        ("disabled", "RUNNER_AUTH_INVALID"),
    ),
)
def test_validate_inbound_message_rejects_inactive_credential_statuses(
    credential_status: str,
    expected_error_code: str,
) -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator()
    identity = RunnerChannelIdentity(
        tenant_id="tenant-1",
        runner_id="runner-1",
        runner_status="active",
        credential_status=credential_status,
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=identity, envelope=envelope)

    assert error.value.error_code == expected_error_code


def test_validate_inbound_message_rejects_runtime_job_task_mismatch_as_not_assigned() -> None:
    envelope = parse_runner_envelope(_base_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=lambda runtime_job_id: RunnerRuntimeJobBinding(
            runtime_job_id=runtime_job_id,
            tenant_id="tenant-1",
            runner_id="runner-1",
            task_id=999,
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_accepts_tool_result_with_channel_manager_runtime_lookup() -> None:
    db = _build_channel_manager_test_session()
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    db.add(tenant)
    db.flush()

    user = User(username="runner-protocol-user", password="hashed", email="runner-protocol@example.com")
    db.add(user)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status="active",
    )
    db.add(runner)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name="runner-protocol-task",
        status="running",
        runtime_placement_mode="runner",
        runner_id=str(runner.id),
        workspace_id="task-42",
    )
    db.add(task)
    db.flush()

    task_runtime_job_id = uuid.uuid4()
    task_runtime_job = RuntimeJob(
        id=task_runtime_job_id,
        tenant_id=tenant.id,
        task_id=task.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        job_type="task.start",
        status="running",
        idempotency_key="task-start-key",
        payload_json={"workspace_id": "task-42"},
    )
    db.add(task_runtime_job)

    tool_runtime_job_id = uuid.uuid4()
    tool_runtime_job = RuntimeJob(
        id=tool_runtime_job_id,
        tenant_id=tenant.id,
        task_id=task.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        job_type="tool.command",
        status="running",
        idempotency_key="tool-command-key",
        payload_json={
            "workspace_id": "task-42",
            "command_id": "cmd-42",
            "task_runtime_job_id": str(task_runtime_job_id),
        },
    )
    db.add(tool_runtime_job)
    db.commit()

    validator = RunnerProtocolValidator(
        runtime_job_lookup=lambda runtime_job_id: _lookup_runtime_job_binding(
            db,
            runtime_job_id,
        )
    )
    envelope_payload = _tooling_plane_tool_result_envelope(
        runtime_job_id=str(tool_runtime_job_id),
        task_id=task.id,
        command_id="cmd-42",
        metadata_overrides={
            "task_runtime_job_id": str(task_runtime_job_id),
            "workspace_id": "task-42",
        },
    )
    envelope_payload["tenant_id"] = str(tenant.id)
    envelope_payload["runner_id"] = str(runner.id)
    envelope = parse_runner_envelope(envelope_payload)
    identity = RunnerChannelIdentity(
        tenant_id=str(tenant.id),
        runner_id=str(runner.id),
        runner_status="active",
        credential_status="active",
    )

    validated = validator.validate_inbound_message(identity=identity, envelope=envelope)

    assert validated.runtime_job_binding is not None
    assert validated.runtime_job_binding.runtime_job_id == str(tool_runtime_job_id)
    assert validated.runtime_job_binding.job_type == "tool.command"
    assert validated.runtime_job_binding.command_id == "cmd-42"
    assert validated.runtime_job_binding.task_runtime_job_id == str(task_runtime_job_id)
    assert validated.runtime_job_binding.workspace_id == "task-42"


def test_validate_inbound_message_accepts_tool_result_with_consistent_runtime_bindings() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    validated = validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert validated.runtime_job_binding is not None
    assert validated.runtime_job_binding.runtime_job_id == "tool-runtime-1"


def test_validate_inbound_message_rejects_tool_result_with_foreign_runner_binding() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-foreign",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_rejects_tool_result_with_foreign_tenant_binding() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-foreign",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_rejects_tool_result_with_wrong_tool_command_runtime_job_type() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_rejects_tool_result_with_wrong_command_id() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope(command_id="cmd-other"))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TOOL_COMMAND_ID_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_without_task_id() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope(task_id=None))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TOOL_TASK_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_with_oversized_result_mapping() -> None:
    envelope = parse_runner_envelope(
        _tooling_plane_tool_result_envelope(
            payload_overrides={"result": {"blob": "x" * (70 * 1024)}},
        )
    )
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TOOL_RESULT_PAYLOAD_TOO_LARGE"


def test_validate_inbound_message_rejects_tool_result_with_wrong_task_runtime_job() -> None:
    envelope = parse_runner_envelope(
        _tooling_plane_tool_result_envelope(
            metadata_overrides={"task_runtime_job_id": "task-runtime-foreign"}
        )
    )
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
                "task-runtime-foreign": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-foreign",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=99,
                    job_type="task.start",
                    workspace_id="task-99",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TOOL_TASK_RUNTIME_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_with_wrong_workspace_binding() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-41",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_when_command_binding_workspace_missing() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id=None,
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_when_task_binding_workspace_missing() -> None:
    envelope = parse_runner_envelope(_tooling_plane_tool_result_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id=None,
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_validate_inbound_message_rejects_tool_result_when_payload_workspace_missing() -> None:
    envelope = parse_runner_envelope(
        _tooling_plane_tool_result_envelope(
            metadata_overrides={"workspace_id": None}
        )
    )
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_validate_inbound_message_accepts_artifact_manifest_binding() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    validated = validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert validated.runtime_job_binding is not None
    assert validated.runtime_job_binding.runtime_job_id == "tool-runtime-1"


def test_validate_inbound_message_rejects_artifact_manifest_wrong_runner() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope(runner_id="runner-foreign"))
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_IDENTITY_MISMATCH"


def test_validate_inbound_message_rejects_artifact_manifest_wrong_tenant() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope(tenant_id="tenant-foreign"))
    validator = RunnerProtocolValidator()

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_TENANT_MISMATCH"


def test_validate_inbound_message_rejects_artifact_manifest_wrong_task() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope(task_id=99))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_validate_inbound_message_rejects_artifact_manifest_wrong_command() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope(command_id="cmd-foreign"))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_ARTIFACT_COMMAND_ID_MISMATCH"


def test_validate_inbound_message_rejects_artifact_manifest_wrong_workspace() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_manifest_envelope(workspace_id="task-99"))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_validate_inbound_message_accepts_artifact_upload_complete_binding() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_upload_complete_envelope())
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    validated = validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert validated.runtime_job_binding is not None
    assert validated.runtime_job_binding.runtime_job_id == "tool-runtime-1"


def test_validate_inbound_message_rejects_artifact_upload_complete_wrong_command() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_upload_complete_envelope(command_id="cmd-foreign"))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_ARTIFACT_COMMAND_ID_MISMATCH"


def test_validate_inbound_message_rejects_artifact_upload_complete_wrong_workspace() -> None:
    envelope = parse_runner_envelope(_data_plane_artifact_upload_complete_envelope(workspace_id="task-99"))
    validator = RunnerProtocolValidator(
        runtime_job_lookup=_runtime_job_lookup(
            {
                "tool-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="tool-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="tool.command",
                    workspace_id="task-42",
                    command_id="cmd-42",
                    task_runtime_job_id="task-runtime-1",
                ),
                "task-runtime-1": RunnerRuntimeJobBinding(
                    runtime_job_id="task-runtime-1",
                    tenant_id="tenant-1",
                    runner_id="runner-1",
                    task_id=42,
                    job_type="task.start",
                    workspace_id="task-42",
                ),
            }
        )
    )

    with pytest.raises(RunnerProtocolValidationError) as error:
        validator.validate_inbound_message(identity=_base_identity(), envelope=envelope)

    assert error.value.error_code == "RUNNER_WORKSPACE_MISMATCH"


def test_parse_runner_envelope_rejects_artifact_manifest_with_malformed_relative_path() -> None:
    with pytest.raises(SharedRunnerProtocolValidationError):
        parse_runner_envelope(_data_plane_artifact_manifest_envelope(artifact_path="../etc/passwd"))


def test_parse_runner_envelope_rejects_artifact_manifest_with_oversized_item_count() -> None:
    artifact = {
        "artifact_client_id": "artifact-client",
        "relative_path": "artifacts/file.txt",
        "artifact_kind": "stdout",
        "size_bytes": 12,
        "content_sha256": "a" * 64,
        "content_type": "text/plain",
        "is_text": True,
        "created_at": "2026-05-25T10:00:00Z",
        "metadata": {},
    }
    with pytest.raises(SharedRunnerProtocolValidationError):
        parse_runner_envelope(_data_plane_artifact_manifest_envelope(artifacts=[dict(artifact) for _ in range(257)]))
