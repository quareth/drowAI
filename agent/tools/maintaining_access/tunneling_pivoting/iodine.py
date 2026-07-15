"""Iodine tool for DNS tunneling and pivoting."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_TIMEOUT = 30


class IodineMode(str, Enum):
    """Supported Iodine modes."""

    CLIENT = "client"
    SERVER = "server"


class IodineArgs(BaseToolArgs):
    """Arguments for the Iodine tool."""

    mode: IodineMode = Field(
        IodineMode.CLIENT,
        description="Iodine mode to use (client or server).",
    )
    top_domain: str = Field(
        ...,
        description="DNS top domain for tunneling.",
        min_length=1,
    )
    server: str = Field(
        ...,
        description="DNS server address (client mode).",
        min_length=1,
    )
    server_ip: Optional[str] = Field(
        None,
        description="Server bind IP for iodined (server mode).",
    )
    password: Optional[str] = Field(
        None,
        description="Password for tunnel authentication (-P).",
    )
    foreground: bool = Field(
        True,
        description="Run in foreground (-f).",
    )
    mtu: Optional[int] = Field(
        None,
        description="MTU size (-m).",
        ge=576,
        le=1500,
    )
    max_downstream: Optional[int] = Field(
        None,
        description="Maximum downstream bandwidth (-M).",
        ge=1,
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_iodine_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse Iodine output into structured metadata."""
    metadata: Dict[str, Any] = {
        "connection_status": "unknown",
        "tunnel_established": False,
        "dns_queries": 0,
        "bytes_transferred": 0,
        "errors": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    for line in combined.splitlines():
        line = line.strip()
        if not line:
            continue

        if "connected" in line.lower() or "tunnel established" in line.lower():
            metadata["connection_status"] = "connected"
            metadata["tunnel_established"] = True
        elif "failed" in line.lower() or "error" in line.lower():
            metadata["connection_status"] = "failed"
            metadata["errors"].append(line)
        elif "dns query" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["dns_queries"] = int(match.group(1))
        elif "bytes" in line.lower() and "transferred" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["bytes_transferred"] = int(match.group(1))

    return metadata


class IodineTool(BaseTool):
    """Iodine tool for DNS tunneling and pivoting."""

    args_model = IodineArgs

    def build_command(self, args: IodineArgs) -> List[str]:
        if args.mode == IodineMode.SERVER:
            if not args.server_ip:
                raise ValueError("server_ip is required for server mode.")
            cmd: List[str] = ["iodined"]
            if args.foreground:
                cmd.append("-f")
            if args.password:
                cmd.extend(["-P", args.password])
            if args.mtu is not None:
                cmd.extend(["-m", str(args.mtu)])
            cmd.append(args.server_ip)
            cmd.append(args.top_domain)
            return cmd

        if not args.server:
            raise ValueError("server is required for client mode.")

        cmd = ["iodine"]
        if args.foreground:
            cmd.append("-f")
        if args.password:
            cmd.extend(["-P", args.password])
        if args.mtu is not None:
            cmd.extend(["-m", str(args.mtu)])
        if args.max_downstream is not None:
            cmd.extend(["-M", str(args.max_downstream)])
        cmd.append(args.server)
        cmd.append(args.top_domain)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: IodineArgs,
    ) -> Dict[str, Any]:
        metadata = parse_iodine_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["mode"] = args.mode.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: IodineArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/iodine_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: IodineArgs) -> ToolResult:
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
                stderr="iodine command not found. Ensure Iodine is installed.",
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
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))

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
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="maintaining_access.tunneling_pivoting.iodine",
        display_name="Iodine",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="dns_tunneling",
                description="Tunnel full IP traffic over DNS with iodine (client or server); returns tunnel status, DNS queries, and bandwidth; use for covert pivoting where DNS is permitted.",
                output_indicators=["iodine", "tunnel", "connected"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=6,
    )
)
