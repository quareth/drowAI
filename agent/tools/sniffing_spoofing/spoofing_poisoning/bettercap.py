"""Bettercap network security tool using Pydantic models."""

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
DEFAULT_TIMEOUT = 60


class BettercapArgs(BaseToolArgs):
    """Arguments for the Bettercap tool."""

    interface: Optional[str] = Field(
        None,
        description="Network interface to use.",
    )
    caplet_file: Optional[str] = Field(
        None,
        description="Caplet script file to execute.",
    )
    eval_commands: Optional[List[str]] = Field(
        None,
        description="Commands to execute non-interactively.",
    )
    gateway_ip: Optional[str] = Field(
        None,
        description="Gateway IP address override.",
    )
    silent: bool = Field(
        False,
        description="Suppress banner output.",
    )
    no_colors: bool = Field(
        False,
        description="Disable colored output.",
    )
    debug: bool = Field(
        False,
        description="Enable debug output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_bettercap_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse bettercap output into structured metadata."""
    metadata: Dict[str, Any] = {
        "targets": [],
        "modules": [],
        "statistics": {},
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()
    targets = []
    for line in lines:
        target_match = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})", line)
        if target_match:
            targets.append(
                {"ip": target_match.group(1), "mac": target_match.group(2)}
            )

    metadata["targets"] = targets

    modules = []
    for line in lines:
        module_match = re.search(r"\[(.*)\]\s+module\s+(\S+)", line, re.IGNORECASE)
        if module_match:
            modules.append(
                {"status": module_match.group(1), "name": module_match.group(2)}
            )
    metadata["modules"] = modules

    stats: Dict[str, Any] = {}
    for line in lines:
        hosts_match = re.search(r"(\d+)\s+hosts", line, re.IGNORECASE)
        if hosts_match:
            stats["hosts"] = int(hosts_match.group(1))
        packets_match = re.search(r"(\d+)\s+packets", line, re.IGNORECASE)
        if packets_match:
            stats["packets"] = int(packets_match.group(1))
        connections_match = re.search(r"(\d+)\s+connections", line, re.IGNORECASE)
        if connections_match:
            stats["connections"] = int(connections_match.group(1))

        lowered = line.lower()
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warning" in lowered:
            metadata["warnings"].append(line.strip())

    metadata["statistics"] = stats
    return metadata


class BettercapTool(BaseTool):
    """Run Bettercap network analysis and attacks."""

    args_model = BettercapArgs

    def build_command(self, args: BettercapArgs) -> List[str]:
        cmd: List[str] = ["bettercap"]

        if args.interface:
            cmd.extend(["-iface", args.interface])
        if args.caplet_file:
            cmd.extend(["-caplet", args.caplet_file])
        if args.eval_commands:
            cmd.extend(["-eval", "; ".join(args.eval_commands)])
        if args.gateway_ip:
            cmd.extend(["-gateway-override", args.gateway_ip])
        if args.silent:
            cmd.append("-silent")
        if args.no_colors:
            cmd.append("-no-colors")
        if args.debug:
            cmd.append("-debug")
        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BettercapArgs,
    ) -> Dict[str, Any]:
        metadata = parse_bettercap_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: BettercapArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/bettercap_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: BettercapArgs) -> ToolResult:
        start = time.time()
        timeout = args.timeout or DEFAULT_TIMEOUT

        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={"timeout": timeout},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="bettercap command not found. Ensure bettercap is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
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
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="sniffing_spoofing.spoofing_poisoning.bettercap",
        display_name="Bettercap",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.EXPLOITATION, PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="mitm_framework",
                description="Run Bettercap MITM modules (ARP, DNS, HTTP/HTTPS, spoofing) against LAN targets; returns module status, target discovery, and packet counts; active — intercepts and modifies traffic.",
                output_indicators=["bettercap", "module", "target"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=5,
    )
)
