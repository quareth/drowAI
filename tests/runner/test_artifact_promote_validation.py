"""Tests for runner-side runtime.artifact.promote validation and cache fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from drowai_runner.control_channel.runtime.validation import RemoteRuntimeRequestValidator
from drowai_runner.cloud_client import RunnerCloudClient
from drowai_runner.control_channel.artifacts.promote import ArtifactPromoteHandler
from drowai_runner.control_channel.composition import RunnerControlChannelComposition
from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.job_store import initialize_runner_job_store
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerRuntimeOperationPayload,
)


def _identity() -> CloudChannelIdentity:
    return CloudChannelIdentity(
        tenant_id=1,
        runner_id="runner-promote-test",
        credential_secret="secret",
        channel_endpoint="ws://localhost",
        protocol_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        heartbeat_interval_seconds=30,
    )


def _promote_envelope(
    *,
    control_runtime_job_id: str,
    params: dict[str, object],
) -> RunnerEnvelope:
    payload = RunnerRuntimeOperationPayload(
        operation_id="promote_artifact_refs:test",
        workspace_id="task-61",
        runtime_image="drowai-runtime-local:latest",
        operation=RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE.value,
        params=params,
    )
    return RunnerEnvelope(
        message_id="promote-msg-1",
        message_type=RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE,
        schema_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        tenant_id="1",
        runner_id="runner-promote-test",
        correlation_id=None,
        runtime_job_id=control_runtime_job_id,
        task_id=61,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=payload,
        raw_message_type=RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE.value,
    )


def test_resolve_runner_runtime_job_id_falls_back_to_task_runtime_job_id() -> None:
    inbound = _promote_envelope(
        control_runtime_job_id="promote-control-job",
        params={
            "task_runtime_job_id": "task-start-job",
            "command_id": "cmd-1",
        },
    )
    resolved = RemoteRuntimeRequestValidator.resolve_runner_runtime_job_id(
        inbound=inbound,
        fallback_runtime_job_id="promote-control-job",
    )
    assert resolved == "task-start-job"


def test_validate_promote_accepts_task_runtime_binding_without_params_runtime_job_id(
    tmp_path,
) -> None:
    task_start_job_id = "41d168bf-063a-4d62-a7dc-2f52df483e3c"
    store = initialize_runner_job_store(tmp_path / "jobs.sqlite")
    store.start_job(
        runtime_job_id=task_start_job_id,
        tenant_id="1",
        task_id="61",
        workspace_id="task-61",
        image="drowai-runtime-local:latest",
    )

    client = RunnerCloudClient.__new__(RunnerCloudClient)
    client._composition = RunnerControlChannelComposition.__new__(
        RunnerControlChannelComposition
    )
    client._composition._job_store = store

    inbound = _promote_envelope(
        control_runtime_job_id="68fa4e0e-433e-493f-a399-bbf12f79ad32",
        params={
            "task_runtime_job_id": task_start_job_id,
            "tool_command_runtime_job_id": "d0f8634b-61c6-4e15-9cf4-b376114993d7",
            "command_id": "tc_b0e82a0a477e41b09522e8b85ede6fb9",
            "workspace_id": "task-61",
            "artifacts": ["artifacts/fping.txt"],
        },
    )

    status, error_code, context = RemoteRuntimeRequestValidator(
        job_store_provider=client._composition.job_store,
    ).validate(
        identity=_identity(),
        inbound=inbound,
    )
    assert status == "accepted"
    assert error_code is None
    assert context is not None
    assert context.runtime_job_id == task_start_job_id


def test_validate_promote_rejects_unknown_control_job_without_task_binding(tmp_path) -> None:
    client = RunnerCloudClient.__new__(RunnerCloudClient)
    client._composition = RunnerControlChannelComposition.__new__(
        RunnerControlChannelComposition
    )
    client._composition._job_store = initialize_runner_job_store(tmp_path / "jobs.sqlite")

    inbound = _promote_envelope(
        control_runtime_job_id="68fa4e0e-433e-493f-a399-bbf12f79ad32",
        params={"command_id": "cmd-1"},
    )

    status, error_code, _context = RemoteRuntimeRequestValidator(
        job_store_provider=client._composition.job_store,
    ).validate(
        identity=_identity(),
        inbound=inbound,
    )
    assert status == "rejected"
    assert error_code == "RUNTIME_JOB_NOT_ASSIGNED"


def test_build_promote_cache_entry_from_params_without_prior_tool_command_cache() -> None:
    promote_handler = ArtifactPromoteHandler(
        validate_runtime_request=MagicMock(),
        operation_service_provider=MagicMock(),
        workspace_manager=MagicMock(),
        manifest_sender=MagicMock(),
    )
    inbound = _promote_envelope(
        control_runtime_job_id="promote-control-job",
        params={
            "task_runtime_job_id": "task-start-job",
            "tool_command_runtime_job_id": "tool-command-job",
            "command_id": "cmd-1",
            "workspace_id": "task-61",
            "tool": "information_gathering.network_discovery.fping",
            "artifacts": ["artifacts/fping.txt"],
            "canonical_status": "succeeded",
            "canonical_success": True,
            "canonical_exit_code": 1,
        },
    )
    entry = promote_handler.build_promote_cache_entry_from_params(
        promote_params=dict(inbound.payload.params),
        inbound=inbound,
        command_key=("task-start-job", "cmd-1"),
    )
    assert entry is not None
    assert entry.tool_command_runtime_job_id == "tool-command-job"
    assert entry.result_payload.exit_code == 1
    assert entry.result_payload.artifacts == ("artifacts/fping.txt",)
