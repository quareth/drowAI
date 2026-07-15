"""Pydantic contracts for shell execution tools.

This module owns shell tool argument/result schemas only. It does not execute
commands, apply shell policy, manage PTY sessions, or parse runtime output.
"""

from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field

from agent.tools.schemas import CONTAINER_TRANSPORT_DESCRIPTION, ContainerTransport

ShellTransport = Literal["direct", "file-comm", "pty"]
"""
Execution transport values that may appear in legacy shell result metadata:
- "direct": Direct compatibility execution in local tests or backend-only paths
- "file-comm": Execute via file-based queue in Kali container (production, container-based)
- "pty": Execute in persistent PTY session (visible to users, best for troubleshooting)
"""


class ShellExecArgs(BaseModel):
    """
    Execute a single shell command inside the task container.
    
    Args:
        command: Shell command to execute
        cwd: Optional working directory relative to workspace root
        env: Additional environment variables
        timeout_sec: Command timeout in seconds (default: 120)
        transport: Execution method (optional):
            - "file-comm": Execute via Kali container queue
            - "pty": Execute in persistent PTY session (visible to users, troubleshooting)
            - None: Executor auto-selects based on availability
        idempotent: Whether rerunning has the same effect
        redact_output: Apply secret redaction heuristics
    
    Examples:
        # Auto-select transport (recommended)
        {"command": "whoami"}
        
        # Force PTY for visibility
        {"command": "nmap -p 80 10.0.0.1", "transport": "pty"}
    """

    command: str = Field(..., description="Command line to execute (interpreted by /bin/sh -lc).")
    cwd: Optional[str] = Field(
        None,
        description="Optional working directory relative to the workspace root.",
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        description="Additional environment variables to merge into the execution environment.",
    )
    timeout_sec: int = Field(
        120,
        ge=1,
        le=900,
        description="Maximum time in seconds to allow the command to run before termination.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )
    idempotent: bool = Field(
        True,
        description="Indicates whether rerunning the command is expected to have the same effect (used for policy decisions).",
    )
    redact_output: bool = Field(
        True,
        description="When true, the tool should apply secret redaction heuristics before returning output.",
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
