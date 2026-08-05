"""Model-facing shell.write_stdin adapter for runtime-session input.

This module validates the public write/poll schema and documents input
semantics. It does not route providers, execute subprocesses, or manage shell
session state; those responsibilities belong to runtime-session dispatch and
the provider-backed shell-session service.
"""

from __future__ import annotations

import time

from runtime_shared.shell_session_contracts import ShellWriteRequest

from ..base_tool import BaseTool
from ..schemas import ToolResult
from ._helpers import build_tool_result
from .contracts import ShellWriteStdinArgs


class ShellWriteStdinTool(BaseTool):
    """
    Validate shell.write_stdin requests before runtime-session dispatch.

    ``chars=""`` polls for more output. ``chars="\\u0003"`` requests an
    interrupt. Other non-empty input is passed through exactly as provided by
    the runtime-session service path; this adapter never appends a newline.
    """

    args_model = ShellWriteStdinArgs

    def build_request(self, args: ShellWriteStdinArgs) -> ShellWriteRequest:
        """Build the service-bound request without modifying stdin characters."""

        return ShellWriteRequest(**args.model_dump())

    def run(self, args: ShellWriteStdinArgs) -> ToolResult:
        """Fail closed because direct adapter execution cannot own sessions."""

        overall_start = time.time()
        request = self.build_request(args)
        message = (
            "shell.write_stdin is available only through runtime-session dispatch; "
            "direct adapter execution is disabled."
        )
        metadata = {
            "shell_write_stdin": {
                "session_id": request.session_id,
                "exit_code": -1,
                "success": False,
                "output_length": 0,
                "has_errors": True,
                "transport": "runtime-session",
                "process_status": None,
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


__all__ = ["ShellWriteStdinTool"]
