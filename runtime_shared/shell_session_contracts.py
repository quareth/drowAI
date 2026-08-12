"""Purpose: define backend-free shell-session request and result contracts.

This module owns the serializable DTOs shared by agent, graph, backend, and
runtime-adjacent shell-session code. It does not execute commands, manage live
PTY resources, or depend on backend-owned services.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from runtime_shared.docker_contracts import build_runtime_contract_environment
from runtime_shared.shell_timeouts import (
    SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC,
    SHELL_SESSION_DEFAULT_YIELD_TIME_MS,
    SHELL_SESSION_MAX_RUNTIME_SEC,
    SHELL_SESSION_MAX_YIELD_TIME_MS,
)

SHELL_SESSION_MAX_ENV_ENTRIES = 64
SHELL_SESSION_MAX_ENV_TOTAL_BYTES = 32 * 1024
SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS = 32_000
SHELL_SESSION_MIN_OUTPUT_CHARS = 1_024
SHELL_SESSION_MAX_OUTPUT_CHARS = 128_000
SHELL_SESSION_MAX_INPUT_CHARS = 16_384
SHELL_SESSION_MAX_PUBLIC_ID_CHARS = 128
SHELL_SESSION_PROTECTED_ENV_NAMES = frozenset(
    {
        "AGENT_MODE",
        "ENABLE_PTY_EXECUTION",
        "EXECUTION_MODE",
        "KALI_EXECUTOR_MAX_CONCURRENT_COMMANDS",
        "PYTHONPATH",
        "RUNTIME_PLACEMENT_MODE",
        "TASK_ID",
        "TENANT_ID",
        "VPN_CONFIG",
        "WORKSPACE",
        *build_runtime_contract_environment().keys(),
    }
)
SHELL_SESSION_PROTECTED_ENV_PREFIXES = (
    "__DROWAI_",
    "DROWAI_",
    "RUNNER_",
)

ShellRuntimePlacementMode = Literal["local", "runner"]
ShellSessionStatus = Literal["success", "error"]


def format_shell_execution_owner_id(
    owner_type: Literal["main", "subagent"],
    owner_id: str,
) -> str:
    """Return the canonical shell-session execution owner identifier."""
    return f"{owner_type}:{owner_id}"


class ShellProcessStatus(str, Enum):
    """Provider-neutral process state exposed in shell-session results."""

    RUNNING = "running"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class ShellSessionLifecycleStatus(str, Enum):
    """Provider-neutral lifecycle state for the live shell session container."""

    ACTIVE = "active"
    CLOSED = "closed"
    UNAVAILABLE = "unavailable"


class ShellInteractionBoundary(str, Enum):
    """Meaningful runtime boundary that resumes graph coordination."""

    OUTPUT_AVAILABLE = "output_available"
    QUIET_BOUNDARY = "quiet_boundary"
    TERMINAL = "terminal"


class ShellInteractionAction(str, Enum):
    """Semantic continuation actions available to shell-session coordination."""

    SEND_INPUT = "send_input"
    WAIT_FOR_OUTPUT = "wait_for_output"
    INTERRUPT = "interrupt"


class ShellSessionErrorCode(str, Enum):
    """Stable agent-visible shell-session error codes."""

    SHELL_RUNTIME_UNAVAILABLE = "shell_runtime_unavailable"
    SESSION_LIMIT_REACHED = "session_limit_reached"
    SESSION_UNAVAILABLE = "session_unavailable"
    SESSION_BUSY = "session_busy"
    COMMAND_START_FAILED = "command_start_failed"
    COMMAND_OUTPUT_INVALID = "command_output_invalid"
    COMMAND_TIMED_OUT = "command_timed_out"
    RUNTIME_TRANSPORT_FAILED = "runtime_transport_failed"


SHELL_SESSION_ERROR_CODES: tuple[str, ...] = tuple(
    code.value for code in ShellSessionErrorCode
)


@dataclass(frozen=True, slots=True)
class ShellSessionIdentity:
    """Serializable authority context for one shell-session operation."""

    tenant_id: int
    task_id: int
    execution_owner_id: str
    runtime_placement_mode: ShellRuntimePlacementMode
    workspace_id: str
    workspace_path: str | None
    runner_id: str | None
    execution_site_id: str | None


@dataclass(frozen=True, slots=True)
class ShellSessionOrigin:
    """Immutable UI correlation identity for one originating shell tool call."""

    tool_call_id: str | None = None
    tool_batch_id: str | None = None
    tool_name: str | None = None


def _validate_env_limits(env: dict[str, str]) -> dict[str, str]:
    if len(env) > SHELL_SESSION_MAX_ENV_ENTRIES:
        raise ValueError(
            f"env cannot contain more than {SHELL_SESSION_MAX_ENV_ENTRIES} entries"
        )

    total_bytes = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8"))
        for key, value in env.items()
    )
    if total_bytes > SHELL_SESSION_MAX_ENV_TOTAL_BYTES:
        raise ValueError(
            f"env cannot exceed {SHELL_SESSION_MAX_ENV_TOTAL_BYTES} UTF-8 bytes"
        )
    return env


def _validate_protected_env(env: dict[str, str]) -> dict[str, str]:
    for key in env:
        normalized_key = str(key).strip().upper()
        if normalized_key in SHELL_SESSION_PROTECTED_ENV_NAMES or normalized_key.startswith(
            SHELL_SESSION_PROTECTED_ENV_PREFIXES
        ):
            raise ValueError(
                f"env cannot replace protected runtime or transport variable `{key}`"
            )
    return env


class ShellExecRequest(BaseModel):
    """Validated service-bound request for starting a shell command session."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(
        ...,
        min_length=1,
        description="Command line interpreted by the task runtime shell.",
    )
    cwd: str | None = Field(
        default=None,
        description="Optional runtime working directory; host paths are not resolved here.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional bounded environment variables for the runtime command.",
    )
    yield_time_ms: int | None = Field(
        default=None,
        ge=0,
        le=SHELL_SESSION_MAX_YIELD_TIME_MS,
        description=(
            "Explicit interactive yield window. Omit to keep the invocation "
            "attached until the process reaches a terminal state."
        ),
    )
    max_output_chars: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
        ge=SHELL_SESSION_MIN_OUTPUT_CHARS,
        le=SHELL_SESSION_MAX_OUTPUT_CHARS,
        description="Maximum stdout delta characters returned in this update.",
    )
    max_runtime_sec: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC,
        ge=1,
        le=SHELL_SESSION_MAX_RUNTIME_SEC,
        description="Hard process lifetime measured from session creation.",
    )

    @field_validator("env", mode="before")
    @classmethod
    def _coerce_optional_env(cls, value: object) -> object:
        if value is None:
            return {}
        return value

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        _validate_protected_env(value)
        return _validate_env_limits(value)


