"""Transport-agnostic runner control protocol DTOs and parsing helpers.

This module defines backend-free message contracts for runner/cloud control
envelopes so backend and runner code can share validation and serialization
without importing service, ORM, or framework modules.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.workspace_files import (
    RuntimeWorkspaceDirectory,
    RuntimeWorkspaceFile,
    RuntimeWorkspaceFileError,
    normalize_runtime_workspace_directories,
    normalize_runtime_workspace_files,
)

RUNNER_PROTOCOL_RUNNER_CONTROL_VERSION = "runner_control.v1"
RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION = "remote_runtime.v1"
RUNNER_PROTOCOL_TOOLING_PLANE_VERSION = "tooling_plane.v1"
RUNNER_PROTOCOL_DATA_PLANE_VERSION = "data_plane.v1"
RUNNER_PROTOCOL_SCHEMA_VERSION = RUNNER_PROTOCOL_RUNNER_CONTROL_VERSION
RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE = (
    RUNNER_PROTOCOL_RUNNER_CONTROL_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
)
RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSIONS = frozenset(
    RUNNER_PROTOCOL_ALLOWED_SCHEMA_VERSION_SEQUENCE
)
RUNNER_VERSION_MAX_LENGTH = 128
RUNNER_RUNTIME_IMAGE_MAX_LENGTH = 255
RUNNER_CAPABILITIES_MAX_ITEMS = 32
RUNNER_CAPABILITY_MAX_LENGTH = 64
RUNNER_LABELS_MAX_ITEMS = 32
RUNNER_LABEL_KEY_MAX_LENGTH = 64
RUNNER_LABEL_VALUE_MAX_LENGTH = 256
RUNNER_ACTIVE_RUNTIME_JOBS_MAX_ITEMS = 128
RUNNER_ACTIVE_RUNTIME_JOB_FIELD_MAX_LENGTH = 255
RUNTIME_OPERATION_ID_MAX_LENGTH = 255
RUNTIME_WORKSPACE_ID_MAX_LENGTH = 255
RUNTIME_OPERATION_NAME_MAX_LENGTH = 128
RUNNER_TERMINAL_FRAME_MAX_BYTES = 16 * 1024
RUNTIME_ENVIRONMENT_METADATA_ACTIONS = frozenset({"read", "write", "query"})
RUNTIME_WORKSPACE_CLEANUP_SCOPES = frozenset({"workspace", "runtime", "all"})
RUNTIME_STOP_LIFECYCLE_INTENTS = frozenset({"stop", "cancel"})
RUNNER_TOOL_NAME_MAX_LENGTH = 255
RUNNER_TOOL_COMMAND_ID_MAX_LENGTH = 255
RUNNER_TOOL_BATCH_ID_MAX_LENGTH = 255
RUNNER_TOOL_EXECUTION_STRATEGY_MAX_LENGTH = 128
RUNNER_TOOL_ERROR_CODE_MAX_LENGTH = 128
RUNNER_TOOL_ERROR_MESSAGE_MAX_LENGTH = 2048
RUNNER_TOOL_STDIO_MAX_BYTES = 128 * 1024
RUNNER_TOOL_RESULT_MAX_ARTIFACTS = 256
RUNNER_TOOL_ARTIFACT_PATH_MAX_LENGTH = 2048
RUNNER_TOOL_RESULT_COMPLETED_STATUS = "completed"
RUNNER_TOOL_RESULT_VALID_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "canceled",
        "timed_out",
        RUNNER_TOOL_RESULT_COMPLETED_STATUS,
    }
)
RUNNER_TOOL_RESULT_TERMINAL_VERDICT_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "canceled", "timed_out"}
)


def is_completed_process_tool_result_status(status: object) -> bool:
    """Return True when a tool.result status denotes a finished process, not a tool verdict."""
    return str(status or "").strip().lower() == RUNNER_TOOL_RESULT_COMPLETED_STATUS


def is_terminal_tool_result_verdict_status(status: object) -> bool:
    """Return True when a tool.result status is a terminal tool-domain verdict."""
    return str(status or "").strip().lower() in RUNNER_TOOL_RESULT_TERMINAL_VERDICT_STATUSES
RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS = 256
RUNNER_ARTIFACT_UPLOAD_MAX_ITEMS = 256
RUNNER_ARTIFACT_RELATIVE_PATH_MAX_LENGTH = 2048
RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH = 255
RUNNER_ARTIFACT_KIND_MAX_LENGTH = 128
RUNNER_ARTIFACT_CONTENT_TYPE_MAX_LENGTH = 255
RUNNER_ARTIFACT_OBJECT_KEY_MAX_LENGTH = 2048
RUNNER_ARTIFACT_UPLOAD_URL_MAX_LENGTH = 8192
RUNNER_ARTIFACT_UPLOAD_METHOD_MAX_LENGTH = 16
RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES = 50 * 1024 * 1024 * 1024
RUNNER_ARTIFACT_METADATA_MAX_ITEMS = 64
RUNNER_ARTIFACT_METADATA_MAX_BYTES = 16 * 1024
RUNNER_ARTIFACT_UPLOAD_HEADERS_MAX_ITEMS = 32
RUNNER_ARTIFACT_UPLOAD_HEADER_KEY_MAX_LENGTH = 128
RUNNER_ARTIFACT_UPLOAD_HEADER_VALUE_MAX_LENGTH = 1024

_TOOL_COMMAND_ALLOWED_FIELDS = frozenset(
    {
        "operation_id",
        "workspace_id",
        "task_runtime_job_id",
        "runtime_image",
        "tool",
        "command",
        "cwd",
        "env",
        "command_id",
        "timeout_seconds",
        "timeout_policy",
        "route_policy",
        "delivery_policy",
        "tool_call_id",
        "tool_batch_id",
        "execution_strategy",
        "params",
        "workspace_files",
        "workspace_directories",
    }
)
_TOOL_RESULT_ALLOWED_FIELDS = frozenset(
    {
        "operation_id",
        "command_id",
        "tool",
        "status",
        "success",
        "exit_code",
        "stdout",
        "stderr",
        "artifacts",
        "error_code",
        "error_message",
        "result",
        "metadata",
    }
)
_ARTIFACT_MANIFEST_ALLOWED_FIELDS = frozenset(
    {
        "task_runtime_job_id",
        "command_id",
        "workspace_id",
        "tool_call_id",
        "tool_batch_id",
        "artifacts",
    }
)
_ARTIFACT_MANIFEST_ITEM_ALLOWED_FIELDS = frozenset(
    {
        "artifact_client_id",
        "relative_path",
        "artifact_kind",
        "size_bytes",
        "content_sha256",
        "content_type",
        "is_text",
        "created_at",
        "metadata",
    }
)
_ARTIFACT_UPLOAD_REQUEST_ALLOWED_FIELDS = frozenset(
    {
        "task_runtime_job_id",
        "command_id",
        "workspace_id",
        "tool_call_id",
        "tool_batch_id",
        "uploads",
    }
)
_ARTIFACT_UPLOAD_REQUEST_ITEM_ALLOWED_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_client_id",
        "object_key",
        "upload_url",
        "upload_method",
        "upload_headers",
        "size_bytes",
        "content_sha256",
        "content_type",
        "is_text",
    }
)
_ARTIFACT_UPLOAD_COMPLETE_ALLOWED_FIELDS = frozenset(
    {
        "task_runtime_job_id",
        "command_id",
        "workspace_id",
        "tool_call_id",
        "tool_batch_id",
        "uploads",
    }
)
_ARTIFACT_UPLOAD_COMPLETE_ITEM_ALLOWED_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_client_id",
        "object_key",
        "size_bytes",
        "content_sha256",
        "uploaded_at",
    }
)

_JSON_SAFE_SCALAR_TYPES = (str, int, float, bool, type(None))
_SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "credential",
    "cookie",
    "bearer",
)
_TOOL_RESULT_INLINE_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)"), r"\1<redacted>"),
    (re.compile(r"(?i)((?:set-cookie|cookie)\s*:\s*)([^\r\n]+)"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)\b(password|passwd|token|api[_-]?key|secret|private[_-]?key|access[_-]?key|authorization|cookie)\b(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2<redacted>",
    ),
)
_SHA256_HEX_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class RunnerProtocolValidationError(ValueError):
    """Raised when a runner envelope or payload fails protocol validation."""


class RunnerProtocolUnsupportedSchemaError(RunnerProtocolValidationError):
    """Raised when a message type is sent with an unsupported schema version."""


class RunnerMessageType(str, Enum):
    """Known control-plane message types for runner/cloud coordination."""

    RUNNER_HELLO = "runner.hello"
    RUNNER_HEARTBEAT = "runner.heartbeat"
    RUNNER_ACK = "runner.ack"
    RUNNER_CAPACITY = "runner.capacity"
    RUNNER_DISCONNECTING = "runner.disconnecting"
    CLOUD_ACK = "cloud.ack"
    RUNNER_ASSIGNMENT_PROBE = "runner.assignment.probe"
    RUNNER_CONFIG_UPDATE = "runner.config.update"
    ERROR = "error"
    TASK_START = "task.start"
    TASK_STOP = "task.stop"
    TASK_PAUSE = "task.pause"
    TASK_RESUME = "task.resume"
    TASK_RETIRE = "task.retire"
    RUNTIME_INPUT = "runtime.input"
    RUNTIME_STARTUP_PROGRESS = "runtime.startup_progress"
    RUNTIME_STATUS = "runtime.status"
    RUNTIME_LOGS = "runtime.logs"
    RUNTIME_METRICS = "runtime.metrics"
    RUNTIME_INVENTORY = "runtime.inventory"
    RUNTIME_WORKSPACE_QUERY = "runtime.workspace.query"
    RUNTIME_WORKSPACE_READ = "runtime.workspace.read"
    RUNTIME_WORKSPACE_WRITE = "runtime.workspace.write"
    RUNTIME_WORKSPACE_CLEANUP = "runtime.workspace.cleanup"
    RUNTIME_ARTIFACT_PROMOTE = "runtime.artifact.promote"
    RUNTIME_ENVIRONMENT_METADATA = "runtime.environment.metadata"
    RUNTIME_VPN_STATUS = "runtime.vpn.status"
    RUNTIME_VPN_RETRY = "runtime.vpn.retry"
    RUNTIME_VPN_CONFIG = "runtime.vpn.config"
    RUNTIME_STARTED = "runtime.started"
    RUNTIME_PAUSED = "runtime.paused"
    RUNTIME_RESUMED = "runtime.resumed"
    RUNTIME_STOPPED = "runtime.stopped"
    RUNTIME_RETIRED = "runtime.retired"
    RUNTIME_FAILED = "runtime.failed"
    TOOL_COMMAND = "tool.command"
    TOOL_RESULT = "tool.result"
    TERMINAL_OPEN = "terminal.open"
    TERMINAL_INPUT = "terminal.input"
    TERMINAL_RESIZE = "terminal.resize"
    TERMINAL_CLOSE = "terminal.close"
    TERMINAL_RESULT = "terminal.result"
    TERMINAL_FRAME = "terminal.frame"
    ARTIFACT_MANIFEST = "artifact.manifest"
    ARTIFACT_UPLOAD_REQUEST = "artifact.upload.request"
    ARTIFACT_UPLOAD_COMPLETE = "artifact.upload.complete"
    UNSUPPORTED = "unsupported"

    @classmethod
    def from_wire(cls, value: str) -> RunnerMessageType:
        """Map a wire value to a known message type or `UNSUPPORTED`."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNSUPPORTED


_TOOLING_PLANE_SCHEMA_REQUIRED_MESSAGE_TYPES = frozenset(
    {
        RunnerMessageType.TOOL_COMMAND,
        RunnerMessageType.TOOL_RESULT,
    }
)
_DATA_PLANE_SCHEMA_REQUIRED_MESSAGE_TYPES = frozenset(
    {
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_REQUEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    }
)
_REMOTE_RUNTIME_SCHEMA_REQUIRED_MESSAGE_TYPES = frozenset(
    {
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
_REMOTE_RUNTIME_DUAL_OPERATION_RESULT_MESSAGE_TYPES = frozenset(
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
    }
)
_REMOTE_RUNTIME_RUNTIME_REQUEST_MESSAGE_TYPES = frozenset(
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
        RunnerMessageType.TERMINAL_OPEN,
        RunnerMessageType.TERMINAL_INPUT,
        RunnerMessageType.TERMINAL_RESIZE,
        RunnerMessageType.TERMINAL_CLOSE,
    }
)
_RUNNER_EXECUTABLE_CONTROL_MESSAGE_TYPES = frozenset(
    set(_REMOTE_RUNTIME_RUNTIME_REQUEST_MESSAGE_TYPES)
    | {
        RunnerMessageType.TOOL_COMMAND,
        RunnerMessageType.ARTIFACT_UPLOAD_REQUEST,
    }
)
_RUNNER_EVENT_MESSAGE_TYPES = frozenset(
    set(_REMOTE_RUNTIME_SCHEMA_REQUIRED_MESSAGE_TYPES)
    | set(_REMOTE_RUNTIME_DUAL_OPERATION_RESULT_MESSAGE_TYPES)
    | {
        RunnerMessageType.RUNNER_HELLO,
        RunnerMessageType.RUNNER_HEARTBEAT,
        RunnerMessageType.RUNNER_CAPACITY,
        RunnerMessageType.RUNNER_DISCONNECTING,
        RunnerMessageType.RUNNER_ACK,
        RunnerMessageType.TOOL_RESULT,
        RunnerMessageType.TERMINAL_FRAME,
        RunnerMessageType.ARTIFACT_MANIFEST,
        RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE,
    }
)


@dataclass(frozen=True, slots=True)
class RunnerErrorPayload:
    """Error message payload carried by `type=error` envelopes."""

    error_code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RunnerHelloPayload:
    """Runner hello payload describing version, capabilities, and labels."""

    version: str
    capabilities: tuple[str, ...]
    labels: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RunnerActiveRuntimeJobPayload:
    """Non-secret active runtime summary carried in runner capacity payloads."""

    runtime_job_id: str
    task_id: str
    workspace_id: str
    status: str


