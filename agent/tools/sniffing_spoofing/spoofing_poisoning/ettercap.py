"""Ettercap ARP poisoning and spoofing tool using Pydantic models."""

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


class EttercapPoisonArgs(BaseToolArgs):
    """Arguments for the Ettercap poisoning tool."""

    interface: Optional[str] = Field(
        None,
        description="Network interface to use for poisoning.",
    )
    mitm_method: str = Field(
        "arp:remote",
        description="MITM method for poisoning (e.g., arp:remote, arp:oneway, icmp).",
    )
    target1: Optional[str] = Field(
        None,
        description="First target (victim).",
    )
    target2: Optional[str] = Field(
        None,
        description="Second target (gateway/router).",
    )
    plugins: Optional[List[str]] = Field(
        None,
        description="Plugins to load for the attack.",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file for captured data.",
    )
    log_file: Optional[str] = Field(
        None,
        description="Log file for ettercap output.",
    )
    quiet: bool = Field(
        False,
        description="Quiet mode - minimal output.",
    )
    one_way: bool = Field(
        False,
        description="One-way poisoning (target1 -> target2 only).",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_ettercap_poison_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse ettercap poisoning output into structured metadata."""
    metadata: Dict[str, Any] = {
        "targets": [],
        "attacks": [],
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
        target_match = re.search(r"Target (\d+):\s+([^\s]+)", line)
        if target_match:
            targets.append(
                {"id": int(target_match.group(1)), "address": target_match.group(2)}
            )
    metadata["targets"] = targets

    attacks = []
    for line in lines:
        attack_match = re.search(r"(ARP|DNS|DHCP)\s+spoof", line, re.IGNORECASE)
        if attack_match:
            attacks.append({"type": attack_match.group(1).lower(), "status": line.strip()})
    metadata["attacks"] = attacks

    stats: Dict[str, Any] = {}
    for line in lines:
        hosts_match = re.search(r"(\d+)\s+hosts\s+poisoned", line, re.IGNORECASE)
        if hosts_match:
            stats["hosts_poisoned"] = int(hosts_match.group(1))
        packets_match = re.search(r"(\d+)\s+packets\s+captured", line, re.IGNORECASE)
        if packets_match:
            stats["packets_captured"] = int(packets_match.group(1))
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


class EttercapPoisonTool(BaseTool):
    """Run Ettercap ARP poisoning and spoofing attacks."""

    args_model = EttercapPoisonArgs

    def build_command(self, args: EttercapPoisonArgs) -> List[str]:
        cmd: List[str] = ["ettercap", "-T"]

        if args.quiet:
            cmd.append("-q")
        if args.interface:
            cmd.extend(["-i", args.interface])
        if args.mitm_method:
            cmd.extend(["-M", args.mitm_method])
        if args.output_file:
            cmd.extend(["-w", args.output_file])
        if args.log_file:
            cmd.extend(["-L", args.log_file])
        if args.one_way:
            cmd.append("-o")
        if args.plugins:
            for plugin in args.plugins:
                cmd.extend(["-P", plugin])

        target1 = args.target1 or args.target
        cmd.append(f"/{target1}/")
        if args.target2:
            cmd.append(f"/{args.target2}/")

        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: EttercapPoisonArgs,
    ) -> Dict[str, Any]:
        metadata = parse_ettercap_poison_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["mitm_method"] = args.mitm_method
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: EttercapPoisonArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/ettercap_poison_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: EttercapPoisonArgs) -> ToolResult:
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
                stderr="ettercap command not found. Ensure ettercap is installed.",
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
        tool_id="sniffing_spoofing.spoofing_poisoning.ettercap",
        display_name="Ettercap",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.EXPLOITATION, PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="mitm_poisoning",
                description="Poison LAN targets via ARP, ICMP, or DNS with Ettercap plugins; returns poisoned host count and intercepted connection metrics; active — intercepts and rewrites traffic.",
                output_indicators=["ettercap", "poison", "target"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