class ShellWaitRequest(BaseModel):
    """Validated internal request for waiting on an existing shell session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=SHELL_SESSION_MAX_PUBLIC_ID_CHARS,
    )
    max_output_chars: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
        ge=SHELL_SESSION_MIN_OUTPUT_CHARS,
        le=SHELL_SESSION_MAX_OUTPUT_CHARS,
    )


class ShellWriteRequest(BaseModel):
    """Validated service-bound request for writing non-empty input to a session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=SHELL_SESSION_MAX_PUBLIC_ID_CHARS,
    )
    chars: str = Field(
        ...,
        min_length=1,
        max_length=SHELL_SESSION_MAX_INPUT_CHARS,
    )
    yield_time_ms: int = Field(
        default=SHELL_SESSION_DEFAULT_YIELD_TIME_MS,
        ge=0,
        le=SHELL_SESSION_MAX_YIELD_TIME_MS,
    )
    max_output_chars: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
        ge=SHELL_SESSION_MIN_OUTPUT_CHARS,
        le=SHELL_SESSION_MAX_OUTPUT_CHARS,
    )


class ShellSessionUpdate(BaseModel):
    """Bounded serializable output delta for shell-session tool results."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    status: ShellSessionStatus
    process_status: ShellProcessStatus | None = None
    session_status: ShellSessionLifecycleStatus | None = None
    interaction_boundary: ShellInteractionBoundary | None = None
    session_id: str | None = Field(
        default=None,
        max_length=SHELL_SESSION_MAX_PUBLIC_ID_CHARS,
    )
    stdout: str = Field(default="", max_length=SHELL_SESSION_MAX_OUTPUT_CHARS)
    stdout_ends_with_newline: bool = False
    stderr: str = Field(default="", max_length=SHELL_SESSION_MAX_OUTPUT_CHARS)
    exit_code: int | None = None
    stdin_available: bool = False
    truncated: bool = False
    duration_ms: int = Field(ge=0)
    summary: str = Field(default="", max_length=512)
    error_code: ShellSessionErrorCode | None = None

    @model_validator(mode="after")
    def _fill_summary(self) -> "ShellSessionUpdate":
        if self.summary.strip():
            return self

        if self.process_status is ShellProcessStatus.RUNNING:
            if self.stdout or self.stderr:
                self.summary = "Command is still running; new output was received."
            elif self.stdin_available:
                self.summary = (
                    "Command is still running and accepts additional input; "
                    "no new output was produced."
                )
            else:
                self.summary = (
                    "Command is still running; no new output was produced."
                )
        elif self.process_status is ShellProcessStatus.COMPLETED:
            if self.exit_code == 0:
                self.summary = "Command completed successfully."
            else:
                self.summary = f"Command completed with exit code {self.exit_code}."
        elif self.process_status is ShellProcessStatus.TERMINATED:
            self.summary = "Command was terminated."
        elif self.process_status is ShellProcessStatus.TIMED_OUT:
            self.summary = "Command timed out."
        elif self.process_status is ShellProcessStatus.FAILED:
            if self.exit_code is not None:
                self.summary = f"Command failed with exit code {self.exit_code}."
            else:
                self.summary = "Shell session process failed."
        elif self.error_code is not None:
            self.summary = f"Shell session failed: {self.error_code.value}."
        else:
            self.summary = (
                "Shell session update completed."
                if self.success
                else "Shell session failed."
            )
        return self
