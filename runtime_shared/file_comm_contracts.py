"""Runtime-safe file-comm and workspace layout contracts.

This module defines the canonical JSONL filenames, lock filenames, standard
runtime workspace subdirectories, and shared command/result schema models used
by both agent-side and executor-side file communication paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_WORKSPACE_PATH = "/workspace"
COMMANDS_FILE_NAME = "commands.jsonl"
RESULTS_FILE_NAME = "results.jsonl"
CANCELLATIONS_FILE_NAME = "cancellations.jsonl"
AGENT_STATE_FILE_NAME = "agent_state.json"
LOCKS_DIRECTORY_NAME = "locks"
COMMANDS_LOCK_FILE_NAME = "commands.lock"
RESULTS_LOCK_FILE_NAME = "results.lock"
CANCELLATIONS_LOCK_FILE_NAME = "cancellations.lock"
DEFAULT_FILE_COMM_TIMEOUT_SECONDS = 600.0
TOOL_TIMEOUT_FAILURE_CATEGORY = "tool_timeout"
TOOL_TIMEOUT_EXIT_CODE = -2

STANDARD_RUNTIME_SUBDIRECTORIES: tuple[str, ...] = (
    "artifacts",
    "logs",
    "results",
    LOCKS_DIRECTORY_NAME,
)
STANDARD_RUNTIME_FILES: tuple[str, ...] = (
    COMMANDS_FILE_NAME,
    RESULTS_FILE_NAME,
    CANCELLATIONS_FILE_NAME,
    AGENT_STATE_FILE_NAME,
)
STANDARD_LOCK_FILES: tuple[str, ...] = (
    COMMANDS_LOCK_FILE_NAME,
    RESULTS_LOCK_FILE_NAME,
    CANCELLATIONS_LOCK_FILE_NAME,
)


@dataclass(frozen=True, slots=True)
class FileCommWorkspacePaths:
    """Resolved file-comm file paths for a workspace root."""

    workspace: Path
    commands_file: Path
    results_file: Path
    cancellations_file: Path
    commands_lock: Path
    results_lock: Path
    cancellations_lock: Path

    @classmethod
    def from_workspace(cls, workspace_path: str | Path = DEFAULT_WORKSPACE_PATH) -> "FileCommWorkspacePaths":
        base = Path(workspace_path)
        locks_dir = base / LOCKS_DIRECTORY_NAME
        return cls(
            workspace=base,
            commands_file=base / COMMANDS_FILE_NAME,
            results_file=base / RESULTS_FILE_NAME,
            cancellations_file=base / CANCELLATIONS_FILE_NAME,
            commands_lock=locks_dir / COMMANDS_LOCK_FILE_NAME,
            results_lock=locks_dir / RESULTS_LOCK_FILE_NAME,
            cancellations_lock=locks_dir / CANCELLATIONS_LOCK_FILE_NAME,
        )


class CommandMessage(BaseModel):
    """Schema for agent-to-executor command payloads."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique command identifier")
    timestamp: str = Field(..., description="ISO timestamp")
    command: str = Field(..., description="Prepared shell command to execute")
    cwd: str | None = Field(
        default=None,
        description="Workspace-relative or /workspace-scoped working directory.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional non-secret environment values for the command.",
    )
    timeout: float = Field(
        DEFAULT_FILE_COMM_TIMEOUT_SECONDS,
        description="Execution timeout in seconds",
    )
    timeout_policy: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-authored timeout policy metadata for executor timeout reporting.",
    )


class ResultMessage(BaseModel):
    """Schema for executor-to-agent result payloads."""

    id: str
    timestamp: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = Field(default_factory=list)
    execution_time: float
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata for tool results",
    )


class CancellationMessage(BaseModel):
    """Schema for backend-to-executor command cancellation requests."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique cancellation request identifier")
    timestamp: str = Field(..., description="ISO timestamp")
    command_id: str = Field(..., description="Command identifier to cancel")
    reason: str = Field("user_stop", description="Cancellation reason")
    source: str = Field("chat_stop", description="Cancellation source")
