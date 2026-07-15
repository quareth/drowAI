from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..filesystem._helpers import (
    build_tool_result,
    resolve_workspace_path_safe,
    workspace_root,
)
from ..schemas import ToolResult
from .contracts import ShellCommandResult, ShellExecArgs, ShellScriptArgs
from .policy import CommandPolicy

# Import centralized output processing utilities
from agent.utils.output_processing import (
    smart_truncate,
    strip_noise,
    extract_error_lines,
    DEFAULT_TOTAL_LIMIT,
)

# Stdout limits for summaries (use centralized default)
STDOUT_SUMMARY_LIMIT = DEFAULT_TOTAL_LIMIT
STDERR_SUMMARY_LIMIT = 1000


def _strip_kali_welcome(output: str) -> str:
    """Remove the Kali welcome message to save context space.
    
    Delegates to centralized strip_noise() for consistent noise removal.
    """
    return strip_noise(output)


def _bash_like_command(command: str) -> Tuple[str, ...]:
    if os.name == "nt":
        return ("powershell", "-Command", command)
    shell = "bash" if shutil.which("bash") else "sh"
    return (shell, "-lc", command)


def _build_shell_command(args: ShellExecArgs) -> Tuple[str, ...]:
    """Construct a shell command tuple respecting env/cwd for PTY/direct modes."""
    env_prefix = ""
    if args.env:
        # Inline env exports so PTY transports inherit variables
        if os.name == "nt":
            assignments = [f"$Env:{key}={shlex.quote(str(value))};" for key, value in args.env.items() if value is not None]
            env_prefix = " ".join(assignments)
        else:
            assignments = [f"{key}={shlex.quote(str(value))}" for key, value in args.env.items() if value is not None]
            if assignments:
                env_prefix = " ".join(assignments) + " "

    cd_prefix = ""
    if args.cwd:
        target_cwd = _resolve_cwd(args.cwd)
        cd_prefix = f"cd {shlex.quote(str(target_cwd))} && "

    command_body = f"{cd_prefix}{env_prefix}{args.command}".strip()

    if os.name == "nt":
        return ("powershell", "-Command", command_body)

    shell = "bash" if shutil.which("bash") else "sh"
    return (shell, "-c", command_body)


def render_shell_script_body(args: ShellScriptArgs) -> str:
    """Return the script body after applying shell strict-mode defaults."""

    body = args.script
    if args.interpreter in {"bash", "sh"} and args.strict_mode:
        strict_prefix = "set -euo pipefail\n"
        if not body.lstrip().startswith("set -e"):
            body = strict_prefix + body
    return body


def build_shell_script_relative_path(
    args: ShellScriptArgs,
    *,
    script_id: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> str:
    """Return a workspace-relative script path for shell.script execution."""

    extension = {
        "bash": ".sh",
        "sh": ".sh",
        "python3": ".py",
        "powershell": ".ps1",
    }.get(args.interpreter, ".sh")
    script_id = script_id or uuid.uuid4().hex[:12]
    timestamp = timestamp or int(time.time())
    return f"scripts/script_{script_id}_{timestamp}{extension}"


def _build_script_command(
    args: ShellScriptArgs,
    *,
    write_script: bool = True,
    relative_path: Optional[str] = None,
) -> Tuple[Tuple[str, ...], Path]:
    """Build the interpreter command tuple, optionally writing the script file."""
    root = workspace_root()
    relative_path = relative_path or build_shell_script_relative_path(args)
    script_path = (root / relative_path).resolve()
    execution_path = str(script_path) if write_script else f"/workspace/{relative_path}"
    if write_script:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(render_shell_script_body(args), encoding="utf-8")

    interpreter_cmd = {
        "bash": ("bash", execution_path),
        "sh": ("sh", execution_path),
        "python3": ("python3", execution_path),
        "powershell": ("pwsh", "-File", execution_path) if shutil.which("pwsh") else ("powershell", "-File", execution_path),
    }.get(args.interpreter, ("bash", execution_path))

    return interpreter_cmd, script_path


def _prepare_env(extra: Optional[Dict[str, str]]) -> Dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def _resolve_cwd(cwd: Optional[str]) -> Path:
    root = workspace_root()
    if cwd is None:
        return root
    return resolve_workspace_path_safe(cwd, workspace=root)


def _run_subprocess(cmd: Tuple[str, ...], cwd: Path, env: Dict[str, str], timeout: int) -> Tuple[int, str, str, float]:
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.time() - start
        return proc.returncode, proc.stdout, proc.stderr, duration
    except subprocess.TimeoutExpired as exc:
        duration = time.time() - start
        return -9, exc.stdout or "", exc.stderr or "Command timed out", duration


def execute_noninteractive(args: ShellExecArgs) -> Tuple[ShellCommandResult, ToolResult]:
    """Execute shell command directly in agent process (direct transport)."""
    overall_start = time.time()
    
    # Policy validation
    policy = CommandPolicy()
    policy_result = policy.validate(args.command)
    
    if not policy_result.allowed:
        result = ShellCommandResult(
            status="error",
            exit_code=-1,
            stdout="",
            stderr=f"Policy violation: {policy_result.reason}",
            duration_ms=0,
            transport="direct",
            truncated=False,
        )
        tool_result = build_tool_result(
            success=False,
            start=overall_start,
            stdout="",
            stderr=result.stderr,
            metadata={
                "shell_exec": result.model_dump(),
                "policy_violation": {
                    "matched_pattern": policy_result.matched_pattern,
                    "severity": policy_result.severity,
                },
            },
            exit_code=-1,
        )
        return result, tool_result
    
    cwd = _resolve_cwd(args.cwd)
    env = _prepare_env(args.env)
    cmd = _build_shell_command(args)
    exit_code, stdout, stderr, duration = _run_subprocess(cmd, cwd, env, args.timeout_sec)

    status = "success" if exit_code == 0 else "error"
    result = ShellCommandResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int(duration * 1000),
        transport="direct",
        truncated=False,
    )

    metadata = {"shell_exec": result.model_dump()}
    
    # Clean and smart-truncate stdout for summary
    # Uses head+tail truncation to preserve both context and results
    clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
    stdout_summary = smart_truncate(clean_stdout, total_limit=STDOUT_SUMMARY_LIMIT) if clean_stdout else ""
    stderr_summary = smart_truncate(stderr, total_limit=STDERR_SUMMARY_LIMIT) if stderr else ""
    
    # Extract error lines if output was truncated (surfaces buried errors)
    if clean_stdout and len(clean_stdout) > STDOUT_SUMMARY_LIMIT:
        error_lines = extract_error_lines(clean_stdout, max_matches=3)
        if error_lines:
            stdout_summary += f"\n\n=== Extracted Errors ===\n{error_lines}"

    tool_result = build_tool_result(
        success=exit_code == 0,
        start=overall_start,
        stdout=stdout_summary,
        stderr=stderr_summary,
        metadata=metadata,
        exit_code=exit_code,
    )
    return result, tool_result


