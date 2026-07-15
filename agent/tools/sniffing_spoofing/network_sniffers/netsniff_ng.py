"""NetSniff-NG - High-performance network packet sniffer and analyzer."""

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


class NetSniffNGArgs(BaseToolArgs):
    """Arguments for the NetSniff-NG tool."""

    interface: Optional[str] = Field(
        None,
        description="Network interface to capture from.",
    )
    input_file: Optional[str] = Field(
        None,
        description="Input pcap file to analyze.",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file path for captured packets.",
    )
    packet_count: Optional[int] = Field(
        None,
        description="Number of packets to capture.",
        ge=1,
        le=1_000_000,
    )
    duration_seconds: Optional[int] = Field(
        None,
        description="Capture duration in seconds.",
        ge=1,
        le=3_600,
    )
    capture_filter: Optional[str] = Field(
        None,
        description="BPF filter to apply.",
    )
    snaplen: Optional[int] = Field(
        None,
        description="Snapshot length in bytes.",
        ge=64,
        le=65_535,
    )
    buffer_size_mb: Optional[int] = Field(
        None,
        description="Buffer size in MB.",
        ge=1,
        le=1_024,
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments for netsniff-ng.",
    )


def parse_netsniff_ng_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse NetSniff-NG output into structured metadata."""
    metadata: Dict[str, Any] = {
        "packet_count": 0,
        "bytes_captured": 0,
        "protocols": [],
        "hosts": [],
        "ports": [],
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()

    try:
        for line in lines:
            packet_match = re.search(r"(\d+)\s+packets?", line, re.IGNORECASE)
            if packet_match:
                metadata["packet_count"] = int(packet_match.group(1))

            bytes_match = re.search(r"(\d+)\s+bytes?", line, re.IGNORECASE)
            if bytes_match:
                metadata["bytes_captured"] = int(bytes_match.group(1))

        protocols = set(re.findall(r"\b([A-Z]{2,10})\b", combined))
        metadata["protocols"] = sorted(protocols)

        metadata["hosts"] = sorted(
            set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", combined))
        )
        metadata["ports"] = sorted(set(re.findall(r":(\d{1,5})\b", combined)))

        for line in lines:
            lowered = line.lower()
            if "error" in lowered:
                metadata["errors"].append(line.strip())
            elif "warning" in lowered:
                metadata["warnings"].append(line.strip())
    except Exception as exc:
        metadata["errors"].append(f"Failed to parse output: {exc}")

    return metadata


class NetSniffNGTool(BaseTool):
    """Run NetSniff-NG network packet analysis and parse the output."""

    args_model = NetSniffNGArgs

    def build_command(self, args: NetSniffNGArgs) -> List[str]:
        cmd: List[str] = ["netsniff-ng"]

        if args.input_file:
            cmd.extend(["-r", args.input_file])
        else:
            interface = args.interface or DEFAULT_INTERFACE
            cmd.extend(["-i", interface])

        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.packet_count:
            cmd.extend(["-c", str(args.packet_count)])
        if args.duration_seconds:
            cmd.extend(["-t", str(args.duration_seconds)])
        if args.capture_filter:
            cmd.extend(["-f", args.capture_filter])
        if args.snaplen:
            cmd.extend(["-s", str(args.snaplen)])
        if args.buffer_size_mb:
            cmd.extend(["-B", str(args.buffer_size_mb)])
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
        args: NetSniffNGArgs,
    ) -> Dict[str, Any]:
        metadata = parse_netsniff_ng_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: NetSniffNGArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/netsniff_ng_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: NetSniffNGArgs) -> ToolResult:
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
                stderr="netsniff-ng command not found. Ensure netsniff-ng is installed.",
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
        tool_id="sniffing_spoofing.network_sniffers.netsniff_ng",
        display_name="NetSniff-NG",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="high_performance_capture",
                description="Capture network packets at high speed with netsniff-ng zero-copy I/O; returns pcap output and packet count; prefer for bulk passive capture, not active probing or decoding",
                output_indicators=["netsniff", "packet", "bytes"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)
