"""DNS2TCP tool for DNS tunneling and pivoting."""

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


class DNS2TCPMode(str, Enum):
    """Supported DNS2TCP modes."""

    CLIENT = "client"
    SERVER = "server"


class DNS2TCPArgs(BaseToolArgs):
    """Arguments for the DNS2TCP tool."""

    mode: DNS2TCPMode = Field(
        DNS2TCPMode.CLIENT,
        description="DNS2TCP mode to use (client or server).",
    )
    domain: str = Field(
        ...,
        description="DNS domain/zone used for tunneling (-z).",
        min_length=1,
    )
    server: str = Field(
        ...,
        description="DNS2TCP server address (client mode).",
        min_length=1,
    )
    resource: Optional[str] = Field(
        None,
        description="Resource identifier for the tunnel (-r).",
    )
    key: Optional[str] = Field(
        None,
        description="Encryption key for DNS tunnel (-k).",
    )
    local_port: Optional[int] = Field(
        None,
        description="Local port to listen on (-l).",
        ge=1,
        le=65535,
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_dns2tcp_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse DNS2TCP output into structured metadata."""
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


class DNS2TCPTool(BaseTool):
    """DNS2TCP tool for DNS tunneling and pivoting."""

    args_model = DNS2TCPArgs

    def build_command(self, args: DNS2TCPArgs) -> List[str]:
        if args.mode == DNS2TCPMode.SERVER:
            cmd: List[str] = ["dns2tcpd", "-z", args.domain]
            if args.key:
                cmd.extend(["-k", args.key])
            if args.resource:
                cmd.extend(["-r", args.resource])
            if args.local_port is not None:
                cmd.extend(["-l", str(args.local_port)])
            return cmd

        if not args.server:
            raise ValueError("server is required for client mode.")

        cmd = ["dns2tcpc", "-z", args.domain]
        if args.key:
            cmd.extend(["-k", args.key])
        if args.resource:
            cmd.extend(["-r", args.resource])
        if args.local_port is not None:
            cmd.extend(["-l", str(args.local_port)])
        cmd.append(args.server)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DNS2TCPArgs,
    ) -> Dict[str, Any]:
        metadata = parse_dns2tcp_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["mode"] = args.mode.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DNS2TCPArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/dns2tcp_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: DNS2TCPArgs) -> ToolResult:
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
                stderr="dns2tcp command not found. Ensure DNS2TCP is installed.",
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
        tool_id="maintaining_access.tunneling_pivoting.dns2tcp",
        display_name="DNS2TCP",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="dns_tunneling",
                description="Tunnel TCP traffic through DNS queries (client or server); returns tunnel status and DNS query and byte counts; use to exfiltrate or pivot via DNS.",
                output_indicators=["tunnel", "dns", "connected"],
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
