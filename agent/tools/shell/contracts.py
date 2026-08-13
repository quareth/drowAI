"""Pydantic contracts for shell execution tools.

This module owns shell tool argument/result schemas only. It does not execute
commands, apply shell policy, manage PTY sessions, or parse runtime output.
"""

from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.tools.schemas import CONTAINER_TRANSPORT_DESCRIPTION, ContainerTransport
from runtime_shared.shell_session_contracts import (
    SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
    SHELL_SESSION_MAX_INPUT_CHARS,
    SHELL_SESSION_MAX_OUTPUT_CHARS,
    SHELL_SESSION_MAX_PUBLIC_ID_CHARS,
    SHELL_SESSION_MIN_OUTPUT_CHARS,
    ShellExecRequest,
)
from runtime_shared.shell_timeouts import (
    SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC,
    SHELL_SESSION_DEFAULT_YIELD_TIME_MS,
    SHELL_SESSION_MAX_RUNTIME_SEC,
    SHELL_SESSION_MAX_YIELD_TIME_MS,
)

ShellTransport = Literal["direct", "file-comm", "pty"]
"""
Execution transport values that may appear in legacy shell result metadata:
- "direct": Direct compatibility execution in local tests or backend-only paths
- "file-comm": Execute via file-based queue in Kali container (production, container-based)
- "pty": Execute in persistent PTY session (visible to users, best for troubleshooting)
"""


class ShellExecArgs(BaseModel):
    """
    Start one provider-backed shell command session inside the task runtime.
    
    Args:
        command: Shell command interpreted by the runtime shell.
        cwd: Optional runtime working directory. Relative paths resolve from /workspace.
        env: Additional bounded environment variables for the runtime command.
        yield_time_ms: Maximum silent wait before returning a live session.
        max_output_chars: Maximum output delta characters returned in this response.
        max_runtime_sec: Hard process lifetime measured from session creation.
    
    Examples:
        {"command": "whoami"}
        {"command": "nmap -p 80 10.0.0.1", "yield_time_ms": 1000}
    """

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., description="Command line to execute (interpreted by /bin/sh -lc).")
    cwd: Optional[str] = Field(
        None,
        description="Optional runtime working directory. Relative paths resolve from /workspace.",
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional bounded environment variables for the runtime command.",
    )
    yield_time_ms: int = Field(
        default=SHELL_SESSION_DEFAULT_YIELD_TIME_MS,
        ge=0,
        le=SHELL_SESSION_MAX_YIELD_TIME_MS,
        description=(
            "Maximum silent wait before returning a live session. Output and "
            "process completion return earlier."
        ),
    )
    max_output_chars: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
        ge=SHELL_SESSION_MIN_OUTPUT_CHARS,
        le=SHELL_SESSION_MAX_OUTPUT_CHARS,
        description="Maximum output delta characters returned in this response.",
    )
    max_runtime_sec: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_RUNTIME_SEC,
        ge=1,
        le=SHELL_SESSION_MAX_RUNTIME_SEC,
        description="Hard process lifetime measured from session creation.",
    )

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if value is None:
            return None
        return ShellExecRequest(command=":", env=value).env

    @field_validator("yield_time_ms", mode="before")
    @classmethod
    def _default_null_yield_time(cls, value: object) -> object:
        return SHELL_SESSION_DEFAULT_YIELD_TIME_MS if value is None else value


class ShellWriteStdinArgs(BaseModel):
    """Write non-empty input to an existing provider-backed shell session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=SHELL_SESSION_MAX_PUBLIC_ID_CHARS,
        description="Opaque public shell-session handle returned by a shell start alias.",
    )
    chars: str = Field(
        ...,
        min_length=1,
        max_length=SHELL_SESSION_MAX_INPUT_CHARS,
        description="Non-empty characters to send to stdin exactly as provided.",
    )
    yield_time_ms: int = Field(
        default=SHELL_SESSION_DEFAULT_YIELD_TIME_MS,
        ge=0,
        le=SHELL_SESSION_MAX_YIELD_TIME_MS,
        description=(
            "Fallback wait when the shell produces neither output nor process completion."
        ),
    )
    max_output_chars: int = Field(
        default=SHELL_SESSION_DEFAULT_MAX_OUTPUT_CHARS,
        ge=SHELL_SESSION_MIN_OUTPUT_CHARS,
        le=SHELL_SESSION_MAX_OUTPUT_CHARS,
        description="Maximum output delta characters returned in this response.",
    )


class ShellScriptArgs(BaseModel):
    """
    Execute a multi-line script within the task container.
    
    Args:
        script: Complete script body to execute
        interpreter: Interpreter to invoke (bash, sh, python3, powershell)
        cwd: Optional working directory relative to workspace root
        env: Additional environment variables
        timeout_sec: Script timeout in seconds (default: 300)
        transport: Execution method (optional):
            - "file-comm": Execute via Kali container queue
            - "pty": Execute in PTY session (script wrapped in bash -c, visible to users)
            - None: Executor auto-selects based on availability
        strict_mode: Enable strict/errexit-like behavior when supported
    
    PTY Transport Notes:
        - PTY transport wraps the script in `bash -c` for execution
        - Script output is visible in the agent terminal (user-visible)
        - Best for troubleshooting and debugging script execution
    
    Examples:
        # Auto-select transport (recommended)
        {"script": "#!/bin/bash\\necho 'test'"}
        
        # Force PTY for visibility
        {"script": "nmap -p 80 10.0.0.1", "transport": "pty"}
    """

    script: str = Field(
        ...,
        description="Complete script body to execute. Implementations should persist the script before execution for auditing.",
    )
    interpreter: Literal["bash", "sh", "python3", "powershell"] = Field(
        "bash",
        description="Interpreter to invoke for the script. Non-shell interpreters must remain feature-flagged.",
    )
    cwd: Optional[str] = Field(
        None,
        description="Optional working directory relative to the workspace root.",
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional environment variables to merge into the execution environment.",
    )
    timeout_sec: int = Field(
        300,
        ge=1,
        le=1_800,
        description="Maximum time in seconds to allow the script to run before termination.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )
    strict_mode: bool = Field(
        True,
        description="When supported, request interpreters to enable strict/errexit-like behaviour.",
    )


class ShellCommandResult(BaseModel):
    """Standard result payload for shell_exec and shell_script tools."""

    status: Literal["success", "error", "timeout"] = Field(
        ..., description="High-level outcome of the command execution."
    )
    exit_code: int = Field(
        ..., description="Process exit code returned by the shell or interpreter."
    )
    stdout: str = Field(
        ..., description="Standard output captured from the command."
    )
    stderr: str = Field(
        ..., description="Standard error captured from the command."
    )
    duration_ms: int = Field(
        ..., ge=0, description="Execution duration in milliseconds."
    )
    transport: ShellTransport = Field(
        ..., description="Transport used for the execution (direct, file-comm, or pty)."
    )
    truncated: bool = Field(
        False,
        description="True when stdout/stderr were truncated to satisfy policy limits.",
    )
