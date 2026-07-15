"""Backend runner protocol validation service for authenticated channel messages.

This module wraps shared `runtime_shared.runner_protocol` DTOs with backend-side
identity and assignment checks. It intentionally avoids transport objects (for
example FastAPI WebSocket instances) and works only with explicit context data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSIONS,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompletePayload,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerRuntimeInputResultPayload,
    RunnerRuntimeLogsResultPayload,
    RunnerRuntimeMetricsResultPayload,
    RunnerRuntimeOperationResultPayload,
    RunnerRuntimeStartupProgressResultPayload,
    RunnerRuntimeStatusResultPayload,
    RunnerRuntimeVpnConfigResultPayload,
    RunnerRuntimeVpnRetryResultPayload,
    RunnerRuntimeVpnStatusResultPayload,
    RunnerTerminalFramePayload,
    RunnerTerminalResultPayload,
    RunnerToolResultPayload,
)


@dataclass(frozen=True, slots=True)
class RunnerChannelIdentity:
    """Authenticated runner channel identity and status snapshot."""

    tenant_id: str
    runner_id: str
    runner_status: str
    credential_status: str


@dataclass(frozen=True, slots=True)
class RunnerRuntimeJobBinding:
    """Tenant/runner/task relationship for an assigned runtime job."""

    runtime_job_id: str
    tenant_id: str
    runner_id: str
    task_id: int | None
    job_type: str | None = None
    workspace_id: str | None = None
    command_id: str | None = None
    task_runtime_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedRunnerMessage:
    """Validated runner envelope enriched with idempotency metadata."""

    envelope: RunnerEnvelope
    idempotency_key: str
    runtime_job_binding: RunnerRuntimeJobBinding | None


class RunnerProtocolValidationError(ValueError):
    """Raised when backend runner-control validation fails."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


RunnerMessageDuplicateChecker = Callable[[str], bool]
RunnerRuntimeJobLookup = Callable[[str], RunnerRuntimeJobBinding | None]
RunnerTaskAssignmentChecker = Callable[[str, str, int], bool]

_REMOTE_RUNTIME_EVENTS_REQUIRING_RUNTIME_JOB = frozenset(
    {
        RunnerMessageType.RUNTIME_STARTED,
        RunnerMessageType.RUNTIME_PAUSED,
        RunnerMessageType.RUNTIME_RESUMED,
        RunnerMessageType.RUNTIME_STOPPED,
        RunnerMessageType.RUNTIME_RETIRED,
        RunnerMessageType.RUNTIME_FAILED,
    }
)
_OUTBOUND_ONLY_CONTROL_MESSAGE_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
        RunnerMessageType.TOOL_COMMAND,
        RunnerMessageType.ARTIFACT_UPLOAD_REQUEST,
    }
)
_RUNNER_ARTIFACT_EVENT_TYPES = frozenset(
    {
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    }
)
_REMOTE_RUNTIME_RUNNER_EVENT_TYPES = frozenset(
    {
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
        RunnerMessageType.TERMINAL_RESULT,
        RunnerMessageType.TERMINAL_FRAME,
    }
)
_RUNNER_RUNTIME_EVENT_TYPES = frozenset(set(_REMOTE_RUNTIME_RUNNER_EVENT_TYPES) | {RunnerMessageType.TOOL_RESULT})
_REMOTE_RUNTIME_EVENT_RESULT_PAYLOAD_TYPES: dict[RunnerMessageType, tuple[type[object], ...]] = {
    RunnerMessageType.RUNTIME_INPUT: (RunnerRuntimeInputResultPayload,),
    RunnerMessageType.RUNTIME_STARTUP_PROGRESS: (RunnerRuntimeStartupProgressResultPayload,),
    RunnerMessageType.RUNTIME_STATUS: (RunnerRuntimeStatusResultPayload,),
    RunnerMessageType.RUNTIME_LOGS: (RunnerRuntimeLogsResultPayload,),
    RunnerMessageType.RUNTIME_METRICS: (RunnerRuntimeMetricsResultPayload,),
    RunnerMessageType.RUNTIME_INVENTORY: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_WORKSPACE_QUERY: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_WORKSPACE_READ: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_WORKSPACE_WRITE: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_VPN_STATUS: (RunnerRuntimeVpnStatusResultPayload,),
    RunnerMessageType.RUNTIME_VPN_RETRY: (RunnerRuntimeVpnRetryResultPayload,),
    RunnerMessageType.RUNTIME_VPN_CONFIG: (RunnerRuntimeVpnConfigResultPayload,),
    RunnerMessageType.RUNTIME_STARTED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_PAUSED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_RESUMED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_STOPPED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_RETIRED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.RUNTIME_FAILED: (RunnerRuntimeOperationResultPayload,),
    RunnerMessageType.TOOL_RESULT: (RunnerToolResultPayload,),
    RunnerMessageType.TERMINAL_RESULT: (RunnerTerminalResultPayload,),
    RunnerMessageType.TERMINAL_FRAME: (RunnerTerminalFramePayload,),
    RunnerMessageType.ARTIFACT_MANIFEST: (RunnerArtifactManifestPayload,),
    RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE: (RunnerArtifactUploadCompletePayload,),
}
_RUNNER_TOOL_RESULT_MAPPING_MAX_BYTES = 64 * 1024


