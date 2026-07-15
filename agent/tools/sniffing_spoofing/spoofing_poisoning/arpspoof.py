"""ARP spoofing tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_INTERFACE = "any"
DEFAULT_TIMEOUT = 60


class ArpCacheMode(str, Enum):
    """ARP cache poisoning modes for arpspoof."""

    OWN = "own"
    HOST = "host"
    BOTH = "both"


class ArpSpoofArgs(BaseToolArgs):
    """Arguments for the ARP spoofing tool."""

    interface: Optional[str] = Field(
        None,
        description="Network interface to use for spoofing.",
    )
    gateway_ip: Optional[str] = Field(
        None,
        description="Gateway IP address to spoof.",
    )
    target_ip: Optional[str] = Field(
        None,
        description="Target IP address to poison.",
    )
    cache_mode: Optional[ArpCacheMode] = Field(
        None,
        description="ARP cache poisoning direction (own/host/both).",
    )
    bidirectional: bool = Field(
        False,
        description="Poison both directions (-r).",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_arpspoof_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse ARP spoof output into structured metadata."""
    metadata: Dict[str, Any] = {
        "packets_sent": 0,
        "targets_spoofed": 0,
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()
    for line in lines:
        lowered = line.lower()
        if "arp reply" in lowered or "packet" in lowered:
            metadata["packets_sent"] += 1
        if "spoof" in lowered and "to" in lowered:
            metadata["targets_spoofed"] += 1
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warning" in lowered:
            metadata["warnings"].append(line.strip())

    return metadata


class ArpSpoofTool(BaseTool):
    """Run ARP spoofing attacks."""

    args_model = ArpSpoofArgs

    def build_command(self, args: ArpSpoofArgs) -> List[str]:
        cmd: List[str] = ["arpspoof"]

        interface = args.interface or DEFAULT_INTERFACE
        cmd.extend(["-i", interface])

        if args.cache_mode:
            cmd.extend(["-c", args.cache_mode.value])
        if args.target_ip:
            cmd.extend(["-t", args.target_ip])
        if args.bidirectional:
            cmd.append("-r")
        if args.verbose:
            cmd.append("-v")

        gateway = args.gateway_ip or args.target
        cmd.append(gateway)

        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ArpSpoofArgs,
    ) -> Dict[str, Any]:
        metadata = parse_arpspoof_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["gateway"] = args.gateway_ip or args.target
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ArpSpoofArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/arpspoof_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ArpSpoofArgs) -> ToolResult:
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
                stderr="arpspoof command not found. Ensure dsniff is installed.",
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
        tool_id="sniffing_spoofing.spoofing_poisoning.arpspoof",
        display_name="ArpSpoof",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.EXPLOITATION, PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="arp_poisoning",
                description="Poison ARP cache on a target IP to intercept LAN traffic; returns spoof confirmation and injected packet counts; active attack — modifies network state.",
                output_indicators=["arp", "spoof", "packet"],
            ),
        ],
        required_services=[],
        target_protocols=["arp"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