def execute_script(args: ShellScriptArgs) -> Tuple[ShellCommandResult, ToolResult]:
    """Execute multi-line script directly in agent process (direct transport)."""
    overall_start = time.time()
    
    # Policy validation on script content (check for obvious violations)
    policy = CommandPolicy()
    # For scripts, check each line for denylist violations
    for line in args.script.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        policy_result = policy.validate(line)
        if not policy_result.allowed and policy_result.severity == "error":
            result = ShellCommandResult(
                status="error",
                exit_code=-1,
                stdout="",
                stderr=f"Policy violation in script: {policy_result.reason} (line: {line[:50]})",
                duration_ms=0,
                transport="direct",
                truncated=False,
            )
            tool_result = build_tool_result(
                success=False,
                start=overall_start,
                stdout="",
                stderr=result.stderr,
                metadata={
                    "shell_script": result.model_dump(),
                    "policy_violation": {
                        "matched_pattern": policy_result.matched_pattern,
                        "severity": policy_result.severity,
                        "line": line[:100],
                    },
                },
                exit_code=-1,
            )
            return result, tool_result
    
    cwd = _resolve_cwd(args.cwd)
    env = _prepare_env(args.env)

    interpreter_cmd, script_path = _build_script_command(args)

    exit_code, stdout, stderr, duration = _run_subprocess(interpreter_cmd, cwd, env, args.timeout_sec)
    status = "success" if exit_code == 0 else "error"

    result = ShellCommandResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int(duration * 1000),
        transport="direct",
        truncated=False,
    )

    metadata = {
        "shell_script": result.model_dump() | {"script_path": str(script_path.relative_to(workspace_root()))},
    }

    # Clean and smart-truncate stdout for summary
    clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
    stdout_summary = smart_truncate(clean_stdout, total_limit=STDOUT_SUMMARY_LIMIT) if clean_stdout else ""
    stderr_summary = smart_truncate(stderr, total_limit=STDERR_SUMMARY_LIMIT) if stderr else ""
    
    # Extract error lines if output was truncated
    if clean_stdout and len(clean_stdout) > STDOUT_SUMMARY_LIMIT:
        error_lines = extract_error_lines(clean_stdout, max_matches=3)
        if error_lines:
            stdout_summary += f"\n\n=== Extracted Errors ===\n{error_lines}"

    tool_result = build_tool_result(
        success=exit_code == 0,
        start=overall_start,
        stdout=stdout_summary,
        stderr=stderr_summary,
        metadata=metadata,
        exit_code=exit_code,
    )

    return result, tool_result


def _pty_not_available() -> Tuple[ShellCommandResult, ToolResult]:
    message = "PTY transport is not wired to the executor yet."
    result = ShellCommandResult(
        status="error",
        exit_code=-1,
        stdout="",
        stderr=message,
        duration_ms=0,
        transport="pty",
        truncated=False,
    )
    tool_result = build_tool_result(
        success=False,
        start=time.time(),
        stdout="",
        stderr=message,
        metadata={"shell_pty": result.model_dump()},
        exit_code=-1,
    )
    return result, tool_result