def remote_runtime_event_result_payload_is_valid(envelope: RunnerEnvelope) -> bool:
    """Return whether an inbound runtime/artifact event has the expected result payload shape."""
    expected_payload_types = _REMOTE_RUNTIME_EVENT_RESULT_PAYLOAD_TYPES.get(envelope.message_type)
    return expected_payload_types is None or isinstance(envelope.payload, expected_payload_types)


class RunnerProtocolValidator:
    """Validate authenticated runner envelopes against backend policy rules."""

    def __init__(
        self,
        *,
        supported_schema_versions: set[str] | None = None,
        duplicate_checker: RunnerMessageDuplicateChecker | None = None,
        runtime_job_lookup: RunnerRuntimeJobLookup | None = None,
        task_assignment_checker: RunnerTaskAssignmentChecker | None = None,
    ) -> None:
        self._supported_schema_versions = (
            frozenset(supported_schema_versions)
            if supported_schema_versions is not None
            else frozenset(RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSIONS)
        )
        self._duplicate_checker = duplicate_checker or (lambda _idempotency_key: False)
        self._runtime_job_lookup = runtime_job_lookup or (lambda _runtime_job_id: None)
        self._task_assignment_checker = task_assignment_checker or (
            lambda _tenant_id, _runner_id, _task_id: False
        )

    def validate_inbound_message(
        self,
        *,
        identity: RunnerChannelIdentity,
        envelope: RunnerEnvelope,
    ) -> ValidatedRunnerMessage:
        """Validate one inbound runner message envelope against backend policy."""
        normalized_identity = _normalize_identity(identity)
        self._validate_schema_version(envelope.schema_version)
        self._validate_identity_match(normalized_identity, envelope)
        self._validate_channel_status(normalized_identity)
        self._validate_message_directionality(envelope)

        idempotency_key = self.build_idempotency_key(envelope)
        if self._duplicate_checker(idempotency_key):
            raise RunnerProtocolValidationError(
                error_code="RUNNER_MESSAGE_DUPLICATE",
                message="Runner message already processed for this runner channel identity.",
            )

        runtime_job_binding = self._validate_runtime_job_binding(
            identity=normalized_identity,
            envelope=envelope,
        )
        self._validate_tool_result_binding(
            identity=normalized_identity,
            envelope=envelope,
            runtime_job_binding=runtime_job_binding,
        )
        self._validate_artifact_binding(
            identity=normalized_identity,
            envelope=envelope,
            runtime_job_binding=runtime_job_binding,
        )

        return ValidatedRunnerMessage(
            envelope=envelope,
            idempotency_key=idempotency_key,
            runtime_job_binding=runtime_job_binding,
        )

    @staticmethod
    def build_idempotency_key(envelope: RunnerEnvelope) -> str:
        """Build a stable idempotency key from tenant/runner/message identity."""
        return f"{_normalize_value(envelope.tenant_id)}:{_normalize_value(envelope.runner_id)}:{envelope.message_id}"

    def _validate_schema_version(self, schema_version: str) -> None:
        if schema_version not in self._supported_schema_versions:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_PROTOCOL_UNSUPPORTED",
                message=f"Unsupported runner protocol version `{schema_version}`.",
            )

    @staticmethod
    def _validate_identity_match(identity: RunnerChannelIdentity, envelope: RunnerEnvelope) -> None:
        if _normalize_value(envelope.runner_id) != identity.runner_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_IDENTITY_MISMATCH",
                message="Envelope runner_id does not match authenticated runner identity.",
            )
        if _normalize_value(envelope.tenant_id) != identity.tenant_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TENANT_MISMATCH",
                message="Envelope tenant_id does not match authenticated runner tenant.",
            )

    @staticmethod
    def _validate_channel_status(identity: RunnerChannelIdentity) -> None:
        if identity.runner_status != "active":
            raise RunnerProtocolValidationError(
                error_code="RUNNER_OFFLINE",
                message="Authenticated runner is not active for channel traffic.",
            )
        if identity.credential_status != "active":
            if identity.credential_status == "revoked":
                error_code = "RUNNER_CREDENTIAL_REVOKED"
            elif identity.credential_status == "expired":
                error_code = "RUNNER_CREDENTIAL_EXPIRED"
            else:
                error_code = "RUNNER_AUTH_INVALID"
            raise RunnerProtocolValidationError(
                error_code=error_code,
                message="Authenticated runner credential is not active for channel traffic.",
            )

    def _validate_runtime_job_binding(
        self,
        *,
        identity: RunnerChannelIdentity,
        envelope: RunnerEnvelope,
    ) -> RunnerRuntimeJobBinding | None:
        runtime_job_id = envelope.runtime_job_id
        if runtime_job_id is None:
            if envelope.message_type is RunnerMessageType.TOOL_RESULT:
                raise RunnerProtocolValidationError(
                    error_code="RUNTIME_JOB_NOT_ASSIGNED",
                    message="tooling_plane tool.result requires an assigned tool.command runtime job id.",
                )
            if envelope.message_type in _REMOTE_RUNTIME_EVENTS_REQUIRING_RUNTIME_JOB:
                raise RunnerProtocolValidationError(
                    error_code="RUNTIME_JOB_NOT_ASSIGNED",
                    message=(
                        f"remote_runtime lifecycle event `{envelope.type}` requires an assigned runtime job id."
                    ),
                )
            if envelope.task_id is not None and not self._task_assignment_checker(
                identity.tenant_id,
                identity.runner_id,
                envelope.task_id,
            ):
                raise RunnerProtocolValidationError(
                    error_code="RUNNER_ASSIGNMENT_NOT_FOUND",
                    message="Task is not assigned to the authenticated runner.",
                )
            return None

        binding = self._runtime_job_lookup(runtime_job_id)
        if binding is None:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Runtime job is not assigned to the authenticated runner.",
            )

        normalized_binding = _normalize_runtime_job_binding(binding)
        if normalized_binding.tenant_id != identity.tenant_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Runtime job tenant does not match authenticated runner tenant.",
            )
        if normalized_binding.runner_id != identity.runner_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Runtime job runner does not match authenticated runner identity.",
            )
        if (
            envelope.task_id is not None
            and normalized_binding.task_id != envelope.task_id
            and not _is_stale_runtime_cleanup_binding(
                envelope=envelope,
                binding=normalized_binding,
            )
        ):
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Envelope task_id does not match runtime job assignment.",
            )
        return normalized_binding

    @staticmethod
    def _validate_message_directionality(envelope: RunnerEnvelope) -> None:
        if envelope.message_type in _OUTBOUND_ONLY_CONTROL_MESSAGE_TYPES:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_DIRECTION_INVALID",
                message=(
                    f"Runner-originated `{envelope.type}` is not accepted inbound; "
                    "cloud-originated control messages must not be sent by runners."
                ),
            )

    def _validate_tool_result_binding(
        self,
        *,
        identity: RunnerChannelIdentity,
        envelope: RunnerEnvelope,
        runtime_job_binding: RunnerRuntimeJobBinding | None,
    ) -> None:
        if envelope.message_type is not RunnerMessageType.TOOL_RESULT:
            return
        if not isinstance(envelope.payload, RunnerToolResultPayload):
            raise RunnerProtocolValidationError(
                error_code="RUNNER_PROTOCOL_INVALID",
                message="tooling_plane tool.result requires a typed tool result payload.",
            )
        if runtime_job_binding is None:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="tooling_plane tool.result requires an assigned tool.command runtime job.",
            )
        if envelope.task_id is None:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_TASK_MISMATCH",
                message="tooling_plane tool.result requires envelope task_id for task identity binding.",
            )
        _validate_tool_result_mapping_size(envelope.payload.result, field_name="result")
        _validate_tool_result_mapping_size(envelope.payload.metadata, field_name="metadata")

        job_type = _normalize_optional_value(runtime_job_binding.job_type)
        if job_type != "tool.command":
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Envelope runtime_job_id must reference a `tool.command` runtime job.",
            )

        expected_command_id = _normalize_optional_value(runtime_job_binding.command_id)
        if not expected_command_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_BINDING_INVALID",
                message="tool.command runtime job binding is missing command_id.",
            )
        if envelope.payload.command_id != expected_command_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_COMMAND_ID_MISMATCH",
                message="tool.result command_id does not match tool.command runtime job binding.",
            )

        payload_task_runtime_job_id = _extract_optional_metadata_text(
            envelope.payload.metadata, "task_runtime_job_id"
        )
        expected_task_runtime_job_id = _normalize_optional_value(
            runtime_job_binding.task_runtime_job_id
        )
        if expected_task_runtime_job_id and (
            payload_task_runtime_job_id is not None
            and payload_task_runtime_job_id != expected_task_runtime_job_id
        ):
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_TASK_RUNTIME_MISMATCH",
                message=(
                    "tool.result task_runtime_job_id does not match tool.command runtime job binding."
                ),
            )
        resolved_task_runtime_job_id = (
            expected_task_runtime_job_id or payload_task_runtime_job_id
        )
        if not resolved_task_runtime_job_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_BINDING_INVALID",
                message="tool.result must bind to a task runtime job id.",
            )

        task_runtime_binding = self._runtime_job_lookup(resolved_task_runtime_job_id)
        if task_runtime_binding is None:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id is not assigned to the authenticated runner.",
            )
        normalized_task_binding = _normalize_runtime_job_binding(task_runtime_binding)
        if normalized_task_binding.tenant_id != identity.tenant_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id tenant does not match authenticated runner tenant.",
            )
        if normalized_task_binding.runner_id != identity.runner_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id runner does not match authenticated runner identity.",
            )
        if normalized_task_binding.task_id != runtime_job_binding.task_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_TASK_MISMATCH",
                message="tool.result task binding does not match tool.command runtime job task.",
            )
        task_runtime_job_type = _normalize_optional_value(normalized_task_binding.job_type)
        if task_runtime_job_type != "task.start":
            raise RunnerProtocolValidationError(
                error_code="RUNNER_TOOL_BINDING_INVALID",
                message="task_runtime_job_id must reference an active `task.start` runtime job.",
            )

        command_workspace_id = _normalize_optional_value(runtime_job_binding.workspace_id)
        task_workspace_id = _normalize_optional_value(normalized_task_binding.workspace_id)
        payload_workspace_id = _extract_optional_metadata_text(envelope.payload.metadata, "workspace_id")
        if not command_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.command runtime job binding is missing workspace identity.",
            )
        if not task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="task runtime job binding is missing workspace identity.",
            )
        if not payload_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.result metadata.workspace_id is required for workspace binding.",
            )
        if command_workspace_id != task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.command and task runtime job workspace bindings do not match.",
            )
        if payload_workspace_id != command_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.result workspace_id does not match tool.command runtime job workspace.",
            )
        if payload_workspace_id != task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.result workspace_id does not match task runtime job workspace.",
            )

    def _validate_artifact_binding(
        self,
        *,
        identity: RunnerChannelIdentity,
        envelope: RunnerEnvelope,
        runtime_job_binding: RunnerRuntimeJobBinding | None,
    ) -> None:
        if envelope.message_type not in _RUNNER_ARTIFACT_EVENT_TYPES:
            return
        payload = envelope.payload
        if envelope.message_type is RunnerMessageType.ARTIFACT_MANIFEST:
            if not isinstance(payload, RunnerArtifactManifestPayload):
                raise RunnerProtocolValidationError(
                    error_code="RUNNER_PROTOCOL_INVALID",
                    message="artifact.manifest requires a typed payload.",
                )
        elif envelope.message_type is RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE:
            if not isinstance(payload, RunnerArtifactUploadCompletePayload):
                raise RunnerProtocolValidationError(
                    error_code="RUNNER_PROTOCOL_INVALID",
                    message="artifact.upload.complete requires a typed payload.",
                )
        else:
            return

        if runtime_job_binding is None:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message=f"{envelope.type} requires an assigned tool.command runtime job id.",
            )
        if envelope.task_id is None:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_TASK_MISMATCH",
                message=f"{envelope.type} requires envelope task_id for task identity binding.",
            )

        job_type = _normalize_optional_value(runtime_job_binding.job_type)
        if job_type != "tool.command":
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message=f"{envelope.type} runtime_job_id must reference a `tool.command` runtime job.",
            )

        expected_command_id = _normalize_optional_value(runtime_job_binding.command_id)
        if not expected_command_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="tool.command runtime job binding is missing command_id.",
            )
        if payload.command_id != expected_command_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_COMMAND_ID_MISMATCH",
                message=f"{envelope.type} command_id does not match tool.command runtime job binding.",
            )

        expected_task_runtime_job_id = _normalize_optional_value(runtime_job_binding.task_runtime_job_id)
        if not expected_task_runtime_job_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="tool.command runtime job binding is missing task_runtime_job_id.",
            )
        if payload.task_runtime_job_id != expected_task_runtime_job_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_TASK_RUNTIME_MISMATCH",
                message=f"{envelope.type} task_runtime_job_id does not match tool.command runtime job binding.",
            )

        task_runtime_binding = self._runtime_job_lookup(payload.task_runtime_job_id)
        if task_runtime_binding is None:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id is not assigned to the authenticated runner.",
            )
        normalized_task_binding = _normalize_runtime_job_binding(task_runtime_binding)
        if normalized_task_binding.tenant_id != identity.tenant_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id tenant does not match authenticated runner tenant.",
            )
        if normalized_task_binding.runner_id != identity.runner_id:
            raise RunnerProtocolValidationError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="task_runtime_job_id runner does not match authenticated runner identity.",
            )
        if normalized_task_binding.task_id != runtime_job_binding.task_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_TASK_MISMATCH",
                message=f"{envelope.type} task binding does not match tool.command runtime job task.",
            )
        task_runtime_job_type = _normalize_optional_value(normalized_task_binding.job_type)
        if task_runtime_job_type != "task.start":
            raise RunnerProtocolValidationError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message=f"{envelope.type} task_runtime_job_id must reference an active `task.start` runtime job.",
            )

        command_workspace_id = _normalize_optional_value(runtime_job_binding.workspace_id)
        task_workspace_id = _normalize_optional_value(normalized_task_binding.workspace_id)
        payload_workspace_id = _normalize_optional_value(payload.workspace_id)
        if not command_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.command runtime job binding is missing workspace identity.",
            )
        if not task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="task runtime job binding is missing workspace identity.",
            )
        if not payload_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message=f"{envelope.type} workspace_id is required for workspace binding.",
            )
        if command_workspace_id != task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="tool.command and task runtime job workspace bindings do not match.",
            )
        if payload_workspace_id != command_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message=f"{envelope.type} workspace_id does not match tool.command runtime job workspace.",
            )
        if payload_workspace_id != task_workspace_id:
            raise RunnerProtocolValidationError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message=f"{envelope.type} workspace_id does not match task runtime job workspace.",
            )


