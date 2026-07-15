"""ProxyTunnel tool for HTTP proxy tunneling and pivoting."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_PROXY_PORT = 8080
DEFAULT_TIMEOUT = 30


class ProxyTunnelArgs(BaseToolArgs):
    """Arguments for the ProxyTunnel tool."""

    proxy_host: str = Field(
        ...,
        description="Proxy host address (-p).",
        min_length=1,
    )
    proxy_port: int = Field(
        DEFAULT_PROXY_PORT,
        description="Proxy port.",
        ge=1,
        le=65535,
    )
    dest_host: str = Field(
        ...,
        description="Destination host to reach through proxy (-d).",
        min_length=1,
    )
    dest_port: int = Field(
        ...,
        description="Destination port to reach through proxy (-d).",
        ge=1,
        le=65535,
    )
    local_port: Optional[int] = Field(
        None,
        description="Local port to bind (-a).",
        ge=1,
        le=65535,
    )
    username: Optional[str] = Field(
        None,
        description="Username for proxy authentication.",
    )
    password: Optional[str] = Field(
        None,
        description="Password for proxy authentication.",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_proxytunnel_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse ProxyTunnel output into structured metadata."""
    metadata: Dict[str, Any] = {
        "connection_status": "unknown",
        "tunnel_established": False,
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
        elif "bytes" in line.lower() and "transferred" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["bytes_transferred"] = int(match.group(1))

    return metadata


class ProxyTunnelTool(BaseTool):
    """ProxyTunnel tool for HTTP proxy tunneling and pivoting."""

    args_model = ProxyTunnelArgs

    def build_command(self, args: ProxyTunnelArgs) -> List[str]:
        cmd: List[str] = [
            "proxytunnel",
            "-p",
            f"{args.proxy_host}:{args.proxy_port}",
            "-d",
            f"{args.dest_host}:{args.dest_port}",
        ]

        if args.local_port is not None:
            cmd.extend(["-a", str(args.local_port)])

        if args.username and args.password:
            cmd.extend(["-P", f"{args.username}:{args.password}"])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ProxyTunnelArgs,
    ) -> Dict[str, Any]:
        metadata = parse_proxytunnel_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["proxy"] = f"{args.proxy_host}:{args.proxy_port}"
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ProxyTunnelArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/proxytunnel_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ProxyTunnelArgs) -> ToolResult:
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
                stderr="proxytunnel command not found. Ensure ProxyTunnel is installed.",
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
        tool_id="maintaining_access.tunneling_pivoting.proxytunnel",
        display_name="ProxyTunnel",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="http_proxy_tunneling",
                description="Tunnel arbitrary TCP through HTTP proxies via the CONNECT method with optional auth; returns tunnel status and bytes transferred; use to pivot via outbound HTTP proxy.",
                output_indicators=["connected", "tunnel", "proxy"],
            ),
        ],
        required_services=["http"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=4,
    )
)
