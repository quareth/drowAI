"""Model-facing shell.exec adapter for runtime-session dispatched execution.

The adapter validates command policy for direct invocations but never executes
commands itself. Real shell execution is owned by the graph runtime-session
dispatch path and provider-backed PTY service.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import ShellExecArgs
from .policy import CommandPolicy, validate_shell_exec_command
from ._helpers import (
    _build_shell_command,
    _strip_kali_welcome,
    extract_error_lines,
    build_tool_result,
)


class ShellExecTool(BaseTool):
    """
    Validate shell.exec requests before runtime-session dispatch.

    Direct ``run`` calls fail closed because model-facing shell execution must
    occur through provider-backed PTY sessions, not host subprocesses.
    """

    args_model = ShellExecArgs

    def build_command(self, args: ShellExecArgs) -> List[str]:
        """Build a shell command list suitable for container execution."""
        return list(_build_shell_command(args))

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ShellExecArgs,
    ) -> Dict[str, Any]:
        """Parse execution output into structured metadata."""
        clean_stdout = _strip_kali_welcome(stdout) if stdout else ""
        metadata: Dict[str, Any] = {
            "command": args.command,
            "exit_code": exit_code,
            "success": exit_code == 0,
            "output_length": len(clean_stdout),
            "has_errors": bool(stderr),
            "transport": "runtime-session",
        }

        if stderr:
            metadata["error_lines"] = extract_error_lines(stderr, max_matches=5)

        return {"shell_exec": metadata}

    def run(self, args: ShellExecArgs) -> ToolResult:
        # Policy validation remains the first gate
        overall_start = time.time()
        policy = CommandPolicy()
        validation_errors = validate_shell_exec_command(args.command, policy=policy)

        if validation_errors:
            first_error = validation_errors[0]
            metadata = {
                "shell_exec": {
                    "command": args.command,
                    "exit_code": -1,
                    "success": False,
                    "output_length": 0,
                    "has_errors": True,
                    "transport": "runtime-session",
                },
                "policy_violation": {
                    "severity": "error",
                    "errors": validation_errors,
                },
            }
            tool_result = build_tool_result(
                success=False,
                start=overall_start,
                stdout="",
                stderr=f"Policy violation: {first_error.get('message', 'Command rejected')}",
                metadata=metadata,
                exit_code=-1,
            )
            return tool_result

        message = (
            "shell.exec is available only through runtime-session dispatch; "
            "direct adapter execution is disabled."
        )
        metadata = {
            "shell_exec": {
                "command": args.command,
                "exit_code": -1,
                "success": False,
                "output_length": 0,
                "has_errors": True,
                "transport": "runtime-session",
                "process_status": None,
                "session_id": None,
                "stdin_available": False,
                "error_code": "shell_runtime_unavailable",
            }
        }
        return build_tool_result(
            success=False,
            start=overall_start,
            stdout="",
            stderr=message,
            metadata=metadata,
            exit_code=-1,
        )
