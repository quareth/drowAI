"""PTunnel tool for ICMP tunneling and pivoting."""

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


class PTunnelMode(str, Enum):
    """Supported PTunnel modes."""

    CLIENT = "client"
    SERVER = "server"


class PTunnelArgs(BaseToolArgs):
    """Arguments for the PTunnel tool."""

    mode: PTunnelMode = Field(
        PTunnelMode.CLIENT,
        description="PTunnel mode to use (client or server).",
    )
    proxy_host: str = Field(
        ...,
        description="PTunnel proxy/server address (-p).",
        min_length=1,
    )
    local_port: int = Field(
        ...,
        description="Local port to bind (-l).",
        ge=1,
        le=65535,
    )
    remote_host: str = Field(
        ...,
        description="Remote host to connect to (-r).",
        min_length=1,
    )
    remote_port: int = Field(
        ...,
        description="Remote port to connect to (-R).",
        ge=1,
        le=65535,
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_ptunnel_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse PTunnel output into structured metadata."""
    metadata: Dict[str, Any] = {
        "connection_status": "unknown",
        "tunnel_established": False,
        "packets_sent": 0,
        "packets_received": 0,
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
        elif "packets sent" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["packets_sent"] = int(match.group(1))
        elif "packets received" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["packets_received"] = int(match.group(1))

    return metadata


class PTunnelTool(BaseTool):
    """PTunnel tool for ICMP tunneling and pivoting."""

    args_model = PTunnelArgs

    def build_command(self, args: PTunnelArgs) -> List[str]:
        if args.mode == PTunnelMode.SERVER:
            return ["ptunnel-ng", "-s"]

        if not args.proxy_host:
            raise ValueError("proxy_host is required for client mode.")
        if args.local_port is None:
            raise ValueError("local_port is required for client mode.")
        if not args.remote_host:
            raise ValueError("remote_host is required for client mode.")
        if args.remote_port is None:
            raise ValueError("remote_port is required for client mode.")

        return [
            "ptunnel-ng",
            "-p",
            args.proxy_host,
            "-l",
            str(args.local_port),
            "-r",
            args.remote_host,
            "-R",
            str(args.remote_port),
        ]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: PTunnelArgs,
    ) -> Dict[str, Any]:
        metadata = parse_ptunnel_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["mode"] = args.mode.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: PTunnelArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/ptunnel_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: PTunnelArgs) -> ToolResult:
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
                stderr="ptunnel-ng command not found. Ensure PTunnel is installed.",
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
        tool_id="maintaining_access.tunneling_pivoting.ptunnel",
        display_name="PTunnel",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="icmp_tunneling",
                description="Tunnel TCP traffic between hosts over ICMP echo packets (client or server); returns tunnel status and packet counts; use to bypass firewalls that allow ping.",
                output_indicators=["ptunnel", "tunnel", "connected"],
            ),
        ],
        required_services=["icmp"],
        target_protocols=["icmp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=6,
    )
)