@dataclass(frozen=True, slots=True)
class RunnerCapacityPayload:
    """Runner capacity counters used in heartbeat and direct capacity events."""

    active_tasks: int
    max_active_tasks: int
    available_tasks: int
    max_parallel_commands_per_task: int
    docker_available: bool
    runtime_image: str
    runtime_image_available: bool
    version: str
    capabilities: tuple[str, ...]
    labels: Mapping[str, str]
    active_runtime_jobs: tuple[RunnerActiveRuntimeJobPayload, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerHeartbeatPayload:
    """Runner heartbeat payload with liveness and optional capacity snapshot."""

    capacity: RunnerCapacityPayload


@dataclass(frozen=True, slots=True)
class RunnerAckPayload:
    """Generic message acknowledgment payload."""

    acked_message_id: str
    status: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RunnerRuntimeOperationPayload:
    """Shared runtime-operation request payload contract."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerToolCommandPayload:
    """`tool.command` payload contract."""

    operation_id: str
    workspace_id: str
    task_runtime_job_id: str
    runtime_image: str
    tool: str
    command: str
    cwd: str
    env: Mapping[str, str]
    command_id: str
    timeout_seconds: float
    timeout_policy: Mapping[str, Any]
    route_policy: Mapping[str, Any]
    delivery_policy: Mapping[str, Any]
    tool_call_id: str | None
    tool_batch_id: str | None
    execution_strategy: str | None
    params: Mapping[str, Any]
    workspace_files: tuple[RuntimeWorkspaceFile, ...] = ()
    workspace_directories: tuple[RuntimeWorkspaceDirectory, ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerToolResultPayload:
    """`tool.result` payload contract."""

    operation_id: str
    command_id: str
    tool: str
    status: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: tuple[str, ...]
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerArtifactManifestItem:
    """Single artifact entry declared by the runner before upload."""

    artifact_client_id: str
    relative_path: str
    artifact_kind: str
    size_bytes: int
    content_sha256: str
    content_type: str
    is_text: bool
    created_at: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerArtifactManifestPayload:
    """`artifact.manifest` payload contract."""

    task_runtime_job_id: str
    command_id: str
    workspace_id: str
    tool_call_id: str | None
    tool_batch_id: str | None
    artifacts: tuple[RunnerArtifactManifestItem, ...]


@dataclass(frozen=True, slots=True)
class RunnerArtifactUploadRequestItem:
    """Single signed upload instruction sent from cloud to runner."""

    artifact_id: str
    artifact_client_id: str
    object_key: str
    upload_url: str
    upload_method: str
    upload_headers: Mapping[str, str]
    size_bytes: int
    content_sha256: str
    content_type: str
    is_text: bool


@dataclass(frozen=True, slots=True)
class RunnerArtifactUploadRequestPayload:
    """`artifact.upload.request` payload contract."""

    task_runtime_job_id: str
    command_id: str
    workspace_id: str
    tool_call_id: str | None
    tool_batch_id: str | None
    uploads: tuple[RunnerArtifactUploadRequestItem, ...]


@dataclass(frozen=True, slots=True)
class RunnerArtifactUploadCompleteItem:
    """Single upload completion item acknowledged by the runner."""

    artifact_id: str
    artifact_client_id: str
    object_key: str
    size_bytes: int
    content_sha256: str
    uploaded_at: str | None


@dataclass(frozen=True, slots=True)
class RunnerArtifactUploadCompletePayload:
    """`artifact.upload.complete` payload contract."""

    task_runtime_job_id: str
    command_id: str
    workspace_id: str
    tool_call_id: str | None
    tool_batch_id: str | None
    uploads: tuple[RunnerArtifactUploadCompleteItem, ...]


@dataclass(frozen=True, slots=True)
class RunnerTaskStopPayload:
    """`task.stop` payload with required lifecycle intent metadata."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    lifecycle_intent: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalOpenPayload:
    """`terminal.open` request payload contract."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    session_name: str
    cols: int
    rows: int
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalInputPayload:
    """`terminal.input` request payload contract."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    session_id: str
    data: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalResizePayload:
    """`terminal.resize` request payload contract."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    session_id: str
    cols: int
    rows: int
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalClosePayload:
    """`terminal.close` request payload contract."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    session_id: str
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeInventoryPayload:
    """`runtime.inventory` request payload."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    scope: str
    filters: Mapping[str, Any]
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeWorkspaceCleanupPayload:
    """`runtime.workspace.cleanup` request payload."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    cleanup_scope: str
    retain_outputs: bool
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeEnvironmentMetadataPayload:
    """`runtime.environment.metadata` request payload."""

    operation_id: str
    workspace_id: str
    runtime_image: str
    operation: str
    action: str
    key: str | None
    value: Any
    filters: Mapping[str, Any]
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeOperationResultPayload:
    """Shared runtime-operation result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeInputResultPayload:
    """`runtime.input` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeStartupProgressResultPayload:
    """`runtime.startup_progress` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeStatusResultPayload:
    """`runtime.status` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeLogsResultPayload:
    """`runtime.logs` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeMetricsResultPayload:
    """`runtime.metrics` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeVpnStatusResultPayload:
    """`runtime.vpn.status` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeVpnRetryResultPayload:
    """`runtime.vpn.retry` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerRuntimeVpnConfigResultPayload:
    """`runtime.vpn.config` result payload contract."""

    operation_id: str
    status: str
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalResultPayload:
    """`terminal.result` payload contract."""

    operation_id: str
    terminal_operation: str
    session_id: str
    status: str
    sequence: int | None
    error_code: str | None
    error_message: str | None
    result: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RunnerTerminalFramePayload:
    """`terminal.frame` payload contract."""

    session_id: str
    sequence: int
    stream: str
    data: str


RunnerPayload: TypeAlias = (
    RunnerErrorPayload
    | RunnerHelloPayload
    | RunnerHeartbeatPayload
    | RunnerAckPayload
    | RunnerCapacityPayload
    | RunnerRuntimeOperationPayload
    | RunnerToolCommandPayload
    | RunnerToolResultPayload
    | RunnerArtifactManifestPayload
    | RunnerArtifactUploadRequestPayload
    | RunnerArtifactUploadCompletePayload
    | RunnerTaskStopPayload
    | RunnerTerminalOpenPayload
    | RunnerTerminalInputPayload
    | RunnerTerminalResizePayload
    | RunnerTerminalClosePayload
    | RunnerRuntimeInventoryPayload
    | RunnerRuntimeWorkspaceCleanupPayload
    | RunnerRuntimeEnvironmentMetadataPayload
    | RunnerRuntimeOperationResultPayload
    | RunnerRuntimeInputResultPayload
    | RunnerRuntimeStartupProgressResultPayload
    | RunnerRuntimeStatusResultPayload
    | RunnerRuntimeLogsResultPayload
    | RunnerRuntimeMetricsResultPayload
    | RunnerRuntimeVpnStatusResultPayload
    | RunnerRuntimeVpnRetryResultPayload
    | RunnerRuntimeVpnConfigResultPayload
    | RunnerTerminalResultPayload
    | RunnerTerminalFramePayload
    | Mapping[str, Any]
)


@dataclass(frozen=True, slots=True)
class RunnerEnvelope:
    """Typed runner-control envelope shared by backend and runner packages."""

    message_id: str
    message_type: RunnerMessageType
    schema_version: str
    tenant_id: str
    runner_id: str
    correlation_id: str | None
    runtime_job_id: str | None
    task_id: int | None
    created_at: str
    payload: RunnerPayload
    raw_message_type: str

    @property
    def type(self) -> str:
        """Compatibility accessor for docs and wire field naming."""
        if self.message_type is RunnerMessageType.UNSUPPORTED:
            return self.raw_message_type
        return self.message_type.value

    def to_dict(self) -> dict[str, Any]:
        """Serialize the envelope to stable wire field names."""
        payload = _serialize_payload(self.payload)
        _validate_schema_version_for_message_type(
            message_type=self.message_type,
            schema_version=self.schema_version,
            payload=payload,
        )
        _validate_outbound_payload_safety(
            schema_version=self.schema_version,
            message_type=self.message_type,
            payload=payload,
        )
        return {
            "message_id": self.message_id,
            "type": self.type,
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "runner_id": self.runner_id,
            "correlation_id": self.correlation_id,
            "runtime_job_id": self.runtime_job_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "payload": payload,
        }

    def to_json(self) -> str:
        """Serialize the envelope to deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True)


def parse_runner_envelope_json(payload_json: str) -> RunnerEnvelope:
    """Parse a JSON payload into a validated runner envelope."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise RunnerProtocolValidationError("Envelope JSON is not valid.") from exc
    if not isinstance(payload, Mapping):
        raise RunnerProtocolValidationError("Envelope JSON must decode to an object.")
    return parse_runner_envelope(payload)


def parse_runner_envelope(payload: Mapping[str, Any]) -> RunnerEnvelope:
    """Parse and validate a wire envelope mapping into typed DTOs."""
    message_id = _require_non_empty_string(payload, "message_id")
    raw_message_type = _require_non_empty_string(payload, "type")
    schema_version = _require_non_empty_string(payload, "schema_version")
    tenant_id = _normalize_identity(payload, "tenant_id")
    runner_id = _normalize_identity(payload, "runner_id")
    created_at = _require_non_empty_string(payload, "created_at")
    _validate_timestamp(created_at, "created_at")

    message_type = RunnerMessageType.from_wire(raw_message_type)
    runtime_job_id = _optional_string(payload, "runtime_job_id")
    task_id = _optional_int(payload, "task_id")
    _validate_data_plane_envelope_identity_requirements(
        message_type=message_type,
        runtime_job_id=runtime_job_id,
        task_id=task_id,
    )
    payload_mapping = _require_mapping(payload, "payload")
    _validate_schema_version_for_message_type(
        message_type=message_type,
        schema_version=schema_version,
        payload=payload_mapping,
    )
    parsed_payload = _parse_payload(
        message_type,
        payload_mapping,
        schema_version=schema_version,
    )

    return RunnerEnvelope(
        message_id=message_id,
        message_type=message_type,
        schema_version=schema_version,
        tenant_id=tenant_id,
        runner_id=runner_id,
        correlation_id=_optional_string(payload, "correlation_id"),
        runtime_job_id=runtime_job_id,
        task_id=task_id,
        created_at=created_at,
        payload=parsed_payload,
        raw_message_type=raw_message_type,
    )


def serialize_runner_envelope(envelope: RunnerEnvelope) -> dict[str, Any]:
    """Serialize a `RunnerEnvelope` into a JSON-ready dictionary."""
    return envelope.to_dict()


def serialize_runner_envelope_json(envelope: RunnerEnvelope) -> str:
    """Serialize a `RunnerEnvelope` into deterministic JSON."""
    return envelope.to_json()


def requires_remote_runtime_schema_version(
    message_type: RunnerMessageType,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the message type must use the remote runtime schema version."""
    if message_type in _REMOTE_RUNTIME_SCHEMA_REQUIRED_MESSAGE_TYPES:
        return True
    if message_type in _REMOTE_RUNTIME_DUAL_OPERATION_RESULT_MESSAGE_TYPES and payload is not None:
        return _looks_like_runtime_operation_result_payload(payload)
    return False


def requires_tooling_plane_schema_version(message_type: RunnerMessageType) -> bool:
    """Return whether the message type must use the tooling plane schema version."""
    return message_type in _TOOLING_PLANE_SCHEMA_REQUIRED_MESSAGE_TYPES


def requires_data_plane_schema_version(message_type: RunnerMessageType) -> bool:
    """Return whether the message type must use the data plane schema version."""
    return message_type in _DATA_PLANE_SCHEMA_REQUIRED_MESSAGE_TYPES


def is_runner_executable_control_message_type(message_type: RunnerMessageType) -> bool:
    """Return whether a message type is a cloud-originated runner control command."""
    return message_type in _RUNNER_EXECUTABLE_CONTROL_MESSAGE_TYPES


def is_runner_event_message_type(message_type: RunnerMessageType) -> bool:
    """Return whether a message type is a runner-originated event/update signal."""
    return message_type in _RUNNER_EVENT_MESSAGE_TYPES


def sanitize_tool_result_payload_for_persistence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a masked copy of a `tool.result` payload safe for persistence."""
    return mask_durable_secrets(dict(payload), source="runner_tool_result")


def sanitize_log_message(value: str, *, max_chars: int = 200) -> str:
    """Return a single-line log message with secret-like substrings redacted."""
    message = " ".join(str(_sanitize_tool_result_text(value)).split())
    if len(message) > max_chars:
        return message[:max_chars] + "..."
    return message


def sanitize_tool_result_payload_for_transport(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted and size-bounded `tool.result` payload safe for transport."""
    sanitized = sanitize_tool_result_payload_for_persistence(payload)
    stdout_text, stdout_truncated, stdout_original_bytes = _truncate_utf8_text_to_max_bytes(
        str(sanitized.get("stdout") or ""),
        max_bytes=RUNNER_TOOL_STDIO_MAX_BYTES,
    )
    stderr_text, stderr_truncated, stderr_original_bytes = _truncate_utf8_text_to_max_bytes(
        str(sanitized.get("stderr") or ""),
        max_bytes=RUNNER_TOOL_STDIO_MAX_BYTES,
    )
    sanitized["stdout"] = stdout_text
    sanitized["stderr"] = stderr_text

    metadata_value = sanitized.get("metadata")
    metadata_mapping = metadata_value if isinstance(metadata_value, Mapping) else {}
    metadata = dict(metadata_mapping)
    if stdout_truncated:
        metadata["runner_transport_stdout_truncated"] = True
        metadata["runner_transport_stdout_original_bytes"] = stdout_original_bytes
    if stderr_truncated:
        metadata["runner_transport_stderr_truncated"] = True
        metadata["runner_transport_stderr_original_bytes"] = stderr_original_bytes
    sanitized["metadata"] = metadata
    return sanitized


def _parse_payload(
    message_type: RunnerMessageType,
    payload: Mapping[str, Any],
    *,
    schema_version: str,
) -> RunnerPayload:
    if (
        schema_version != RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION
        and message_type in _REMOTE_RUNTIME_RUNTIME_REQUEST_MESSAGE_TYPES
    ):
        return MappingProxyType(dict(payload))
    if message_type is RunnerMessageType.ERROR:
        return _parse_error_payload(payload)
    if message_type is RunnerMessageType.RUNNER_HELLO:
        return _parse_hello_payload(payload)
    if message_type is RunnerMessageType.RUNNER_HEARTBEAT:
        return _parse_heartbeat_payload(payload)
    if message_type is RunnerMessageType.RUNNER_ACK:
        return _parse_ack_payload(payload)
    if message_type is RunnerMessageType.RUNNER_CAPACITY:
        return _parse_capacity_payload(payload)
    if message_type is RunnerMessageType.TASK_STOP:
        return _parse_task_stop_payload(payload)
    if message_type is RunnerMessageType.RUNTIME_INVENTORY:
        return _parse_runtime_inventory_payload(payload)
    if message_type is RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP:
        return _parse_runtime_workspace_cleanup_payload(payload)
    if message_type is RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA:
        return _parse_runtime_environment_metadata_payload(payload)
    if message_type in {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_RETIRE,
    }:
        return _parse_runtime_operation_payload(payload)
    if message_type is RunnerMessageType.TOOL_COMMAND:
        return _parse_tool_command_payload(payload)
    if message_type is RunnerMessageType.TOOL_RESULT:
        return _parse_tool_result_payload(payload)
    if message_type is RunnerMessageType.ARTIFACT_MANIFEST:
        return _parse_artifact_manifest_payload(payload)
    if message_type is RunnerMessageType.ARTIFACT_UPLOAD_REQUEST:
        return _parse_artifact_upload_request_payload(payload)
    if message_type is RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE:
        return _parse_artifact_upload_complete_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_OPEN:
        return _parse_terminal_open_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_INPUT:
        return _parse_terminal_input_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_RESIZE:
        return _parse_terminal_resize_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_CLOSE:
        return _parse_terminal_close_payload(payload)
    if message_type in {
        RunnerMessageType.RUNTIME_INPUT,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        RunnerMessageType.RUNTIME_STATUS,
        RunnerMessageType.RUNTIME_LOGS,
        RunnerMessageType.RUNTIME_METRICS,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE,
        RunnerMessageType.RUNTIME_VPN_STATUS,
        RunnerMessageType.RUNTIME_VPN_RETRY,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
    }:
        return _parse_remote_runtime_dual_operation_or_result_payload(message_type, payload)
    if message_type in {
        RunnerMessageType.RUNTIME_STARTED,
        RunnerMessageType.RUNTIME_PAUSED,
        RunnerMessageType.RUNTIME_RESUMED,
        RunnerMessageType.RUNTIME_STOPPED,
        RunnerMessageType.RUNTIME_RETIRED,
        RunnerMessageType.RUNTIME_FAILED,
    }:
        return _parse_runtime_operation_result_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_RESULT:
        return _parse_terminal_result_payload(payload)
    if message_type is RunnerMessageType.TERMINAL_FRAME:
        return _parse_terminal_frame_payload(payload)
    return MappingProxyType(dict(payload))


def _parse_error_payload(payload: Mapping[str, Any]) -> RunnerErrorPayload:
    return RunnerErrorPayload(
        error_code=_require_non_empty_string(payload, "error_code"),
        message=_require_non_empty_string(payload, "message"),
        retryable=_optional_bool(payload, "retryable"),
    )


def _parse_hello_payload(payload: Mapping[str, Any]) -> RunnerHelloPayload:
    capabilities, labels = _normalize_runner_metadata(payload)
    version = _require_non_empty_string(payload, "version")
    if len(version) > RUNNER_VERSION_MAX_LENGTH:
        raise RunnerProtocolValidationError(
            f"version length must be <= {RUNNER_VERSION_MAX_LENGTH} characters."
        )

    return RunnerHelloPayload(
        version=version,
        capabilities=capabilities,
        labels=labels,
    )


def _parse_capacity_payload(payload: Mapping[str, Any]) -> RunnerCapacityPayload:
    active_tasks = _require_int(payload, "active_tasks")
    max_active_tasks = _require_int(payload, "max_active_tasks")
    available_tasks = _require_int(payload, "available_tasks")
    max_parallel_commands_per_task = _require_int(payload, "max_parallel_commands_per_task")
    runtime_image = _require_non_empty_string(payload, "runtime_image")
    if len(runtime_image) > RUNNER_RUNTIME_IMAGE_MAX_LENGTH:
        raise RunnerProtocolValidationError(
            f"runtime_image length must be <= {RUNNER_RUNTIME_IMAGE_MAX_LENGTH} characters."
        )
    docker_available = _require_bool(payload, "docker_available")
    runtime_image_available = _require_bool(payload, "runtime_image_available")
    version = _require_non_empty_string(payload, "version")
    if len(version) > RUNNER_VERSION_MAX_LENGTH:
        raise RunnerProtocolValidationError(
            f"version length must be <= {RUNNER_VERSION_MAX_LENGTH} characters."
        )

    capabilities, labels = _normalize_runner_metadata(payload)
    active_runtime_jobs = _parse_active_runtime_jobs(payload.get("active_runtime_jobs"))

    if active_tasks < 0 or max_active_tasks < 0 or available_tasks < 0:
        raise RunnerProtocolValidationError("capacity values must be non-negative integers.")
    if max_parallel_commands_per_task < 1:
        raise RunnerProtocolValidationError("max_parallel_commands_per_task must be >= 1.")
    if active_tasks > max_active_tasks:
        raise RunnerProtocolValidationError("active_tasks must be <= max_active_tasks.")
    if available_tasks > max_active_tasks:
        raise RunnerProtocolValidationError("available_tasks must be <= max_active_tasks.")

    return RunnerCapacityPayload(
        active_tasks=active_tasks,
        max_active_tasks=max_active_tasks,
        available_tasks=available_tasks,
        max_parallel_commands_per_task=max_parallel_commands_per_task,
        docker_available=docker_available,
        runtime_image=runtime_image,
        runtime_image_available=runtime_image_available,
        version=version,
        capabilities=capabilities,
        labels=labels,
        active_runtime_jobs=active_runtime_jobs,
    )


def _parse_active_runtime_jobs(raw_value: Any) -> tuple[RunnerActiveRuntimeJobPayload, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise RunnerProtocolValidationError("active_runtime_jobs must be an array when provided.")
    if len(raw_value) > RUNNER_ACTIVE_RUNTIME_JOBS_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"active_runtime_jobs length must be <= {RUNNER_ACTIVE_RUNTIME_JOBS_MAX_ITEMS}."
        )
    items: list[RunnerActiveRuntimeJobPayload] = []
    for raw_item in raw_value:
        if not isinstance(raw_item, Mapping):
            raise RunnerProtocolValidationError("active_runtime_jobs items must be objects.")
        runtime_job_id = _require_bounded_runtime_job_summary_field(raw_item, "runtime_job_id")
        task_id = _require_bounded_runtime_job_summary_field(raw_item, "task_id")
        workspace_id = _require_bounded_runtime_job_summary_field(raw_item, "workspace_id")
        status = _require_bounded_runtime_job_summary_field(raw_item, "status")
        items.append(
            RunnerActiveRuntimeJobPayload(
                runtime_job_id=runtime_job_id,
                task_id=task_id,
                workspace_id=workspace_id,
                status=status,
            )
        )
    return tuple(items)


def _require_bounded_runtime_job_summary_field(payload: Mapping[str, Any], field_name: str) -> str:
    value = _require_non_empty_string(payload, field_name)
    if len(value) > RUNNER_ACTIVE_RUNTIME_JOB_FIELD_MAX_LENGTH:
        raise RunnerProtocolValidationError(
            f"{field_name} length must be <= {RUNNER_ACTIVE_RUNTIME_JOB_FIELD_MAX_LENGTH} characters."
        )
    return value


def _parse_heartbeat_payload(payload: Mapping[str, Any]) -> RunnerHeartbeatPayload:
    capacity_value = payload.get("capacity")
    if not isinstance(capacity_value, Mapping):
        raise RunnerProtocolValidationError("capacity is required and must be an object.")
    capacity = _parse_capacity_payload(capacity_value)
    return RunnerHeartbeatPayload(
        capacity=capacity,
    )


def _parse_ack_payload(payload: Mapping[str, Any]) -> RunnerAckPayload:
    acked_message_id = _require_non_empty_string(payload, "acked_message_id")
    status = _optional_string(payload, "status")
    error_code = _optional_string(payload, "error_code")
    return RunnerAckPayload(
        acked_message_id=acked_message_id,
        status=status,
        error_code=error_code,
    )


def _parse_runtime_operation_payload(
    payload: Mapping[str, Any],
    *,
    allow_sensitive_params: bool = False,
) -> RunnerRuntimeOperationPayload:
    operation_id = _require_non_empty_string(payload, "operation_id")
    workspace_id = _require_non_empty_string(payload, "workspace_id")
    runtime_image = _require_non_empty_string(payload, "runtime_image")
    operation = _require_non_empty_string(payload, "operation")
    params = _require_mapping(payload, "params")
    _validate_runtime_operation_text(operation_id, field_name="operation_id", max_length=RUNTIME_OPERATION_ID_MAX_LENGTH)
    _validate_runtime_operation_text(workspace_id, field_name="workspace_id", max_length=RUNTIME_WORKSPACE_ID_MAX_LENGTH)
    _validate_runtime_operation_text(
        operation,
        field_name="operation",
        max_length=RUNTIME_OPERATION_NAME_MAX_LENGTH,
    )
    _assert_json_safe(params, path="params")
    if not allow_sensitive_params:
        _assert_secret_safe_mapping(params, field_name="params")
    return RunnerRuntimeOperationPayload(
        operation_id=operation_id,
        workspace_id=workspace_id,
        runtime_image=runtime_image,
        operation=operation,
        params=MappingProxyType(dict(params)),
    )


def _parse_tool_command_payload(payload: Mapping[str, Any]) -> RunnerToolCommandPayload:
    _reject_unknown_fields(payload, allowed_fields=_TOOL_COMMAND_ALLOWED_FIELDS, payload_type="tool.command")

    operation_id = _require_non_empty_string(payload, "operation_id")
    workspace_id = _require_non_empty_string(payload, "workspace_id")
    task_runtime_job_id = _require_non_empty_string(payload, "task_runtime_job_id")
    runtime_image = _require_non_empty_string(payload, "runtime_image")
    tool = _require_non_empty_string(payload, "tool")
    command = _require_non_empty_string(payload, "command")
    cwd = _require_non_empty_string(payload, "cwd")
    env = _require_mapping(payload, "env")
    command_id = _require_non_empty_string(payload, "command_id")
    timeout_seconds = _require_positive_number(payload, "timeout_seconds")
    timeout_policy = _require_mapping(payload, "timeout_policy")
    route_policy = _require_mapping(payload, "route_policy")
    delivery_policy = _require_mapping(payload, "delivery_policy")
    tool_call_id = _optional_string(payload, "tool_call_id")
    tool_batch_id = _optional_string(payload, "tool_batch_id")
    execution_strategy = _optional_string(payload, "execution_strategy")
    params = _require_mapping(payload, "params")
    try:
        workspace_files = normalize_runtime_workspace_files(payload.get("workspace_files", ()))
        for workspace_file in workspace_files:
            workspace_file.content_bytes()
        workspace_directories = normalize_runtime_workspace_directories(
            payload.get("workspace_directories", ())
        )
    except RuntimeWorkspaceFileError as exc:
        raise RunnerProtocolValidationError(str(exc)) from exc

    _validate_runtime_operation_text(
        operation_id,
        field_name="operation_id",
        max_length=RUNTIME_OPERATION_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        workspace_id,
        field_name="workspace_id",
        max_length=RUNTIME_WORKSPACE_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        task_runtime_job_id,
        field_name="task_runtime_job_id",
        max_length=RUNTIME_OPERATION_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        tool,
        field_name="tool",
        max_length=RUNNER_TOOL_NAME_MAX_LENGTH,
    )
    _validate_max_utf8_bytes(
        command,
        field_name="command",
        max_bytes=RUNNER_TOOL_STDIO_MAX_BYTES,
    )
    _validate_runtime_operation_text(
        command_id,
        field_name="command_id",
        max_length=RUNNER_TOOL_COMMAND_ID_MAX_LENGTH,
    )
    if tool_batch_id is not None:
        _validate_runtime_operation_text(
            tool_batch_id,
            field_name="tool_batch_id",
            max_length=RUNNER_TOOL_BATCH_ID_MAX_LENGTH,
        )
    if execution_strategy is not None:
        _validate_runtime_operation_text(
            execution_strategy,
            field_name="execution_strategy",
            max_length=RUNNER_TOOL_EXECUTION_STRATEGY_MAX_LENGTH,
        )

    normalized_env = {str(key): str(value) for key, value in env.items()}
    _assert_json_safe(normalized_env, path="env")
    _assert_json_safe(timeout_policy, path="timeout_policy")
    _assert_json_safe(route_policy, path="route_policy")
    _assert_json_safe(delivery_policy, path="delivery_policy")
    _assert_json_safe(params, path="params")
    _assert_secret_safe_mapping(normalized_env, field_name="env")
    _assert_secret_safe_mapping(timeout_policy, field_name="timeout_policy")
    _assert_secret_safe_mapping(route_policy, field_name="route_policy")
    _assert_secret_safe_mapping(delivery_policy, field_name="delivery_policy")
    _assert_secret_safe_mapping(params, field_name="params")

    return RunnerToolCommandPayload(
        operation_id=operation_id,
        workspace_id=workspace_id,
        task_runtime_job_id=task_runtime_job_id,
        runtime_image=runtime_image,
        tool=tool,
        command=command,
        cwd=cwd,
        env=MappingProxyType(normalized_env),
        command_id=command_id,
        timeout_seconds=timeout_seconds,
        timeout_policy=MappingProxyType(dict(timeout_policy)),
        route_policy=MappingProxyType(dict(route_policy)),
        delivery_policy=MappingProxyType(dict(delivery_policy)),
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        execution_strategy=execution_strategy,
        params=MappingProxyType(dict(params)),
        workspace_files=workspace_files,
        workspace_directories=workspace_directories,
    )


def _parse_tool_result_payload(payload: Mapping[str, Any]) -> RunnerToolResultPayload:
    _reject_unknown_fields(payload, allowed_fields=_TOOL_RESULT_ALLOWED_FIELDS, payload_type="tool.result")

    operation_id = _require_non_empty_string(payload, "operation_id")
    command_id = _require_non_empty_string(payload, "command_id")
    tool = _require_non_empty_string(payload, "tool")
    status = _require_non_empty_string(payload, "status").strip().lower()
    success = _require_bool(payload, "success")
    exit_code = _require_int(payload, "exit_code")
    stdout = _require_string(payload, "stdout")
    stderr = _require_string(payload, "stderr")
    artifacts = _require_list(payload, "artifacts")
    error_code = _optional_string(payload, "error_code")
    error_message = _optional_string(payload, "error_message")
    result = _require_mapping(payload, "result")
    metadata = _require_mapping(payload, "metadata")

    _validate_runtime_operation_text(
        operation_id,
        field_name="operation_id",
        max_length=RUNTIME_OPERATION_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        command_id,
        field_name="command_id",
        max_length=RUNNER_TOOL_COMMAND_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        tool,
        field_name="tool",
        max_length=RUNNER_TOOL_NAME_MAX_LENGTH,
    )
    if status not in RUNNER_TOOL_RESULT_VALID_STATUSES:
        allowed = ", ".join(sorted(RUNNER_TOOL_RESULT_VALID_STATUSES))
        raise RunnerProtocolValidationError(
            f"status is required and must be one of: {allowed}."
        )

    _validate_max_utf8_bytes(stdout, field_name="stdout", max_bytes=RUNNER_TOOL_STDIO_MAX_BYTES)
    _validate_max_utf8_bytes(stderr, field_name="stderr", max_bytes=RUNNER_TOOL_STDIO_MAX_BYTES)
    if error_code is not None:
        _validate_runtime_operation_text(
            error_code,
            field_name="error_code",
            max_length=RUNNER_TOOL_ERROR_CODE_MAX_LENGTH,
        )
    if error_message is not None:
        _validate_runtime_operation_text(
            error_message,
            field_name="error_message",
            max_length=RUNNER_TOOL_ERROR_MESSAGE_MAX_LENGTH,
        )

    normalized_artifacts: list[str] = []
    if len(artifacts) > RUNNER_TOOL_RESULT_MAX_ARTIFACTS:
        raise RunnerProtocolValidationError(
            f"artifacts must not exceed {RUNNER_TOOL_RESULT_MAX_ARTIFACTS} entries."
        )
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, str):
            raise RunnerProtocolValidationError("artifacts entries must be non-empty strings.")
        normalized = artifact.strip()
        if not normalized:
            raise RunnerProtocolValidationError("artifacts entries must be non-empty strings.")
        if len(normalized) > RUNNER_TOOL_ARTIFACT_PATH_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"artifacts[{index}] length must be <= {RUNNER_TOOL_ARTIFACT_PATH_MAX_LENGTH} characters."
            )
        normalized_artifacts.append(normalized)

    _assert_json_safe(result, path="result")
    _assert_json_safe(metadata, path="metadata")

    return RunnerToolResultPayload(
        operation_id=operation_id,
        command_id=command_id,
        tool=tool,
        status=status,
        success=success,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        artifacts=tuple(normalized_artifacts),
        error_code=error_code,
        error_message=error_message,
        result=MappingProxyType(dict(result)),
        metadata=MappingProxyType(dict(metadata)),
    )


def _parse_artifact_manifest_payload(payload: Mapping[str, Any]) -> RunnerArtifactManifestPayload:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_MANIFEST_ALLOWED_FIELDS,
        payload_type="artifact.manifest",
    )
    task_runtime_job_id, command_id, workspace_id, tool_call_id, tool_batch_id = _parse_artifact_binding_fields(
        payload,
        payload_type="artifact.manifest",
    )
    artifacts_raw = _require_list(payload, "artifacts")
    if len(artifacts_raw) > RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"artifact.manifest artifacts must not exceed {RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS} entries."
        )
    artifacts: list[RunnerArtifactManifestItem] = []
    for index, artifact_raw in enumerate(artifacts_raw):
        if not isinstance(artifact_raw, Mapping):
            raise RunnerProtocolValidationError(
                f"artifact.manifest artifacts[{index}] must be an object."
            )
        artifacts.append(_parse_artifact_manifest_item(artifact_raw, index=index))

    return RunnerArtifactManifestPayload(
        task_runtime_job_id=task_runtime_job_id,
        command_id=command_id,
        workspace_id=workspace_id,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        artifacts=tuple(artifacts),
    )


def _parse_artifact_manifest_item(
    payload: Mapping[str, Any],
    *,
    index: int,
) -> RunnerArtifactManifestItem:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_MANIFEST_ITEM_ALLOWED_FIELDS,
        payload_type=f"artifact.manifest artifacts[{index}]",
    )
    artifact_client_id = _require_non_empty_string(payload, "artifact_client_id")
    relative_path = _require_non_empty_string(payload, "relative_path")
    artifact_kind = _require_non_empty_string(payload, "artifact_kind")
    size_bytes = _require_int(payload, "size_bytes")
    content_sha256 = _require_non_empty_string(payload, "content_sha256")
    content_type = _require_non_empty_string(payload, "content_type")
    is_text = _require_bool(payload, "is_text")
    created_at = _optional_string(payload, "created_at")
    metadata = _require_mapping(payload, "metadata")

    _validate_runtime_operation_text(
        artifact_client_id,
        field_name="artifact_client_id",
        max_length=RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH,
    )
    normalized_path = _normalize_workspace_relative_path(relative_path, field_name="relative_path")
    _validate_runtime_operation_text(
        artifact_kind,
        field_name="artifact_kind",
        max_length=RUNNER_ARTIFACT_KIND_MAX_LENGTH,
    )
    if size_bytes < 0 or size_bytes > RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES:
        raise RunnerProtocolValidationError(
            f"size_bytes must be between 0 and {RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES}."
        )
    _validate_sha256(content_sha256, field_name="content_sha256")
    _validate_runtime_operation_text(
        content_type,
        field_name="content_type",
        max_length=RUNNER_ARTIFACT_CONTENT_TYPE_MAX_LENGTH,
    )
    if created_at is not None:
        _validate_timestamp(created_at, "created_at")
    normalized_metadata = _normalize_artifact_metadata_map(metadata, field_name="metadata")

    return RunnerArtifactManifestItem(
        artifact_client_id=artifact_client_id,
        relative_path=normalized_path,
        artifact_kind=artifact_kind,
        size_bytes=size_bytes,
        content_sha256=content_sha256.lower(),
        content_type=content_type,
        is_text=is_text,
        created_at=created_at,
        metadata=normalized_metadata,
    )


def _parse_artifact_upload_request_payload(
    payload: Mapping[str, Any],
) -> RunnerArtifactUploadRequestPayload:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_UPLOAD_REQUEST_ALLOWED_FIELDS,
        payload_type="artifact.upload.request",
    )
    task_runtime_job_id, command_id, workspace_id, tool_call_id, tool_batch_id = _parse_artifact_binding_fields(
        payload,
        payload_type="artifact.upload.request",
    )
    uploads_raw = _require_list(payload, "uploads")
    if len(uploads_raw) > RUNNER_ARTIFACT_UPLOAD_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"artifact.upload.request uploads must not exceed {RUNNER_ARTIFACT_UPLOAD_MAX_ITEMS} entries."
        )
    uploads: list[RunnerArtifactUploadRequestItem] = []
    for index, upload_raw in enumerate(uploads_raw):
        if not isinstance(upload_raw, Mapping):
            raise RunnerProtocolValidationError(
                f"artifact.upload.request uploads[{index}] must be an object."
            )
        uploads.append(_parse_artifact_upload_request_item(upload_raw, index=index))

    return RunnerArtifactUploadRequestPayload(
        task_runtime_job_id=task_runtime_job_id,
        command_id=command_id,
        workspace_id=workspace_id,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        uploads=tuple(uploads),
    )


def _parse_artifact_upload_request_item(
    payload: Mapping[str, Any],
    *,
    index: int,
) -> RunnerArtifactUploadRequestItem:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_UPLOAD_REQUEST_ITEM_ALLOWED_FIELDS,
        payload_type=f"artifact.upload.request uploads[{index}]",
    )
    artifact_id = _require_non_empty_string(payload, "artifact_id")
    artifact_client_id = _require_non_empty_string(payload, "artifact_client_id")
    object_key = _require_non_empty_string(payload, "object_key")
    upload_url = _require_non_empty_string(payload, "upload_url")
    upload_method = _require_non_empty_string(payload, "upload_method").upper()
    upload_headers = _require_mapping(payload, "upload_headers")
    size_bytes = _require_int(payload, "size_bytes")
    content_sha256 = _require_non_empty_string(payload, "content_sha256")
    content_type = _require_non_empty_string(payload, "content_type")
    is_text = _require_bool(payload, "is_text")

    _validate_runtime_operation_text(
        artifact_id,
        field_name="artifact_id",
        max_length=RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        artifact_client_id,
        field_name="artifact_client_id",
        max_length=RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        object_key,
        field_name="object_key",
        max_length=RUNNER_ARTIFACT_OBJECT_KEY_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        upload_url,
        field_name="upload_url",
        max_length=RUNNER_ARTIFACT_UPLOAD_URL_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        upload_method,
        field_name="upload_method",
        max_length=RUNNER_ARTIFACT_UPLOAD_METHOD_MAX_LENGTH,
    )
    if size_bytes < 0 or size_bytes > RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES:
        raise RunnerProtocolValidationError(
            f"size_bytes must be between 0 and {RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES}."
        )
    _validate_sha256(content_sha256, field_name="content_sha256")
    _validate_runtime_operation_text(
        content_type,
        field_name="content_type",
        max_length=RUNNER_ARTIFACT_CONTENT_TYPE_MAX_LENGTH,
    )
    normalized_headers = _normalize_upload_headers(upload_headers)

    # Signed upload instructions may include ephemeral credentials in URL/headers.
    _assert_json_safe(upload_headers, path="upload_headers")

    return RunnerArtifactUploadRequestItem(
        artifact_id=artifact_id,
        artifact_client_id=artifact_client_id,
        object_key=object_key,
        upload_url=upload_url,
        upload_method=upload_method,
        upload_headers=normalized_headers,
        size_bytes=size_bytes,
        content_sha256=content_sha256.lower(),
        content_type=content_type,
        is_text=is_text,
    )


def _parse_artifact_upload_complete_payload(
    payload: Mapping[str, Any],
) -> RunnerArtifactUploadCompletePayload:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_UPLOAD_COMPLETE_ALLOWED_FIELDS,
        payload_type="artifact.upload.complete",
    )
    task_runtime_job_id, command_id, workspace_id, tool_call_id, tool_batch_id = _parse_artifact_binding_fields(
        payload,
        payload_type="artifact.upload.complete",
    )
    uploads_raw = _require_list(payload, "uploads")
    if len(uploads_raw) > RUNNER_ARTIFACT_UPLOAD_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"artifact.upload.complete uploads must not exceed {RUNNER_ARTIFACT_UPLOAD_MAX_ITEMS} entries."
        )
    uploads: list[RunnerArtifactUploadCompleteItem] = []
    for index, upload_raw in enumerate(uploads_raw):
        if not isinstance(upload_raw, Mapping):
            raise RunnerProtocolValidationError(
                f"artifact.upload.complete uploads[{index}] must be an object."
            )
        uploads.append(_parse_artifact_upload_complete_item(upload_raw, index=index))

    return RunnerArtifactUploadCompletePayload(
        task_runtime_job_id=task_runtime_job_id,
        command_id=command_id,
        workspace_id=workspace_id,
        tool_call_id=tool_call_id,
        tool_batch_id=tool_batch_id,
        uploads=tuple(uploads),
    )


def _parse_artifact_upload_complete_item(
    payload: Mapping[str, Any],
    *,
    index: int,
) -> RunnerArtifactUploadCompleteItem:
    _reject_unknown_fields(
        payload,
        allowed_fields=_ARTIFACT_UPLOAD_COMPLETE_ITEM_ALLOWED_FIELDS,
        payload_type=f"artifact.upload.complete uploads[{index}]",
    )
    artifact_id = _require_non_empty_string(payload, "artifact_id")
    artifact_client_id = _require_non_empty_string(payload, "artifact_client_id")
    object_key = _require_non_empty_string(payload, "object_key")
    size_bytes = _require_int(payload, "size_bytes")
    content_sha256 = _require_non_empty_string(payload, "content_sha256")
    uploaded_at = _optional_string(payload, "uploaded_at")

    _validate_runtime_operation_text(
        artifact_id,
        field_name="artifact_id",
        max_length=RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        artifact_client_id,
        field_name="artifact_client_id",
        max_length=RUNNER_ARTIFACT_CLIENT_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        object_key,
        field_name="object_key",
        max_length=RUNNER_ARTIFACT_OBJECT_KEY_MAX_LENGTH,
    )
    if size_bytes < 0 or size_bytes > RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES:
        raise RunnerProtocolValidationError(
            f"size_bytes must be between 0 and {RUNNER_ARTIFACT_UPLOAD_ITEM_MAX_SIZE_BYTES}."
        )
    _validate_sha256(content_sha256, field_name="content_sha256")
    if uploaded_at is not None:
        _validate_timestamp(uploaded_at, "uploaded_at")

    return RunnerArtifactUploadCompleteItem(
        artifact_id=artifact_id,
        artifact_client_id=artifact_client_id,
        object_key=object_key,
        size_bytes=size_bytes,
        content_sha256=content_sha256.lower(),
        uploaded_at=uploaded_at,
    )


def _parse_artifact_binding_fields(
    payload: Mapping[str, Any],
    *,
    payload_type: str,
) -> tuple[str, str, str, str | None, str | None]:
    task_runtime_job_id = _require_non_empty_string(payload, "task_runtime_job_id")
    command_id = _require_non_empty_string(payload, "command_id")
    workspace_id = _require_non_empty_string(payload, "workspace_id")
    tool_call_id = _optional_string(payload, "tool_call_id")
    tool_batch_id = _optional_string(payload, "tool_batch_id")

    _validate_runtime_operation_text(
        task_runtime_job_id,
        field_name=f"{payload_type}.task_runtime_job_id",
        max_length=RUNTIME_OPERATION_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        command_id,
        field_name=f"{payload_type}.command_id",
        max_length=RUNNER_TOOL_COMMAND_ID_MAX_LENGTH,
    )
    _validate_runtime_operation_text(
        workspace_id,
        field_name=f"{payload_type}.workspace_id",
        max_length=RUNTIME_WORKSPACE_ID_MAX_LENGTH,
    )
    if tool_batch_id is not None:
        _validate_runtime_operation_text(
            tool_batch_id,
            field_name=f"{payload_type}.tool_batch_id",
            max_length=RUNNER_TOOL_BATCH_ID_MAX_LENGTH,
        )
    return task_runtime_job_id, command_id, workspace_id, tool_call_id, tool_batch_id


def _parse_runtime_vpn_config_operation_payload(payload: Mapping[str, Any]) -> RunnerRuntimeOperationPayload:
    """Allow secret-like VPN material in params.vpn_config for inbound requests only."""
    base_payload = _parse_runtime_operation_payload(payload, allow_sensitive_params=True)
    params = dict(base_payload.params)

    vpn_config = params.get("vpn_config")
    if vpn_config is not None and not isinstance(vpn_config, Mapping):
        raise RunnerProtocolValidationError("params.vpn_config must be an object when provided.")

    safe_params = {
        key: value
        for key, value in params.items()
        if str(key).strip().lower() != "vpn_config"
    }
    _assert_secret_safe_mapping(safe_params, field_name="params")
    return base_payload


def _parse_task_stop_payload(payload: Mapping[str, Any]) -> RunnerTaskStopPayload:
    base_payload = _parse_runtime_operation_payload(payload)
    lifecycle_intent_raw = base_payload.params.get("lifecycle_intent")
    if not isinstance(lifecycle_intent_raw, str):
        raise RunnerProtocolValidationError(
            "params.lifecycle_intent is required and must be one of: stop, cancel."
        )
    lifecycle_intent = lifecycle_intent_raw.strip().lower()
    if lifecycle_intent not in RUNTIME_STOP_LIFECYCLE_INTENTS:
        raise RunnerProtocolValidationError(
            "params.lifecycle_intent is required and must be one of: stop, cancel."
        )
    return RunnerTaskStopPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        lifecycle_intent=lifecycle_intent,
        params=base_payload.params,
    )


def _parse_runtime_inventory_payload(payload: Mapping[str, Any]) -> RunnerPayload:
    if _looks_like_runtime_operation_result_payload(payload):
        return _parse_runtime_operation_result_payload(payload)

    base_payload = _parse_runtime_operation_payload(payload)
    scope_raw = base_payload.params.get("scope", "task")
    if not isinstance(scope_raw, str):
        raise RunnerProtocolValidationError("params.scope must be a string when provided.")
    scope = scope_raw.strip() or "task"
    filters_value = base_payload.params.get("filters", {})
    if not isinstance(filters_value, Mapping):
        raise RunnerProtocolValidationError("params.filters must be an object when provided.")
    _assert_json_safe(filters_value, path="params.filters")
    return RunnerRuntimeInventoryPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        scope=scope,
        filters=MappingProxyType(dict(filters_value)),
        params=base_payload.params,
    )


def _parse_runtime_workspace_cleanup_payload(payload: Mapping[str, Any]) -> RunnerPayload:
    if _looks_like_runtime_operation_result_payload(payload):
        return _parse_runtime_operation_result_payload(payload)

    base_payload = _parse_runtime_operation_payload(payload)
    cleanup_scope_raw = base_payload.params.get("cleanup_scope")
    if not isinstance(cleanup_scope_raw, str):
        raise RunnerProtocolValidationError(
            "params.cleanup_scope is required and must be one of: workspace, runtime, all."
        )
    cleanup_scope = cleanup_scope_raw.strip().lower()
    if cleanup_scope not in RUNTIME_WORKSPACE_CLEANUP_SCOPES:
        raise RunnerProtocolValidationError(
            "params.cleanup_scope is required and must be one of: workspace, runtime, all."
        )
    retain_outputs = base_payload.params.get("retain_outputs")
    if not isinstance(retain_outputs, bool):
        raise RunnerProtocolValidationError("params.retain_outputs is required and must be a boolean.")
    return RunnerRuntimeWorkspaceCleanupPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        cleanup_scope=cleanup_scope,
        retain_outputs=retain_outputs,
        params=base_payload.params,
    )


def _parse_runtime_environment_metadata_payload(payload: Mapping[str, Any]) -> RunnerPayload:
    if _looks_like_runtime_operation_result_payload(payload):
        return _parse_runtime_operation_result_payload(payload)

    base_payload = _parse_runtime_operation_payload(payload)
    action_raw = base_payload.params.get("action")
    if not isinstance(action_raw, str):
        raise RunnerProtocolValidationError("params.action is required and must be one of: read, write, query.")
    action = action_raw.strip().lower()
    if action not in RUNTIME_ENVIRONMENT_METADATA_ACTIONS:
        raise RunnerProtocolValidationError("params.action is required and must be one of: read, write, query.")
    key = _optional_string(base_payload.params, "key")
    value = base_payload.params.get("value")
    filters_value = base_payload.params.get("filters", {})
    if not isinstance(filters_value, Mapping):
        raise RunnerProtocolValidationError("params.filters must be an object when provided.")
    if action in {"read", "write"} and key is None:
        raise RunnerProtocolValidationError("params.key is required for action=read and action=write.")
    if action == "write" and "value" not in base_payload.params:
        raise RunnerProtocolValidationError("params.value is required for action=write.")
    if "value" in base_payload.params:
        _assert_json_safe(value, path="params.value")
    _assert_json_safe(filters_value, path="params.filters")
    return RunnerRuntimeEnvironmentMetadataPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        action=action,
        key=key,
        value=value,
        filters=MappingProxyType(dict(filters_value)),
        params=base_payload.params,
    )


def _parse_terminal_open_payload(payload: Mapping[str, Any]) -> RunnerTerminalOpenPayload:
    base_payload = _parse_runtime_operation_payload(payload)
    session_name = _require_non_empty_string(base_payload.params, "session_name")
    cols = _require_int(base_payload.params, "cols")
    rows = _require_int(base_payload.params, "rows")
    if cols < 1:
        raise RunnerProtocolValidationError("params.cols is required and must be >= 1.")
    if rows < 1:
        raise RunnerProtocolValidationError("params.rows is required and must be >= 1.")
    return RunnerTerminalOpenPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        session_name=session_name,
        cols=cols,
        rows=rows,
        params=base_payload.params,
    )


def _parse_terminal_input_payload(payload: Mapping[str, Any]) -> RunnerTerminalInputPayload:
    base_payload = _parse_runtime_operation_payload(payload)
    session_id = _require_non_empty_string(base_payload.params, "session_id")
    data = base_payload.params.get("data")
    if not isinstance(data, str):
        raise RunnerProtocolValidationError("params.data is required and must be a string.")
    return RunnerTerminalInputPayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        session_id=session_id,
        data=data,
        params=base_payload.params,
    )


def _parse_terminal_resize_payload(payload: Mapping[str, Any]) -> RunnerTerminalResizePayload:
    base_payload = _parse_runtime_operation_payload(payload)
    session_id = _require_non_empty_string(base_payload.params, "session_id")
    cols = _require_int(base_payload.params, "cols")
    rows = _require_int(base_payload.params, "rows")
    if cols < 1:
        raise RunnerProtocolValidationError("params.cols is required and must be >= 1.")
    if rows < 1:
        raise RunnerProtocolValidationError("params.rows is required and must be >= 1.")
    return RunnerTerminalResizePayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        session_id=session_id,
        cols=cols,
        rows=rows,
        params=base_payload.params,
    )


def _parse_terminal_close_payload(payload: Mapping[str, Any]) -> RunnerTerminalClosePayload:
    base_payload = _parse_runtime_operation_payload(payload)
    session_id = _require_non_empty_string(base_payload.params, "session_id")
    return RunnerTerminalClosePayload(
        operation_id=base_payload.operation_id,
        workspace_id=base_payload.workspace_id,
        runtime_image=base_payload.runtime_image,
        operation=base_payload.operation,
        session_id=session_id,
        params=base_payload.params,
    )


def _parse_runtime_operation_result_payload(payload: Mapping[str, Any]) -> RunnerRuntimeOperationResultPayload:
    operation_id = _require_non_empty_string(payload, "operation_id")
    status = _require_non_empty_string(payload, "status")
    result = _require_mapping(payload, "result")
    error_code = _optional_string(payload, "error_code")
    error_message = _optional_string(payload, "error_message")

    _validate_runtime_operation_text(operation_id, field_name="operation_id", max_length=RUNTIME_OPERATION_ID_MAX_LENGTH)
    _assert_json_safe(result, path="result")
    _assert_secret_safe_mapping(result, field_name="result")
    return RunnerRuntimeOperationResultPayload(
        operation_id=operation_id,
        status=status.strip().lower(),
        error_code=error_code,
        error_message=error_message,
        result=MappingProxyType(dict(result)),
    )


def _parse_remote_runtime_dual_operation_or_result_payload(
    message_type: RunnerMessageType,
    payload: Mapping[str, Any],
) -> RunnerPayload:
    if not _looks_like_runtime_operation_result_payload(payload):
        if message_type is RunnerMessageType.RUNTIME_VPN_CONFIG:
            return _parse_runtime_vpn_config_operation_payload(payload)
        return _parse_runtime_operation_payload(payload)

    base_result = _parse_runtime_operation_result_payload(payload)
    if message_type is RunnerMessageType.RUNTIME_INPUT:
        return RunnerRuntimeInputResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_STARTUP_PROGRESS:
        return RunnerRuntimeStartupProgressResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_STATUS:
        return RunnerRuntimeStatusResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_LOGS:
        return RunnerRuntimeLogsResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_METRICS:
        return RunnerRuntimeMetricsResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_VPN_STATUS:
        return RunnerRuntimeVpnStatusResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type is RunnerMessageType.RUNTIME_VPN_RETRY:
        return RunnerRuntimeVpnRetryResultPayload(
            operation_id=base_result.operation_id,
            status=base_result.status,
            error_code=base_result.error_code,
            error_message=base_result.error_message,
            result=base_result.result,
        )
    if message_type in {
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
    }:
        return base_result
    return RunnerRuntimeVpnConfigResultPayload(
        operation_id=base_result.operation_id,
        status=base_result.status,
        error_code=base_result.error_code,
        error_message=base_result.error_message,
        result=base_result.result,
    )


def _parse_terminal_result_payload(payload: Mapping[str, Any]) -> RunnerTerminalResultPayload:
    operation_id = _require_non_empty_string(payload, "operation_id")
    terminal_operation = _require_non_empty_string(payload, "terminal_operation").strip().lower()
    if terminal_operation not in {"open", "input", "resize", "close"}:
        raise RunnerProtocolValidationError(
            "terminal_operation is required and must be one of: open, input, resize, close."
        )
    session_id = _require_non_empty_string(payload, "session_id")
    status = _require_non_empty_string(payload, "status")
    sequence = _optional_int(payload, "sequence")
    if sequence is not None and sequence < 0:
        raise RunnerProtocolValidationError("sequence must be >= 0 when provided.")
    result = _require_mapping(payload, "result")
    error_code = _optional_string(payload, "error_code")
    error_message = _optional_string(payload, "error_message")
    _validate_runtime_operation_text(operation_id, field_name="operation_id", max_length=RUNTIME_OPERATION_ID_MAX_LENGTH)
    _assert_json_safe(result, path="result")
    _assert_secret_safe_mapping(result, field_name="result")
    return RunnerTerminalResultPayload(
        operation_id=operation_id,
        terminal_operation=terminal_operation,
        session_id=session_id,
        status=status.strip().lower(),
        sequence=sequence,
        error_code=error_code,
        error_message=error_message,
        result=MappingProxyType(dict(result)),
    )


def _parse_terminal_frame_payload(payload: Mapping[str, Any]) -> RunnerTerminalFramePayload:
    session_id = _require_non_empty_string(payload, "session_id")
    sequence = _require_int(payload, "sequence")
    if sequence < 0:
        raise RunnerProtocolValidationError("sequence must be >= 0.")
    stream = _require_non_empty_string(payload, "stream")
    data_value = payload.get("data")
    if not isinstance(data_value, str):
        raise RunnerProtocolValidationError("data is required and must be a string.")
    data_size = len(data_value.encode("utf-8", errors="replace"))
    if data_size > RUNNER_TERMINAL_FRAME_MAX_BYTES:
        raise RunnerProtocolValidationError(
            f"terminal.frame data must be <= {RUNNER_TERMINAL_FRAME_MAX_BYTES} bytes."
        )
    return RunnerTerminalFramePayload(
        session_id=session_id,
        sequence=sequence,
        stream=stream.strip().lower(),
        data=data_value,
    )


def _serialize_payload(payload: RunnerPayload) -> Any:
    if isinstance(payload, RunnerErrorPayload):
        return {
            "error_code": payload.error_code,
            "message": payload.message,
            "retryable": payload.retryable,
        }
    if isinstance(payload, RunnerHelloPayload):
        return {
            "version": payload.version,
            "capabilities": list(payload.capabilities),
            "labels": dict(payload.labels),
        }
    if isinstance(payload, RunnerCapacityPayload):
        return {
            "active_tasks": payload.active_tasks,
            "max_active_tasks": payload.max_active_tasks,
            "available_tasks": payload.available_tasks,
            "max_parallel_commands_per_task": payload.max_parallel_commands_per_task,
            "docker_available": payload.docker_available,
            "runtime_image": payload.runtime_image,
            "runtime_image_available": payload.runtime_image_available,
            "version": payload.version,
            "capabilities": list(payload.capabilities),
            "labels": dict(payload.labels),
            "active_runtime_jobs": [
                {
                    "runtime_job_id": item.runtime_job_id,
                    "task_id": item.task_id,
                    "workspace_id": item.workspace_id,
                    "status": item.status,
                }
                for item in payload.active_runtime_jobs
            ],
        }
    if isinstance(payload, RunnerHeartbeatPayload):
        return {
            "capacity": _serialize_payload(payload.capacity),
        }
    if isinstance(payload, RunnerAckPayload):
        return {
            "acked_message_id": payload.acked_message_id,
            "status": payload.status,
            "error_code": payload.error_code,
        }
    if isinstance(payload, RunnerToolCommandPayload):
        return {
            "operation_id": payload.operation_id,
            "workspace_id": payload.workspace_id,
            "task_runtime_job_id": payload.task_runtime_job_id,
            "runtime_image": payload.runtime_image,
            "tool": payload.tool,
            "command": payload.command,
            "cwd": payload.cwd,
            "env": dict(payload.env),
            "command_id": payload.command_id,
            "timeout_seconds": payload.timeout_seconds,
            "timeout_policy": dict(payload.timeout_policy),
            "route_policy": dict(payload.route_policy),
            "delivery_policy": dict(payload.delivery_policy),
            "tool_call_id": payload.tool_call_id,
            "tool_batch_id": payload.tool_batch_id,
            "execution_strategy": payload.execution_strategy,
            "params": dict(payload.params),
        }
    if isinstance(payload, RunnerToolResultPayload):
        return {
            "operation_id": payload.operation_id,
            "command_id": payload.command_id,
            "tool": payload.tool,
            "status": payload.status,
            "success": payload.success,
            "exit_code": payload.exit_code,
            "stdout": payload.stdout,
            "stderr": payload.stderr,
            "artifacts": list(payload.artifacts),
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "result": dict(payload.result),
            "metadata": dict(payload.metadata),
        }
    if isinstance(payload, RunnerArtifactManifestPayload):
        return {
            "task_runtime_job_id": payload.task_runtime_job_id,
            "command_id": payload.command_id,
            "workspace_id": payload.workspace_id,
            "tool_call_id": payload.tool_call_id,
            "tool_batch_id": payload.tool_batch_id,
            "artifacts": [
                {
                    "artifact_client_id": item.artifact_client_id,
                    "relative_path": item.relative_path,
                    "artifact_kind": item.artifact_kind,
                    "size_bytes": item.size_bytes,
                    "content_sha256": item.content_sha256,
                    "content_type": item.content_type,
                    "is_text": item.is_text,
                    "created_at": item.created_at,
                    "metadata": dict(item.metadata),
                }
                for item in payload.artifacts
            ],
        }
    if isinstance(payload, RunnerArtifactUploadRequestPayload):
        return {
            "task_runtime_job_id": payload.task_runtime_job_id,
            "command_id": payload.command_id,
            "workspace_id": payload.workspace_id,
            "tool_call_id": payload.tool_call_id,
            "tool_batch_id": payload.tool_batch_id,
            "uploads": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_client_id": item.artifact_client_id,
                    "object_key": item.object_key,
                    "upload_url": item.upload_url,
                    "upload_method": item.upload_method,
                    "upload_headers": dict(item.upload_headers),
                    "size_bytes": item.size_bytes,
                    "content_sha256": item.content_sha256,
                    "content_type": item.content_type,
                    "is_text": item.is_text,
                }
                for item in payload.uploads
            ],
        }
    if isinstance(payload, RunnerArtifactUploadCompletePayload):
        return {
            "task_runtime_job_id": payload.task_runtime_job_id,
            "command_id": payload.command_id,
            "workspace_id": payload.workspace_id,
            "tool_call_id": payload.tool_call_id,
            "tool_batch_id": payload.tool_batch_id,
            "uploads": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_client_id": item.artifact_client_id,
                    "object_key": item.object_key,
                    "size_bytes": item.size_bytes,
                    "content_sha256": item.content_sha256,
                    "uploaded_at": item.uploaded_at,
                }
                for item in payload.uploads
            ],
        }
    if isinstance(
        payload,
        (
            RunnerRuntimeOperationPayload,
            RunnerTaskStopPayload,
            RunnerTerminalOpenPayload,
            RunnerTerminalInputPayload,
            RunnerTerminalResizePayload,
            RunnerTerminalClosePayload,
            RunnerRuntimeInventoryPayload,
            RunnerRuntimeWorkspaceCleanupPayload,
            RunnerRuntimeEnvironmentMetadataPayload,
        ),
    ):
        return {
            "operation_id": payload.operation_id,
            "workspace_id": payload.workspace_id,
            "runtime_image": payload.runtime_image,
            "operation": payload.operation,
            "params": dict(payload.params),
        }
    if isinstance(
        payload,
        (
            RunnerRuntimeInputResultPayload,
            RunnerRuntimeStartupProgressResultPayload,
            RunnerRuntimeStatusResultPayload,
            RunnerRuntimeLogsResultPayload,
            RunnerRuntimeMetricsResultPayload,
            RunnerRuntimeVpnStatusResultPayload,
            RunnerRuntimeVpnRetryResultPayload,
            RunnerRuntimeVpnConfigResultPayload,
        ),
    ):
        return {
            "operation_id": payload.operation_id,
            "status": payload.status,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "result": dict(payload.result),
        }
    if isinstance(payload, RunnerRuntimeOperationResultPayload):
        return {
            "operation_id": payload.operation_id,
            "status": payload.status,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "result": dict(payload.result),
        }
    if isinstance(payload, RunnerTerminalResultPayload):
        return {
            "operation_id": payload.operation_id,
            "terminal_operation": payload.terminal_operation,
            "session_id": payload.session_id,
            "status": payload.status,
            "sequence": payload.sequence,
            "error_code": payload.error_code,
            "error_message": payload.error_message,
            "result": dict(payload.result),
        }
    if isinstance(payload, RunnerTerminalFramePayload):
        return {
            "session_id": payload.session_id,
            "sequence": payload.sequence,
            "stream": payload.stream,
            "data": payload.data,
        }
    return dict(payload)


def _validate_data_plane_envelope_identity_requirements(
    *,
    message_type: RunnerMessageType,
    runtime_job_id: str | None,
    task_id: int | None,
) -> None:
    if not requires_data_plane_schema_version(message_type):
        return
    if runtime_job_id is None:
        raise RunnerProtocolValidationError("runtime_job_id is required for data_plane artifact messages.")
    if task_id is None:
        raise RunnerProtocolValidationError("task_id is required for data_plane artifact messages.")
    _validate_runtime_operation_text(
        runtime_job_id,
        field_name="runtime_job_id",
        max_length=RUNTIME_OPERATION_ID_MAX_LENGTH,
    )


def _normalize_workspace_relative_path(path: str, *, field_name: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if len(normalized) > RUNNER_ARTIFACT_RELATIVE_PATH_MAX_LENGTH:
        raise RunnerProtocolValidationError(
            f"{field_name} length must be <= {RUNNER_ARTIFACT_RELATIVE_PATH_MAX_LENGTH} characters."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise RunnerProtocolValidationError(f"{field_name} must not include control characters.")
    if normalized == "/workspace":
        raise RunnerProtocolValidationError(f"{field_name} must not be the workspace root path.")
    if normalized.startswith("/workspace/"):
        normalized = normalized[len("/workspace/") :]
    elif normalized.startswith("/"):
        raise RunnerProtocolValidationError(
            f"{field_name} must be workspace-relative or under /workspace/."
        )

    parts: list[str] = []
    for segment in normalized.split("/"):
        candidate = segment.strip()
        if not candidate or candidate == ".":
            continue
        if candidate == "..":
            raise RunnerProtocolValidationError(
                f"{field_name} must remain within workspace-relative boundaries."
            )
        parts.append(candidate)
    if not parts:
        raise RunnerProtocolValidationError(f"{field_name} must resolve to a file path.")
    return "/".join(parts)


def _normalize_artifact_metadata_map(
    metadata: Mapping[str, Any],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if len(metadata) > RUNNER_ARTIFACT_METADATA_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"{field_name} must not exceed {RUNNER_ARTIFACT_METADATA_MAX_ITEMS} entries."
        )
    normalized: dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise RunnerProtocolValidationError(f"{field_name} keys must be strings.")
        normalized_key = key.strip()
        if not normalized_key:
            raise RunnerProtocolValidationError(f"{field_name} keys must be non-empty strings.")
        normalized[normalized_key] = value
    _assert_json_safe(normalized, path=field_name)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > RUNNER_ARTIFACT_METADATA_MAX_BYTES:
        raise RunnerProtocolValidationError(
            f"{field_name} must be <= {RUNNER_ARTIFACT_METADATA_MAX_BYTES} bytes."
        )
    return MappingProxyType(normalized)


def _normalize_upload_headers(headers: Mapping[str, Any]) -> Mapping[str, str]:
    if len(headers) > RUNNER_ARTIFACT_UPLOAD_HEADERS_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"upload_headers must not exceed {RUNNER_ARTIFACT_UPLOAD_HEADERS_MAX_ITEMS} entries."
        )
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str):
            raise RunnerProtocolValidationError("upload_headers keys must be strings.")
        header_name = key.strip()
        if not header_name:
            raise RunnerProtocolValidationError("upload_headers keys must be non-empty strings.")
        if len(header_name) > RUNNER_ARTIFACT_UPLOAD_HEADER_KEY_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"upload_headers keys must be <= {RUNNER_ARTIFACT_UPLOAD_HEADER_KEY_MAX_LENGTH} characters."
            )
        header_value = str(value)
        if len(header_value) > RUNNER_ARTIFACT_UPLOAD_HEADER_VALUE_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"upload_headers values must be <= {RUNNER_ARTIFACT_UPLOAD_HEADER_VALUE_MAX_LENGTH} characters."
            )
        normalized[header_name] = header_value
    return MappingProxyType(normalized)


def _validate_sha256(value: str, *, field_name: str) -> None:
    if not _SHA256_HEX_RE.match(value.strip()):
        raise RunnerProtocolValidationError(f"{field_name} must be a lowercase or uppercase SHA-256 hex digest.")


def _optional_string(payload: Mapping[str, Any], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    raise RunnerProtocolValidationError(f"{field_name} must be a string when provided.")


def _require_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise RunnerProtocolValidationError(f"{field_name} is required and must be a string.")
    return value


def _normalize_identity(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    raise RunnerProtocolValidationError(f"{field_name} is required and must be a non-empty string.")


def _optional_int(payload: Mapping[str, Any], field_name: str) -> int | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise RunnerProtocolValidationError(f"{field_name} must be an integer when provided.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise RunnerProtocolValidationError(f"{field_name} must be an integer when provided.")


def _require_int(payload: Mapping[str, Any], field_name: str) -> int:
    value = _optional_int(payload, field_name)
    if value is None:
        raise RunnerProtocolValidationError(f"{field_name} is required.")
    return value


def _optional_bool(payload: Mapping[str, Any], field_name: str) -> bool:
    value = payload.get(field_name, False)
    if isinstance(value, bool):
        return value
    raise RunnerProtocolValidationError(f"{field_name} must be a boolean.")


def _optional_bool_or_none(payload: Mapping[str, Any], field_name: str) -> bool | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise RunnerProtocolValidationError(f"{field_name} must be a boolean when provided.")


def _require_bool(payload: Mapping[str, Any], field_name: str) -> bool:
    value = payload.get(field_name)
    if isinstance(value, bool):
        return value
    raise RunnerProtocolValidationError(f"{field_name} is required and must be a boolean.")


def _require_non_empty_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise RunnerProtocolValidationError(f"{field_name} is required and must be a non-empty string.")
    stripped = value.strip()
    if not stripped:
        raise RunnerProtocolValidationError(f"{field_name} is required and must be a non-empty string.")
    return stripped


def _require_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise RunnerProtocolValidationError(f"{field_name} is required and must be an object.")
    return value


def _require_list(payload: Mapping[str, Any], field_name: str) -> list[Any]:
    value = payload.get(field_name)
    if not isinstance(value, list):
        raise RunnerProtocolValidationError(f"{field_name} is required and must be an array.")
    return value


def _require_positive_number(payload: Mapping[str, Any], field_name: str) -> float:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerProtocolValidationError(
            f"{field_name} is required and must be a positive number."
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise RunnerProtocolValidationError(
            f"{field_name} is required and must be a positive number."
        )
    return normalized


def _validate_timestamp(timestamp: str, field_name: str) -> None:
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RunnerProtocolValidationError(f"{field_name} must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise RunnerProtocolValidationError(f"{field_name} must include timezone information.")


def _looks_like_runtime_operation_result_payload(payload: Mapping[str, Any]) -> bool:
    return "status" in payload and "result" in payload and "operation_id" in payload


def _validate_schema_version_for_message_type(
    *,
    message_type: RunnerMessageType,
    schema_version: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    if message_type is RunnerMessageType.UNSUPPORTED:
        return
    if requires_tooling_plane_schema_version(message_type) and schema_version != RUNNER_PROTOCOL_TOOLING_PLANE_VERSION:
        raise RunnerProtocolUnsupportedSchemaError(
            f"Unsupported schema version `{schema_version}` for `{message_type.value}`."
        )
    if requires_data_plane_schema_version(message_type) and schema_version != RUNNER_PROTOCOL_DATA_PLANE_VERSION:
        raise RunnerProtocolUnsupportedSchemaError(
            f"Unsupported schema version `{schema_version}` for `{message_type.value}`."
        )
    if requires_remote_runtime_schema_version(message_type, payload) and schema_version != RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION:
        raise RunnerProtocolUnsupportedSchemaError(
            f"Unsupported schema version `{schema_version}` for `{message_type.value}`."
        )


def _validate_outbound_payload_safety(
    *,
    schema_version: str,
    message_type: RunnerMessageType,
    payload: Any,
) -> None:
    if schema_version not in {
        RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        RUNNER_PROTOCOL_TOOLING_PLANE_VERSION,
    }:
        return
    _assert_json_safe(payload, path="payload")
    if isinstance(payload, Mapping):
        if message_type is RunnerMessageType.TOOL_RESULT:
            # Preserve stable user-facing result/metadata key shapes for tool.result.
            payload = {
                key: value
                for key, value in payload.items()
                if key not in {"result", "metadata"}
            }
        _assert_secret_safe_mapping(payload, field_name="payload")


def _validate_runtime_operation_text(value: str, *, field_name: str, max_length: int) -> None:
    if len(value) > max_length:
        raise RunnerProtocolValidationError(f"{field_name} length must be <= {max_length} characters.")


def _validate_max_utf8_bytes(value: str, *, field_name: str, max_bytes: int) -> None:
    encoded_size = len(value.encode("utf-8", errors="replace"))
    if encoded_size > max_bytes:
        raise RunnerProtocolValidationError(f"{field_name} must be <= {max_bytes} bytes.")


def _truncate_utf8_text_to_max_bytes(value: str, *, max_bytes: int) -> tuple[str, bool, int]:
    encoded = value.encode("utf-8", errors="replace")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return value, False, original_bytes
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True, original_bytes


def _reject_unknown_fields(
    payload: Mapping[str, Any],
    *,
    allowed_fields: frozenset[str],
    payload_type: str,
) -> None:
    unknown_fields = sorted(str(key) for key in payload.keys() if key not in allowed_fields)
    if unknown_fields:
        unknown_text = ", ".join(unknown_fields)
        raise RunnerProtocolValidationError(f"{payload_type} includes unknown fields: {unknown_text}.")


def _assert_json_safe(value: Any, *, path: str) -> None:
    if isinstance(value, _JSON_SAFE_SCALAR_TYPES):
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise RunnerProtocolValidationError(f"{path} keys must be strings.")
            _assert_json_safe(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_json_safe(nested, path=f"{path}[{index}]")
        return
    raise RunnerProtocolValidationError(f"{path} must be JSON-safe.")


def _assert_secret_safe_mapping(payload: Mapping[str, Any], *, field_name: str) -> None:
    for key, value in payload.items():
        lowered_key = str(key).strip().lower()
        if any(part in lowered_key for part in _SENSITIVE_KEY_PARTS):
            raise RunnerProtocolValidationError(f"{field_name} includes forbidden key `{key}`.")
        if isinstance(value, Mapping):
            _assert_secret_safe_mapping(value, field_name=f"{field_name}.{key}")


def _sanitize_tool_result_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    sanitized = value
    for pattern, replacement in _TOOL_RESULT_INLINE_SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _normalize_runner_metadata(payload: Mapping[str, Any]) -> tuple[tuple[str, ...], Mapping[str, str]]:
    capabilities_raw = payload.get("capabilities", ())
    if not isinstance(capabilities_raw, (list, tuple)):
        raise RunnerProtocolValidationError("capabilities must be an array.")
    if len(capabilities_raw) > RUNNER_CAPABILITIES_MAX_ITEMS:
        raise RunnerProtocolValidationError(
            f"capabilities must not exceed {RUNNER_CAPABILITIES_MAX_ITEMS} entries."
        )
    capabilities: list[str] = []
    for capability in capabilities_raw:
        if not isinstance(capability, str):
            raise RunnerProtocolValidationError("capabilities entries must be non-empty strings.")
        normalized_capability = capability.strip()
        if not normalized_capability:
            raise RunnerProtocolValidationError("capabilities entries must be non-empty strings.")
        if len(normalized_capability) > RUNNER_CAPABILITY_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"capability entries must be <= {RUNNER_CAPABILITY_MAX_LENGTH} characters."
            )
        capabilities.append(normalized_capability)

    labels_raw = payload.get("labels", {})
    if not isinstance(labels_raw, Mapping):
        raise RunnerProtocolValidationError("labels must be an object.")
    if len(labels_raw) > RUNNER_LABELS_MAX_ITEMS:
        raise RunnerProtocolValidationError(f"labels must not exceed {RUNNER_LABELS_MAX_ITEMS} entries.")
    labels: dict[str, str] = {}
    for key, value in labels_raw.items():
        if not isinstance(key, str):
            raise RunnerProtocolValidationError("labels keys must be strings.")
        normalized_key = key.strip()
        if not normalized_key:
            raise RunnerProtocolValidationError("labels keys must be non-empty strings.")
        if len(normalized_key) > RUNNER_LABEL_KEY_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"label keys must be <= {RUNNER_LABEL_KEY_MAX_LENGTH} characters."
            )
        normalized_value = str(value).strip()
        if len(normalized_value) > RUNNER_LABEL_VALUE_MAX_LENGTH:
            raise RunnerProtocolValidationError(
                f"label values must be <= {RUNNER_LABEL_VALUE_MAX_LENGTH} characters."
            )
        labels[normalized_key] = normalized_value

    return tuple(capabilities), MappingProxyType(labels)
