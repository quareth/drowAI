"""SSH password login proof tool using direct non-interactive sshpass execution."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .common import SshLoginArgs, ServiceAccessToolMixin, command_tool_result, service_metadata


class SshLoginTool(ServiceAccessToolMixin, BaseTool):
    """Authenticate to SSH with one supplied password and run an inert proof command."""

    tool_id = "service_access.ssh_login"
    args_model = SshLoginArgs
    operation_name = "ssh_login"
    planner_guidance = "Use only to prove one supplied SSH username/password works; no brute force."

    def build_command(self, args: SshLoginArgs) -> List[str]:
        return [
            "sshpass",
            "-p",
            args.password,
            "ssh",
            "-o",
            "BatchMode=no",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={args.timeout_seconds}",
            "-p",
            str(args.port),
            f"{args.username}@{args.host}",
            "true",
        ]

    def parse_output(self, stdout: str, stderr: str, exit_code: int, args: SshLoginArgs) -> Dict[str, Any]:
        return service_metadata(
            operation=self.operation_name,
            host=args.host,
            port=args.port,
            username=args.username,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    def run(self, args: SshLoginArgs) -> ToolResult:
        return command_tool_result(tool=self, args=args, command=self.build_command(args), start=time.time())
