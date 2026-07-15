"""DNS spoofing tool using Pydantic models."""

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
DEFAULT_INTERFACE = "any"
DEFAULT_TIMEOUT = 60


class DnsSpoofArgs(BaseToolArgs):
    """Arguments for the DNS spoofing tool."""

    interface: Optional[str] = Field(
        None,
        description="Network interface to use for spoofing.",
    )
    hosts_file: Optional[str] = Field(
        None,
        description="Hosts file mapping domains to spoofed IPs.",
    )
    capture_filter: Optional[str] = Field(
        None,
        description="BPF filter expression for DNS traffic.",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_dnsspoof_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse DNS spoof output into structured metadata."""
    metadata: Dict[str, Any] = {
        "queries_received": 0,
        "responses_sent": 0,
        "domains_spoofed": [],
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()
    for line in lines:
        lowered = line.lower()
        if "query" in lowered:
            metadata["queries_received"] += 1
        if "response" in lowered:
            metadata["responses_sent"] += 1
        domain_match = re.search(r"\b([a-z0-9.-]+\.[a-z]{2,})\b", line, re.IGNORECASE)
        if domain_match:
            metadata["domains_spoofed"].append(domain_match.group(1))
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warning" in lowered:
            metadata["warnings"].append(line.strip())

    metadata["domains_spoofed"] = sorted(set(metadata["domains_spoofed"]))
    return metadata


class DnsSpoofTool(BaseTool):
    """Run DNS spoofing attacks."""

    args_model = DnsSpoofArgs

    def build_command(self, args: DnsSpoofArgs) -> List[str]:
        cmd: List[str] = ["dnsspoof"]

        interface = args.interface or DEFAULT_INTERFACE
        cmd.extend(["-i", interface])

        if args.hosts_file:
            cmd.extend(["-f", args.hosts_file])
        if args.verbose:
            cmd.append("-v")

        if args.capture_filter:
            cmd.append(args.capture_filter)

        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DnsSpoofArgs,
    ) -> Dict[str, Any]:
        metadata = parse_dnsspoof_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DnsSpoofArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/dnsspoof_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: DnsSpoofArgs) -> ToolResult:
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
                stderr="dnsspoof command not found. Ensure dsniff is installed.",
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
        tool_id="sniffing_spoofing.spoofing_poisoning.dnsspoof",
        display_name="DnsSpoof",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.EXPLOITATION, PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="dns_spoofing",
                description="Spoof DNS responses to redirect domain queries to attacker-chosen IPs; returns spoofed query and response counts; active attack — requires hosts mapping file.",
                output_indicators=["dns", "response", "spoof"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
