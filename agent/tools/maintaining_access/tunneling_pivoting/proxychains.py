"""ProxyChains tool for proxy chaining and pivoting."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_TIMEOUT = 30


class ProxyChainsArgs(BaseToolArgs):
    """Arguments for the ProxyChains tool."""

    config_file: Optional[str] = Field(
        None,
        description="Path to ProxyChains configuration file (-f).",
    )
    quiet: bool = Field(
        False,
        description="Quiet mode (-q).",
    )
    command: str = Field(
        ...,
        description="Command to execute through proxy chain.",
    )
    command_args: List[str] = Field(
        default_factory=list,
        description="Arguments for the command to execute.",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_proxychains_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse ProxyChains output into structured metadata."""
    metadata: Dict[str, Any] = {
        "proxies_used": [],
        "connection_status": "unknown",
        "total_proxies": 0,
        "errors": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    for line in combined.splitlines():
        line = line.strip()
        if not line:
            continue

        if "proxychains" in line.lower() and ":" in line:
            metadata["proxies_used"].append(line)
            metadata["total_proxies"] += 1
        elif "connected" in line.lower():
            metadata["connection_status"] = "connected"
        elif "failed" in line.lower() or "error" in line.lower():
            metadata["connection_status"] = "failed"
            metadata["errors"].append(line)
    
    return metadata


class ProxyChainsTool(BaseTool):
    """ProxyChains tool for proxy chaining and pivoting."""
    
    args_model = ProxyChainsArgs

    def build_command(self, args: ProxyChainsArgs) -> List[str]:
        cmd: List[str] = ["proxychains4"]

        if args.quiet:
            cmd.append("-q")

        if args.config_file:
            cmd.extend(["-f", args.config_file])

        cmd.append(args.command)
        if args.command_args:
            cmd.extend(args.command_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ProxyChainsArgs,
    ) -> Dict[str, Any]:
        metadata = parse_proxychains_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["command"] = args.command
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ProxyChainsArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/proxychains_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ProxyChainsArgs) -> ToolResult:
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
                stderr="proxychains4 command not found. Ensure ProxyChains is installed.",
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
        tool_id="maintaining_access.tunneling_pivoting.proxychains",
        display_name="ProxyChains",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="proxy_chaining",
                description="Route an arbitrary command's TCP connections through a configured SOCKS or HTTP proxy chain; returns wrapped command output; use to pivot existing tools through compromised hosts.",
                output_indicators=["proxychains", "connected", "proxy"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=3,
    )
)
