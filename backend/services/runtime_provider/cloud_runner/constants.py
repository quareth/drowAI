"""Protocol constants for cloud runner provider collaborators."""

from runtime_shared.runtime_image_contract import default_runtime_image_for_machine
from runtime_shared.runner_protocol import RunnerMessageType

_POD_ID = "cloud-runner-provider"
_DEFAULT_RUNTIME_IMAGE = default_runtime_image_for_machine()

_TOOL_COMMAND_JOB_TYPE = RunnerMessageType.TOOL_COMMAND.value
_TASK_START_JOB_TYPE = RunnerMessageType.TASK_START.value
_RUNNER_REQUIRED_TOOL_CAPABILITY = "tool_command.v1"
_RUNNER_REQUIRED_CHANNEL_CAPABILITY = "tooling_plane.commands.v1"
_FORBIDDEN_TOOL_COMMAND_PARAM_IDENTITY_FIELDS = frozenset(
    {
        "runtime_job_id",
        "runner_runtime_job_id",
        "task_runtime_job_id",
        "tool_command_runtime_job_id",
    }
)

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
    "vpn_config",
    "config_data",
)
_ACTIVE_START_RUNTIME_JOB_STATUSES = frozenset(
    {
        "queued",
        "assigned",
        "dispatching",
        "dispatched",
        "acknowledged",
        "accepted",
        "running",
        "succeeded",
    }
)
_TERMINAL_PENDING_RUNTIME_JOB_STATUSES = frozenset(
    {
        "queued",
        "assigned",
        "dispatching",
        "dispatched",
        "acknowledged",
        "accepted",
        "running",
    }
)
_RESULT_PENDING_RUNTIME_JOB_STATUSES = frozenset(
    {
        "queued",
        "assigned",
        "dispatching",
        "dispatched",
        "acknowledged",
        "accepted",
        "running",
    }
)
_TOOL_COMMAND_ACK_PENDING_RUNTIME_JOB_STATUSES = frozenset(
    {
        "queued",
        "assigned",
        "dispatching",
        "dispatched",
    }
)