def _normalize_identity(identity: RunnerChannelIdentity) -> RunnerChannelIdentity:
    return RunnerChannelIdentity(
        tenant_id=_normalize_value(identity.tenant_id),
        runner_id=_normalize_value(identity.runner_id),
        runner_status=_normalize_status(identity.runner_status),
        credential_status=_normalize_status(identity.credential_status),
    )


def _normalize_runtime_job_binding(binding: RunnerRuntimeJobBinding) -> RunnerRuntimeJobBinding:
    return RunnerRuntimeJobBinding(
        runtime_job_id=_normalize_value(binding.runtime_job_id),
        tenant_id=_normalize_value(binding.tenant_id),
        runner_id=_normalize_value(binding.runner_id),
        task_id=binding.task_id,
        job_type=_normalize_optional_value(binding.job_type),
        workspace_id=_normalize_optional_value(binding.workspace_id),
        command_id=_normalize_optional_value(binding.command_id),
        task_runtime_job_id=_normalize_optional_value(binding.task_runtime_job_id),
    )


def _is_stale_runtime_cleanup_binding(
    *,
    envelope: RunnerEnvelope,
    binding: RunnerRuntimeJobBinding,
) -> bool:
    return (
        envelope.message_type is RunnerMessageType.RUNTIME_RETIRED
        and binding.task_id is None
        and binding.job_type == RunnerMessageType.TASK_RETIRE.value
    )


def _normalize_value(value: str) -> str:
    return str(value).strip()


def _normalize_status(status: str) -> str:
    return str(status).strip().lower()


def _normalize_optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _extract_optional_metadata_text(metadata: object, key: str) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get(key)
    return _normalize_optional_value(value if isinstance(value, str) else None)


def _validate_tool_result_mapping_size(value: Mapping[str, Any], *, field_name: str) -> None:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="replace")
    if len(encoded) > _RUNNER_TOOL_RESULT_MAPPING_MAX_BYTES:
        raise RunnerProtocolValidationError(
            error_code="RUNNER_TOOL_RESULT_PAYLOAD_TOO_LARGE",
            message=(
                f"tool.result {field_name} must be <= {_RUNNER_TOOL_RESULT_MAPPING_MAX_BYTES} bytes."
            ),
        )
