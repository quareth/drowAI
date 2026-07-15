from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import ShellExecArgs
from .policy import CommandPolicy
from ._helpers import (
    STDOUT_SUMMARY_LIMIT,
    STDERR_SUMMARY_LIMIT,
    _build_shell_command,
    _prepare_env,
    _resolve_cwd,
    _run_subprocess,
    _strip_kali_welcome,
    extract_error_lines,
    smart_truncate,
    build_tool_result,
)


class ShellExecTool(BaseTool):
    """
    Execute a single shell command.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute in persistent PTY session (visible to users)
    - None: Executor auto-selects based on availability
    
    PTY support:
        - build_command() produces a PTY-friendly shell invocation
        - Executor routes to PTY automatically when ENABLE_PTY_EXECUTION=true
        - transport parameter is optional; executor chooses the best path
    
    PTY execution improves visibility and debugging by streaming output
    live in the user-facing terminal while preserving consistent parsing
    and artifact creation across transports.
    """

    args_model = ShellExecArgs

    def build_command(self, args: ShellExecArgs) -> List[str]:
        """Build a shell command list suitable for container execution."""
        return list(_build_shell_command(args))

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: ShellExecArgs) -> Dict[str, Any]:
        """Parse execution output into structured metadata."""
        clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
        metadata: Dict[str, Any] = {
            "command": args.command,
            "exit_code": exit_code,
            "success": exit_code == 0,
            "output_length": len(clean_stdout),
            "has_errors": bool(stderr),
            "transport": args.transport or "direct",
        }

        if stderr:
            metadata["error_lines"] = extract_error_lines(stderr, max_matches=5)

        return {"shell_exec": metadata}

    def create_artifacts(
        self,
        stdout: str,
        args: ShellExecArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist large outputs and stderr to artifacts/ for downstream review."""
        created: List[str] = []
        ts = timestamp or int(time.time())

        try:
            os.makedirs("artifacts", exist_ok=True)

            if stdout and len(stdout) > 10 * 1024:
                path = os.path.join("artifacts", f"shell_exec_{ts}.txt")
                with open(path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(stdout)
                created.append(path)

            if stderr:
                err_path = os.path.join("artifacts", f"shell_exec_{ts}_errors.txt")
                with open(err_path, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(stderr)
                created.append(err_path)
        except Exception:
            # Artifact creation is optional; never block tool execution
            pass

        return created

    def run(self, args: ShellExecArgs) -> ToolResult:
        # Policy validation remains the first gate
        overall_start = time.time()
        policy = CommandPolicy()
        policy_result = policy.validate(args.command)

        if not policy_result.allowed:
            metadata = {
                "shell_exec": {
                    "command": args.command,
                    "exit_code": -1,
                    "success": False,
                    "output_length": 0,
                    "has_errors": True,
                    "transport": args.transport or "direct",
                },
                "policy_violation": {
                    "matched_pattern": policy_result.matched_pattern,
                    "severity": policy_result.severity,
                },
            }
            tool_result = build_tool_result(
                success=False,
                start=overall_start,
                stdout="",
                stderr=f"Policy violation: {policy_result.reason}",
                metadata=metadata,
                exit_code=-1,
            )
            return tool_result

        # Build and execute the command
        cwd = _resolve_cwd(args.cwd)
        env = _prepare_env(args.env)
        command = self.build_command(args)
        exit_code, stdout, stderr, _duration = _run_subprocess(tuple(command), cwd, env, args.timeout_sec)

        clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
        stdout_summary = smart_truncate(clean_stdout, total_limit=STDOUT_SUMMARY_LIMIT) if clean_stdout else ""
        stderr_summary = smart_truncate(stderr, total_limit=STDERR_SUMMARY_LIMIT) if stderr else ""

        metadata = self.parse_output(stdout, stderr, exit_code, args)
        artifacts = self.create_artifacts(stdout=stdout, args=args, stderr=stderr)

        tool_result = build_tool_result(
            success=exit_code == 0,
            start=overall_start,
            stdout=stdout_summary,
            stderr=stderr_summary,
            metadata=metadata,
            exit_code=exit_code,
        )
        try:
            tool_result.artifacts = artifacts
        except Exception:
            pass
        return tool_result
