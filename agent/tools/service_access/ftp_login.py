"""FTP login proof tool using direct non-interactive lftp execution."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .common import (
    FtpLoginArgs,
    ServiceAccessToolMixin,
    command_tool_result,
    service_metadata,
)


class FtpLoginTool(ServiceAccessToolMixin, BaseTool):
    """Authenticate to FTP and run a minimal pwd proof without opening a session."""

    tool_id = "service_access.ftp_login"
    args_model = FtpLoginArgs
    operation_name = "ftp_login"
    planner_guidance = "Use only to prove one supplied FTP username/password works; no brute force."

    def build_command(self, args: FtpLoginArgs) -> List[str]:
        commands = (
            f"set cmd:fail-exit yes; set net:timeout {args.timeout_seconds}; "
            "pwd; bye"
        )
        return [
            "lftp",
            "-u",
            f"{args.username},{args.password}",
            "-p",
            str(args.port),
            "-e",
            commands,
            args.host,
        ]

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: FtpLoginArgs) -> Dict[str, Any]:
        return service_metadata(
            operation=self.operation_name,
            host=args.host,
            port=args.port,
            username=args.username,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    def run(self, args: FtpLoginArgs) -> ToolResult:
        return command_tool_result(tool=self, args=args, command=self.build_command(args), start=time.time())
