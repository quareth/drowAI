"""Responder LLMNR/NBT-NS/MDNS spoofing tool using Pydantic models."""

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


class ResponderArgs(BaseToolArgs):
    """Arguments for the Responder tool."""

    interface: str = Field(
        ...,
        description="Network interface to use (required by responder).",
    )
    analyze: bool = Field(
        False,
        description="Analyze mode - only analyze traffic (-A).",
    )
    wpad: bool = Field(
        False,
        description="Enable WPAD rogue proxy (-w).",
    )
    wredir: bool = Field(
        False,
        description="Enable NetBIOS wredir responses (-r).",
    )
    dhcp: bool = Field(
        False,
        description="Enable DHCP answers (-d).",
    )
    fingerprint: bool = Field(
        False,
        description="Enable OS fingerprinting (-f).",
    )
    verbose: bool = Field(
        False,
        description="Verbose mode (-v).",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_responder_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse responder output into structured metadata."""
    metadata: Dict[str, Any] = {
        "responses": [],
        "targets": [],
        "statistics": {},
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()

    responses = []
    for line in lines:
        response_match = re.search(
            r"\[([^\]]+)\]\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)",
            line,
        )
        if response_match:
            responses.append(
                {
                    "service": response_match.group(1),
                    "source_ip": response_match.group(2),
                    "source_name": response_match.group(3),
                    "requested_name": response_match.group(4),
                }
            )
    metadata["responses"] = responses

    targets = []
    for line in lines:
        target_match = re.search(r"Target\s+([^\s]+)\s+([^\s]+)", line)
        if target_match:
            targets.append(
                {"ip": target_match.group(1), "name": target_match.group(2)}
            )
    metadata["targets"] = targets

    stats: Dict[str, Any] = {}
    for line in lines:
        responses_match = re.search(r"(\d+)\s+responses\s+sent", line)
        if responses_match:
            stats["responses_sent"] = int(responses_match.group(1))
        requests_match = re.search(r"(\d+)\s+requests\s+received", line)
        if requests_match:
            stats["requests_received"] = int(requests_match.group(1))
        creds_match = re.search(r"(\d+)\s+credentials\s+captured", line)
        if creds_match:
            stats["credentials_captured"] = int(creds_match.group(1))

        lowered = line.lower()
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warning" in lowered:
            metadata["warnings"].append(line.strip())

    metadata["statistics"] = stats
    return metadata


class ResponderTool(BaseTool):
    """Run Responder LLMNR/NBT-NS/MDNS spoofing tool."""

    args_model = ResponderArgs

    def build_command(self, args: ResponderArgs) -> List[str]:
        cmd: List[str] = ["responder", "-I", args.interface]

        if args.analyze:
            cmd.append("-A")
        if args.wpad:
            cmd.append("-w")
        if args.wredir:
            cmd.append("-r")
        if args.dhcp:
            cmd.append("-d")
        if args.fingerprint:
            cmd.append("-f")
        if args.verbose:
            cmd.append("-v")
        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ResponderArgs,
    ) -> Dict[str, Any]:
        metadata = parse_responder_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["interface"] = args.interface
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ResponderArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/responder_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ResponderArgs) -> ToolResult:
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
                stderr="responder command not found. Ensure responder is installed.",
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
        tool_id="sniffing_spoofing.spoofing_poisoning.responder",
        display_name="Responder",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.EXPLOITATION, PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="llmnr_poisoning",
                description="Poison LLMNR, NBT-NS, and MDNS name resolution to capture NTLMv2 hashes and credentials; returns responses sent and credentials captured; active — Windows networks.",
                output_indicators=["LLMNR", "NBT-NS", "MDNS"],
            ),
        ],
        required_services=[],
        target_protocols=["llmnr", "nbt-ns", "mdns"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
