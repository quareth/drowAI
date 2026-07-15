"""RPC enumeration tool using rpcinfo."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120


class RPCEnumArgs(BaseToolArgs):
    """Arguments for RPC enumeration via rpcinfo."""

    portmapper_only: bool = Field(
        default=True,
        description="Query portmapper services only (-p).",
    )
    transport: Optional[str] = Field(
        default=None,
        description="Transport protocol to use (tcp or udp).",
    )


def parse_rpcinfo_output(output_text: str) -> Dict[str, Any]:
    """Parse rpcinfo output into structured metadata."""
    metadata: Dict[str, Any] = {
        "services": [],
        "errors": [],
        "warnings": [],
    }

    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue

        if "program" in line.lower() and "proto" in line.lower():
            continue

        if "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
            continue

        match = re.match(
            r"^(?P<program>\d+)\s+(?P<version>\d+)\s+(?P<proto>\S+)\s+(?P<port>\d+)\s*(?P<service>.*)$",
            line,
        )
        if match:
            metadata["services"].append(
                {
                    "program": match.group("program"),
                    "version": match.group("version"),
                    "protocol": match.group("proto"),
                    "port": match.group("port"),
                    "service": match.group("service").strip(),
                }
            )
        elif "warning" in line.lower():
            metadata["warnings"].append(line)

    metadata["total_services"] = len(metadata["services"])
    return metadata


class RPCEnumTool(BaseTool):
    """RPC enumeration tool using rpcinfo."""

    args_model = RPCEnumArgs

    def build_command(self, args: RPCEnumArgs) -> List[str]:
        cmd = ["rpcinfo"]
        if args.portmapper_only:
            cmd.append("-p")
        if args.transport:
            cmd.extend(["-T", args.transport])
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: RPCEnumArgs,
    ) -> Dict[str, Any]:
        metadata = parse_rpcinfo_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: RPCEnumArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        path = f"artifacts/rpc_enum_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [path]

    def run(self, args: RPCEnumArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="rpcinfo command not found. Ensure rpcbind is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(
            proc.stdout, args=args, timestamp=int(start), stderr=proc.stderr
        )

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from agent.tools.enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="system_services.rpc_enum",
        display_name="rpcinfo",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="rpc_service_enumeration",
                description="Enumerates RPC services via rpcinfo.",
                output_indicators=["program", "version", "proto", "port"],
            ),
        ],
        required_services=["rpc"],
        target_protocols=["tcp", "udp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
