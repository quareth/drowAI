"""Runner cloud-channel protocol helpers for envelope construction and parsing.

This module owns runner-side message helpers used by cloud mode. It keeps
wire-shape details in one place and depends only on runtime-shared DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import uuid
from typing import Any, Callable, Mapping

from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerAckPayload,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompletePayload,
    RunnerArtifactUploadRequestPayload,
    RunnerEnvelope,
    RunnerHeartbeatPayload,
    RunnerHelloPayload,
    RunnerMessageType,
    RunnerProtocolValidationError,
    RunnerToolCommandPayload,
    RunnerToolResultPayload,
    is_runner_event_message_type,
    is_runner_executable_control_message_type,
    parse_runner_envelope,
)

RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE = "RUNNER_ASSIGNMENT_NOT_FOUND"
RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE = "RUNTIME_JOB_NOT_ASSIGNED"
RUNNER_DEFERRED_RUNTIME_ERROR_CODE = "RUNNER_REMOTE_OPERATION_DEFERRED"
_RUNNER_CONTROL_ASSIGNMENT_PROBE_TYPES = frozenset(
    {
        RunnerMessageType.RUNNER_ASSIGNMENT_PROBE.value,
        "runtime.assignment.probe",
    }
)
_RUNNER_CONTROL_UNSUPPORTED_RUNTIME_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_INVENTORY,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
        RunnerMessageType.TOOL_COMMAND,
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
        RunnerMessageType.RUNNER_CONFIG_UPDATE,
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    }
)
_RUNNER_CONTROL_RUNTIME_JOB_SCOPED_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_INVENTORY,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
        RunnerMessageType.TOOL_COMMAND,
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    }
)
_REMOTE_RUNTIME_MESSAGE_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_INVENTORY,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
        RunnerMessageType.RUNTIME_STARTED,
        RunnerMessageType.RUNTIME_PAUSED,
        RunnerMessageType.RUNTIME_RESUMED,
        RunnerMessageType.RUNTIME_STOPPED,
        RunnerMessageType.RUNTIME_RETIRED,
        RunnerMessageType.RUNTIME_FAILED,
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
        RunnerMessageType.TERMINAL_RESULT,
        RunnerMessageType.TERMINAL_FRAME,
    }
)
_TOOLING_PLANE_RUNTIME_IDENTITY_PARAM_KEYS = frozenset(
    {
        "tenant_id",
        "runner_id",
        "runtime_job_id",
        "runner_runtime_job_id",
        "task_runtime_job_id",
        "task_id",
        "workspace_id",
    }
)
_TOOLING_PLANE_SECRET_REFERENCE_PARAM_KEYS = frozenset(
    {
        "secret_ref",
        "secret_refs",
        "secret_reference",
        "secret_references",
        "secret_resolver",
        "secret_resolution",
        "resolve_secret",
        "resolve_secrets",
    }
)


@dataclass(frozen=True, slots=True)
class RunnerTaskRuntimeBinding:
    """Runner-local task runtime identity used for tooling_plane tool.command validation."""

    runtime_job_id: str
    tenant_id: str
    task_id: str
    workspace_id: str


RunnerTaskRuntimeBindingLookup = Callable[[str], RunnerTaskRuntimeBinding | None]


@dataclass(frozen=True, slots=True)
class RunnerInboundAckDecision:
    """Deterministic runner ack decision for one inbound control envelope."""

    should_ack: bool
    status: str | None
    error_code: str | None


def build_runner_hello_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    runner_version: str,
    labels: Mapping[str, str],
    capabilities: tuple[str, ...],
    protocol_version: str = RUNNER_PROTOCOL_SCHEMA_VERSION,
) -> RunnerEnvelope:
    """Build a typed `runner.hello` envelope."""
    payload = RunnerHelloPayload(
        version=runner_version.strip() or "unknown",
        capabilities=tuple(item.strip() for item in capabilities if item.strip()),
        labels=dict(labels),
    )
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.RUNNER_HELLO,
        payload=payload,
        protocol_version=protocol_version,
    )


def build_runner_heartbeat_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    heartbeat_payload: RunnerHeartbeatPayload,
    protocol_version: str = RUNNER_PROTOCOL_SCHEMA_VERSION,
) -> RunnerEnvelope:
    """Build a typed `runner.heartbeat` envelope with capacity snapshot."""
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.RUNNER_HEARTBEAT,
        payload=heartbeat_payload,
        protocol_version=protocol_version,
    )


def build_runner_ack_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    acked_message_id: str,
    status: str = "accepted",
    error_code: str | None = None,
    correlation_id: str | None = None,
    protocol_version: str = RUNNER_PROTOCOL_SCHEMA_VERSION,
) -> RunnerEnvelope:
    """Build a typed `runner.ack` envelope."""
    payload = RunnerAckPayload(
        acked_message_id=str(acked_message_id).strip(),
        status=str(status).strip() or None,
        error_code=(str(error_code).strip() or None) if error_code is not None else None,
    )
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.RUNNER_ACK,
        payload=payload,
        correlation_id=correlation_id,
        protocol_version=protocol_version,
    )


def build_remote_runtime_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    message_type: RunnerMessageType,
    payload: Any,
    correlation_id: str | None = None,
    runtime_job_id: str | None = None,
    task_id: int | None = None,
    protocol_version: str = RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
) -> RunnerEnvelope:
    """Build a remote_runtime runtime/terminal envelope (default remote_runtime schema version)."""
    if message_type not in _REMOTE_RUNTIME_MESSAGE_TYPES:
        raise RunnerProtocolValidationError(
            f"{message_type.value} is not a remote_runtime runtime/terminal message type."
        )
    normalized_protocol_version = str(protocol_version).strip()
    if normalized_protocol_version != RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION:
        raise RunnerProtocolValidationError(
            f"Unsupported schema version `{normalized_protocol_version}` for `{message_type.value}`."
        )
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=message_type,
        payload=payload,
        correlation_id=correlation_id,
        runtime_job_id=runtime_job_id,
        task_id=task_id,
        protocol_version=normalized_protocol_version,
    )


def build_tooling_plane_tool_result_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    payload: RunnerToolResultPayload,
    correlation_id: str | None = None,
    runtime_job_id: str | None = None,
    task_id: int | None = None,
    protocol_version: str = RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
) -> RunnerEnvelope:
    """Build a tooling_plane ``tool.result`` envelope for runner command completion events."""
    normalized_protocol_version = str(protocol_version).strip()
    if normalized_protocol_version != RUNNER_PROTOCOL_TOOLING_PLANE_VERSION:
        raise RunnerProtocolValidationError(
            f"Unsupported schema version `{normalized_protocol_version}` for `tool.result`."
        )
    normalized_runtime_job_id = str(runtime_job_id or "").strip()
    if not normalized_runtime_job_id:
        raise RunnerProtocolValidationError("tool.result runtime_job_id is required.")
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.TOOL_RESULT,
        payload=payload,
        correlation_id=correlation_id,
        runtime_job_id=normalized_runtime_job_id,
        task_id=task_id,
        protocol_version=normalized_protocol_version,
    )


def build_data_plane_artifact_manifest_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    payload: RunnerArtifactManifestPayload,
    correlation_id: str | None = None,
    runtime_job_id: str | None = None,
    task_id: int | None = None,
    protocol_version: str = RUNNER_PROTOCOL_DATA_PLANE_VERSION,
) -> RunnerEnvelope:
    """Build a data_plane ``artifact.manifest`` envelope."""
    normalized_protocol_version = str(protocol_version).strip()
    if normalized_protocol_version != RUNNER_PROTOCOL_DATA_PLANE_VERSION:
        raise RunnerProtocolValidationError(
            f"Unsupported schema version `{normalized_protocol_version}` for `artifact.manifest`."
        )
    normalized_runtime_job_id = str(runtime_job_id or "").strip()
    if not normalized_runtime_job_id:
        raise RunnerProtocolValidationError("artifact.manifest runtime_job_id is required.")
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.ARTIFACT_MANIFEST,
        payload=payload,
        correlation_id=correlation_id,
        runtime_job_id=normalized_runtime_job_id,
        task_id=task_id,
        protocol_version=normalized_protocol_version,
    )


def build_data_plane_artifact_upload_complete_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    payload: RunnerArtifactUploadCompletePayload,
    correlation_id: str | None = None,
    runtime_job_id: str | None = None,
    task_id: int | None = None,
    protocol_version: str = RUNNER_PROTOCOL_DATA_PLANE_VERSION,
) -> RunnerEnvelope:
    """Build a data_plane ``artifact.upload.complete`` envelope."""
    normalized_protocol_version = str(protocol_version).strip()
    if normalized_protocol_version != RUNNER_PROTOCOL_DATA_PLANE_VERSION:
        raise RunnerProtocolValidationError(
            f"Unsupported schema version `{normalized_protocol_version}` for `artifact.upload.complete`."
        )
    normalized_runtime_job_id = str(runtime_job_id or "").strip()
    if not normalized_runtime_job_id:
        raise RunnerProtocolValidationError("artifact.upload.complete runtime_job_id is required.")
    return _build_envelope(
        tenant_id=tenant_id,
        runner_id=runner_id,
        message_type=RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
        payload=payload,
        correlation_id=correlation_id,
        runtime_job_id=normalized_runtime_job_id,
        task_id=task_id,
        protocol_version=normalized_protocol_version,
    )


def parse_inbound_envelope(raw_message: str | bytes) -> RunnerEnvelope:
    """Parse one inbound websocket text/binary message into a runner envelope."""
    if isinstance(raw_message, bytes):
        try:
            raw_text = raw_message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunnerProtocolValidationError("Inbound envelope must be UTF-8 text.") from exc
    else:
        raw_text = raw_message

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RunnerProtocolValidationError("Inbound envelope JSON is invalid.") from exc
    if not isinstance(payload, Mapping):
        raise RunnerProtocolValidationError("Inbound envelope must decode to an object.")
    return parse_runner_envelope(payload)


def should_ack_inbound(envelope: RunnerEnvelope) -> bool:
    """Return whether the runner should ack an inbound control-plane envelope."""
    if is_assignment_probe_message(envelope):
        return True
    if envelope.message_type is RunnerMessageType.TOOL_COMMAND:
        return True
    return envelope.message_type in _RUNNER_CONTROL_UNSUPPORTED_RUNTIME_TYPES


def is_assignment_probe_message(envelope: RunnerEnvelope) -> bool:
    """Return whether inbound envelope is a runner_control assignment/probe control message."""
    return envelope.type in _RUNNER_CONTROL_ASSIGNMENT_PROBE_TYPES


def is_runner_executable_control_message(envelope: RunnerEnvelope) -> bool:
    """Return whether inbound envelope is a runner-executable control command."""
    return is_runner_executable_control_message_type(envelope.message_type)


def is_runner_event_message(envelope: RunnerEnvelope) -> bool:
    """Return whether envelope is a runner-originated event/update message."""
    return is_runner_event_message_type(envelope.message_type)


def validate_data_plane_artifact_upload_request_binding(
    envelope: RunnerEnvelope,
    *,
    expected_tenant_id: int,
    expected_runner_id: str,
) -> RunnerArtifactUploadRequestPayload:
    """Validate data_plane upload-request identity before runner upload side effects."""
    if envelope.message_type is not RunnerMessageType.ARTIFACT_UPLOAD_REQUEST:
        raise RunnerProtocolValidationError(
            "data_plane upload request validation requires `artifact.upload.request` type."
        )
    if not isinstance(envelope.payload, RunnerArtifactUploadRequestPayload):
        raise RunnerProtocolValidationError(
            "data_plane upload request validation requires typed upload-request payload."
        )
    if str(envelope.tenant_id).strip() != str(int(expected_tenant_id)):
        raise RunnerProtocolValidationError(
            "artifact.upload.request tenant_id does not match authenticated runner tenant."
        )
    if str(envelope.runner_id).strip() != str(expected_runner_id).strip():
        raise RunnerProtocolValidationError(
            "artifact.upload.request runner_id does not match authenticated runner identity."
        )
    runtime_job_id = str(envelope.runtime_job_id or "").strip()
    if not runtime_job_id:
        raise RunnerProtocolValidationError("artifact.upload.request runtime_job_id is required.")
    if envelope.task_id is None:
        raise RunnerProtocolValidationError("artifact.upload.request task_id is required.")
    if not envelope.payload.uploads:
        raise RunnerProtocolValidationError("artifact.upload.request uploads must not be empty.")
    return envelope.payload


def validate_tooling_plane_tool_command_binding(
    envelope: RunnerEnvelope,
    *,
    expected_tenant_id: int,
    expected_runner_id: str,
    task_runtime_binding_lookup: RunnerTaskRuntimeBindingLookup,
) -> RunnerTaskRuntimeBinding:
    """Validate tooling_plane tool.command identity binding against local task runtime state."""
    if envelope.message_type is not RunnerMessageType.TOOL_COMMAND:
        raise RunnerProtocolValidationError("tooling_plane tool.command validation requires `tool.command` type.")
    if not isinstance(envelope.payload, RunnerToolCommandPayload):
        raise RunnerProtocolValidationError("tooling_plane tool.command validation requires typed payload.")
    if str(envelope.tenant_id).strip() != str(int(expected_tenant_id)):
        raise RunnerProtocolValidationError("tool.command tenant_id does not match authenticated runner tenant.")
    if str(envelope.runner_id).strip() != str(expected_runner_id).strip():
        raise RunnerProtocolValidationError("tool.command runner_id does not match authenticated runner identity.")
    envelope_runtime_job_id = str(envelope.runtime_job_id or "").strip()
    if not envelope_runtime_job_id:
        raise RunnerProtocolValidationError("tool.command runtime_job_id is required for correlation.")

    task_runtime_job_id = envelope.payload.task_runtime_job_id
    binding = task_runtime_binding_lookup(task_runtime_job_id)
    if binding is None:
        raise RunnerProtocolValidationError(
            "tool.command task_runtime_job_id is missing or not present in local runner job store."
        )

    normalized_binding = RunnerTaskRuntimeBinding(
        runtime_job_id=str(binding.runtime_job_id).strip(),
        tenant_id=str(binding.tenant_id).strip(),
        task_id=str(binding.task_id).strip(),
        workspace_id=str(binding.workspace_id).strip(),
    )
    if not normalized_binding.runtime_job_id:
        raise RunnerProtocolValidationError("Local task runtime binding runtime_job_id is empty.")
    if not normalized_binding.tenant_id:
        raise RunnerProtocolValidationError("Local task runtime binding tenant_id is empty.")
    if normalized_binding.tenant_id != str(int(expected_tenant_id)):
        raise RunnerProtocolValidationError(
            "tool.command task_runtime_job_id tenant_id does not match local task runtime binding."
        )
    if not normalized_binding.task_id:
        raise RunnerProtocolValidationError("Local task runtime binding task_id is empty.")
    if envelope.task_id is None:
        raise RunnerProtocolValidationError("tool.command task_id is required for local task runtime binding.")
    if normalized_binding.task_id != str(envelope.task_id):
        raise RunnerProtocolValidationError(
            "tool.command task_id does not match local task runtime task binding."
        )
    if not normalized_binding.workspace_id:
        raise RunnerProtocolValidationError("Local task runtime binding workspace_id is empty.")
    if envelope.payload.workspace_id != normalized_binding.workspace_id:
        raise RunnerProtocolValidationError(
            "tool.command workspace_id does not match local task runtime workspace binding."
        )
    for key in envelope.payload.params.keys():
        lowered_key = str(key).strip().lower()
        if lowered_key in _TOOLING_PLANE_RUNTIME_IDENTITY_PARAM_KEYS:
            raise RunnerProtocolValidationError(
                f"tool.command params must not override runtime identity field `{key}`."
            )
        if lowered_key in _TOOLING_PLANE_SECRET_REFERENCE_PARAM_KEYS:
            raise RunnerProtocolValidationError(
                f"tool.command params must not include secret-reference field `{key}`."
            )
    return normalized_binding


def classify_runner_control_inbound_ack(
    envelope: RunnerEnvelope,
    *,
    expected_tenant_id: int,
    expected_runner_id: str,
    assigned_runtime_jobs: Mapping[str, int | None] | None = None,
    task_runtime_binding_lookup: RunnerTaskRuntimeBindingLookup | None = None,
) -> RunnerInboundAckDecision:
    """Classify inbound runner_control control message into deterministic ack outcome."""
    if not should_ack_inbound(envelope):
        return RunnerInboundAckDecision(
            should_ack=False,
            status=None,
            error_code=None,
        )

    if str(envelope.tenant_id).strip() != str(int(expected_tenant_id)):
        return RunnerInboundAckDecision(
            should_ack=True,
            status="rejected",
            error_code=RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE,
        )
    if str(envelope.runner_id).strip() != str(expected_runner_id).strip():
        return RunnerInboundAckDecision(
            should_ack=True,
            status="rejected",
            error_code=RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE,
        )
    if is_assignment_probe_message(envelope):
        return RunnerInboundAckDecision(
            should_ack=True,
            status="accepted",
            error_code=None,
        )
    if envelope.message_type is RunnerMessageType.TOOL_COMMAND:
        if task_runtime_binding_lookup is None:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
            )
        try:
            validate_tooling_plane_tool_command_binding(
                envelope,
                expected_tenant_id=expected_tenant_id,
                expected_runner_id=expected_runner_id,
                task_runtime_binding_lookup=task_runtime_binding_lookup,
            )
        except RunnerProtocolValidationError:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
            )
        return RunnerInboundAckDecision(
            should_ack=True,
            status="accepted",
            error_code=None,
        )
    if envelope.message_type in _RUNNER_CONTROL_RUNTIME_JOB_SCOPED_TYPES:
        runtime_job_id = (envelope.runtime_job_id or "").strip()
        if not runtime_job_id:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_ASSIGNMENT_REJECTED_ERROR_CODE,
            )
        normalized_assignments = _normalize_runtime_job_assignments(assigned_runtime_jobs)
        if runtime_job_id not in normalized_assignments:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
            )
        assigned_task_id = normalized_assignments[runtime_job_id]
        if assigned_task_id is None:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
            )
        if assigned_task_id is not None and envelope.task_id != assigned_task_id:
            return RunnerInboundAckDecision(
                should_ack=True,
                status="rejected",
                error_code=RUNNER_RUNTIME_JOB_NOT_ASSIGNED_ERROR_CODE,
            )
    if envelope.message_type in _RUNNER_CONTROL_UNSUPPORTED_RUNTIME_TYPES:
        return RunnerInboundAckDecision(
            should_ack=True,
            status="failed",
            error_code=RUNNER_DEFERRED_RUNTIME_ERROR_CODE,
        )

    return RunnerInboundAckDecision(
        should_ack=False,
        status=None,
        error_code=None,
    )


def _normalize_runtime_job_assignments(
    assignments: Mapping[str, int | None] | None,
) -> dict[str, int | None]:
    if not assignments:
        return {}
    normalized: dict[str, int | None] = {}
    for runtime_job_id, task_id in assignments.items():
        normalized_runtime_job_id = str(runtime_job_id).strip()
        if not normalized_runtime_job_id:
            continue
        normalized[normalized_runtime_job_id] = task_id
    return normalized


def _build_envelope(
    *,
    tenant_id: int,
    runner_id: str,
    message_type: RunnerMessageType,
    payload: Any,
    protocol_version: str,
    correlation_id: str | None = None,
    runtime_job_id: str | None = None,
    task_id: int | None = None,
) -> RunnerEnvelope:
    message_id = str(uuid.uuid4())
    return RunnerEnvelope(
        message_id=message_id,
        message_type=message_type,
        schema_version=str(protocol_version).strip() or RUNNER_PROTOCOL_SCHEMA_VERSION,
        tenant_id=str(int(tenant_id)),
        runner_id=str(runner_id).strip(),
        correlation_id=correlation_id,
        runtime_job_id=(str(runtime_job_id).strip() or None) if runtime_job_id else None,
        task_id=task_id,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=payload,
        raw_message_type=message_type.value,
    )
