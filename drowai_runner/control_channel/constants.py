"""Static control-channel constants and protocol vocabulary.

Values only; no logic, no I/O, and no imports from sibling control_channel
modules. The only external dependency is ``runtime_shared.runner_protocol`` for
the message-type enum and terminal frame size limit referenced below.
"""

from __future__ import annotations

from runtime_shared.runner_protocol import (
    RUNNER_TERMINAL_FRAME_MAX_BYTES,
    RunnerMessageType,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_OPEN_TIMEOUT_SECONDS = 10.0
DEFAULT_RECV_TIMEOUT_SECONDS = 1.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
BACKOFF_JITTER_RATIO = 0.20
TOOL_RESULT_POLL_INTERVAL_SECONDS = 0.10
TOOL_RESULT_GRACE_SECONDS = 5.0
TENANT_ID_ENV = "DROWAI_RUNNER_TENANT_ID"
RUNNER_VERSION_ENV = "DROWAI_RUNNER_VERSION"
_SEMANTIC_TOOL_METADATA_KEYS = frozenset(
    {
        "semantic_observations",
        "semantic_evidence",
        "semantic_schema_version",
        "capability_family",
    }
)
_REMOTE_RUNTIME_REQUEST_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_STOP,
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
_REMOTE_RUNTIME_LIFECYCLE_REQUEST_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START,
        RunnerMessageType.TASK_PAUSE,
        RunnerMessageType.TASK_RESUME,
        RunnerMessageType.TASK_STOP,
        RunnerMessageType.TASK_RETIRE,
    }
)
_REMOTE_RUNTIME_WORKSPACE_SCOPED_REQUEST_TYPES = frozenset(
    {
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        RunnerMessageType.RUNTIME_WORKSPACE_READ,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        RunnerMessageType.RUNTIME_VPN_CONFIG,
    }
)
_REMOTE_RUNTIME_RUNNER_IDENTITY_ERROR = "RUNNER_ASSIGNMENT_NOT_FOUND"
_REMOTE_RUNTIME_CONTEXT_MISSING = "RUNTIME_JOB_NOT_ASSIGNED"
_REMOTE_RUNTIME_CONTEXT_MISMATCH = "RUNTIME_JOB_ASSIGNMENT_MISMATCH"
_REMOTE_RUNTIME_WORKSPACE_MISMATCH = "RUNTIME_WORKSPACE_MISMATCH"
_REMOTE_RUNTIME_START_CONFLICT = "RUNTIME_JOB_START_CONFLICT"
_TOOLING_PLANE_TOOL_COMMAND_BINDING_CONFLICT = "TOOL_COMMAND_RUNTIME_JOB_BINDING_CONFLICT"
_TOOLING_PLANE_TOOL_RESULT_STATUS_COMPLETED = "completed"
_DATA_PLANE_ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT = "ARTIFACT_UPLOAD_REQUEST_BINDING_CONFLICT"
_DATA_PLANE_ARTIFACT_UPLOAD_MAX_RETRY_ATTEMPTS = 3
_DATA_PLANE_ARTIFACT_WARNING_LIMIT = 16
_TERMINAL_FRAME_MAX_BYTES = RUNNER_TERMINAL_FRAME_MAX_BYTES
_TERMINAL_FRAME_MAX_FRAMES_PER_OPERATION = 32
_TERMINAL_FRAME_MAX_TOTAL_BYTES_PER_OPERATION = _TERMINAL_FRAME_MAX_BYTES * 8
_TERMINAL_FRAME_POLL_INTERVAL_SECONDS = 0.05
_TERMINAL_STREAM_CAPABILITY = "terminal_stream_v1"
