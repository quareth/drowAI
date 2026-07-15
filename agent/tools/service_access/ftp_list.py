"""FTP directory listing tool using direct non-interactive lftp execution."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .common import (
    FtpListArgs,
    ServiceAccessToolMixin,
    command_tool_result,
    lftp_quote,
    service_metadata,
)


class FtpListTool(ServiceAccessToolMixin, BaseTool):
    """Authenticate to FTP and list one remote directory without an interactive session."""

    tool_id = "service_access.ftp_list"
    args_model = FtpListArgs
    operation_name = "ftp_list"
    planner_guidance = "Use only to list a known FTP directory with supplied credentials."

    def build_command(self, args: FtpListArgs) -> List[str]:
        commands = (
            f"set cmd:fail-exit yes; set net:timeout {args.timeout_seconds}; "
            f"ls {lftp_quote(args.remote_path)}; bye"
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

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: FtpListArgs) -> Dict[str, Any]:
        lines = [line for line in (stdout or "").splitlines() if line.strip()]
        return service_metadata(
            operation=self.operation_name,
            host=args.host,
            port=args.port,
            username=args.username,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            extra={
                "remote_path": args.remote_path,
                "entry_count": len(lines),
            },
        )

    def run(self, args: FtpListArgs) -> ToolResult:
        return command_tool_result(tool=self, args=args, command=self.build_command(args), start=time.time())
