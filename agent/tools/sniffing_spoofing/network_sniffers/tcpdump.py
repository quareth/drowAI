"""Tcpdump network packet analyzer tool using Pydantic models."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

DEFAULT_INTERFACE = "any"
DEFAULT_TIMEOUT = 60
MAX_VERBOSITY = 3
TCPDUMP_PACKET_LIMIT = 200
TCPDUMP_HARD_TIMEOUT_SECONDS = 15
TCPDUMP_DEFAULT_SNAPLEN = 256
TCPDUMP_TIMEOUT_EXIT_CODE = 124
TCPDUMP_ARTIFACT_DIR = "artifacts"


def _default_pcap_path() -> str:
    """Return a unique workspace-relative tcpdump PCAP path."""

    return f"{TCPDUMP_ARTIFACT_DIR}/tcpdump_{uuid4().hex}.pcap"


def _validate_workspace_relative_pcap_path(value: str) -> str:
    """Validate tcpdump output stays inside the task workspace."""

    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("write_file must not be empty")
    path = Path(normalized)
    if path.is_absolute():
        raise ValueError("write_file must be workspace-relative")
    if any(part == ".." for part in path.parts):
        raise ValueError("write_file must not contain '..' path segments")
    if path.suffix.lower() != ".pcap":
        raise ValueError("write_file must use a .pcap extension")
    return path.as_posix()


def _ensure_pcap_parent(relative_path: str) -> None:
    """Create the capture output directory when the transport shares cwd."""

    parent = os.path.dirname(relative_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


class ProtocolFilter(str, Enum):
    """Common protocol filters for packet capture."""

    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"
    ARP = "arp"
    IP = "ip"
    IP6 = "ip6"
    ETHER = "ether"


class TcpdumpPlannerArgs(BaseModel):
    """Planner-facing tcpdump arguments without runtime guardrail controls."""

    model_config = ConfigDict(extra="forbid")

    interface: Optional[str] = Field(
        None,
        description="Network interface to capture from (e.g., eth0, wlan0).",
    )
    protocol: Optional[ProtocolFilter] = Field(
        None,
        description="Protocol filter to apply.",
    )
    host: Optional[str] = Field(
        None,
        description="Host address to filter on.",
    )
    port: Optional[int] = Field(
        None,
        description="Port number to filter on.",
        ge=1,
        le=65_535,
    )
    bpf_filter: Optional[str] = Field(
        None,
        description="Custom BPF filter expression.",
    )
    verbose_level: int = Field(
        0,
        description="Verbosity level (0-3).",
        ge=0,
        le=MAX_VERBOSITY,
    )
    quiet: bool = Field(
        False,
        description="Suppress output except for errors.",
    )
    include_payload: bool = Field(
        False,
        description=(
            "Include plaintext packet payload bytes in the generated PCAP up to "
            "the tool snap length. This cannot decode encrypted HTTPS/TLS "
            "payloads; leave false when host, port, protocol, or timing "
            "metadata is enough."
        ),
    )


class TcpdumpArgs(BaseToolArgs):
    """Arguments for the tcpdump tool."""

    timeout: Optional[int] = Field(
        None,
        description="Outer execution timeout for direct non-planner calls.",
        exclude=True,
    )
    interface: Optional[str] = Field(
        None,
        description="Network interface to capture from (e.g., eth0, wlan0).",
    )
    protocol: Optional[ProtocolFilter] = Field(
        None,
        description="Protocol filter to apply.",
    )
    port: Optional[int] = Field(
        None,
        description="Port number to filter on.",
        ge=1,
        le=65_535,
    )
    host: Optional[str] = Field(
        None,
        description="Host address to filter on.",
    )
    packet_count: Optional[int] = Field(
        None,
        description="Number of packets to capture.",
        ge=1,
    )
    duration_seconds: Optional[int] = Field(
        None,
        description="Duration in seconds to capture.",
        ge=1,
        le=3_600,
    )
    snaplen: Optional[int] = Field(
        None,
        description="Snapshot length (bytes to capture per packet).",
        ge=64,
        le=65_535,
    )
    verbose_level: int = Field(
        0,
        description="Verbosity level (0-3).",
        ge=0,
        le=MAX_VERBOSITY,
    )
    quiet: bool = Field(
        False,
        description="Suppress output except for errors.",
    )
    include_payload: bool = Field(
        False,
        description=(
            "Include plaintext packet payload bytes in the generated PCAP up to "
            "the tool snap length. This cannot decode encrypted HTTPS/TLS "
            "payloads; leave false when host, port, protocol, or timing "
            "metadata is enough."
        ),
    )
    write_file: str = Field(
        default_factory=_default_pcap_path,
        description="Workspace-relative PCAP output path.",
    )
    bpf_filter: Optional[str] = Field(
        None,
        description="Custom BPF filter expression.",
    )

    @field_validator("write_file", mode="before")
    @classmethod
    def _normalize_write_file(cls, value: Any) -> str:
        if value is None or not str(value).strip():
            return _default_pcap_path()
        return _validate_workspace_relative_pcap_path(str(value))


def parse_tcpdump_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse tcpdump output into structured metadata."""
    metadata: Dict[str, Any] = {
        "packets": [],
        "statistics": {"total_packets": 0, "protocols": {}},
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()
    packets = []

    for line in lines:
        if not line.strip():
            continue

        packet_info: Dict[str, Any] = {}

        timestamp_match = re.search(r"(\d{2}:\d{2}:\d{2}\.\d{6})", line)
        if timestamp_match:
            packet_info["timestamp"] = timestamp_match.group(1)

        src_dst_match = re.search(r"(\S+) > (\S+):", line)
        if src_dst_match:
            packet_info["source"] = src_dst_match.group(1)
            packet_info["destination"] = src_dst_match.group(2)

        protocol_match = re.search(r"\b([A-Z]{2,8})\b", line)
        if protocol_match:
            packet_info["protocol"] = protocol_match.group(1).lower()

        length_match = re.search(r"length (\d+)", line)
        if length_match:
            packet_info["length"] = int(length_match.group(1))

        if packet_info:
            packets.append(packet_info)

        lowered = line.lower()
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warning" in lowered:
            metadata["warnings"].append(line.strip())

    metadata["packets"] = packets
    metadata["statistics"]["total_packets"] = len(packets)

    captured_match = re.search(r"(\d+)\s+packets captured", combined, re.IGNORECASE)
    received_match = re.search(
        r"(\d+)\s+packets received by filter",
        combined,
        re.IGNORECASE,
    )
    dropped_match = re.search(
        r"(\d+)\s+packets dropped by kernel",
        combined,
        re.IGNORECASE,
    )
    if captured_match:
        captured_packets = int(captured_match.group(1))
        metadata["statistics"]["captured_packets"] = captured_packets
        if not metadata["statistics"]["total_packets"]:
            metadata["statistics"]["total_packets"] = captured_packets
    if received_match:
        metadata["statistics"]["received_by_filter"] = int(received_match.group(1))
    if dropped_match:
        metadata["statistics"]["dropped_by_kernel"] = int(dropped_match.group(1))

    protocols: Dict[str, int] = {}
    for packet in packets:
        protocol = packet.get("protocol")
        if not protocol:
            continue
        protocols[protocol] = protocols.get(protocol, 0) + 1

    metadata["statistics"]["protocols"] = protocols
    return metadata


class TcpdumpTool(BaseTool):
    """Run tcpdump network analysis and parse the results."""

    args_model = TcpdumpArgs
    planner_args_model = TcpdumpPlannerArgs
    informational_exit_codes = frozenset({0, TCPDUMP_TIMEOUT_EXIT_CODE})

    @classmethod
    def compile_planner_parameters(
        cls,
        planner_args: TcpdumpPlannerArgs | Dict[str, Any],
        *,
        action_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile semantic planner args into execution args.

        Runtime capture limits intentionally stay out of parameters so they
        cannot be reflected back into LLM-visible state as user-tunable knobs.
        """
        if isinstance(planner_args, TcpdumpPlannerArgs):
            compiled = planner_args.model_dump(
                exclude_defaults=True,
                exclude_none=True,
                mode="json",
            )
        else:
            compiled = TcpdumpPlannerArgs(**dict(planner_args or {})).model_dump(
                exclude_defaults=True,
                exclude_none=True,
                mode="json",
            )
        compiled["target"] = action_target or "unused"
        return compiled

    def build_command(self, args: TcpdumpArgs) -> List[str]:
        cmd: List[str] = [
            "timeout",
            f"{TCPDUMP_HARD_TIMEOUT_SECONDS}s",
            "tcpdump",
        ]

        interface = args.interface or DEFAULT_INTERFACE
        packet_count = TCPDUMP_PACKET_LIMIT
        if args.packet_count:
            packet_count = min(args.packet_count, TCPDUMP_PACKET_LIMIT)

        cmd.extend(
            [
                "-i",
                interface,
                "-nn",
                "-tttt",
                "-l",
                "-s",
                str(TCPDUMP_DEFAULT_SNAPLEN),
                "-c",
                str(packet_count),
            ]
        )
        _ensure_pcap_parent(args.write_file)
        cmd.extend(["-w", args.write_file])

        if args.verbose_level:
            cmd.append("-" + ("v" * args.verbose_level))
        if args.quiet:
            cmd.append("-q")

        filter_parts: List[str] = []
        if args.protocol:
            filter_parts.append(args.protocol.value)
        if args.host:
            filter_parts.append(f"host {args.host}")
        if args.port:
            filter_parts.append(f"port {args.port}")
        if args.bpf_filter:
            filter_parts.append(args.bpf_filter)

        if filter_parts:
            cmd.append(" and ".join(filter_parts))

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TcpdumpArgs,
    ) -> Dict[str, Any]:
        metadata = parse_tcpdump_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["capture_file"] = args.write_file
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: TcpdumpArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        _ = stdout, timestamp
        if not args.write_file:
            return []
        return [args.write_file] if os.path.isfile(args.write_file) else []

    def run(self, args: TcpdumpArgs) -> ToolResult:
        start = time.time()
        timeout = max(args.timeout or DEFAULT_TIMEOUT, TCPDUMP_HARD_TIMEOUT_SECONDS + 5)

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
                stderr="tcpdump command not found. Ensure tcpdump is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))

        return ToolResult(
            success=self.is_success_exit_code(
                proc.returncode,
                args,
                stdout=proc.stdout,
                stderr=proc.stderr,
            ),
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
        tool_id="sniffing_spoofing.network_sniffers.tcpdump",
        display_name="Tcpdump",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="packet_capture",
                description=(
                    "Capture network packets into a finite workspace-local PCAP "
                    "for host, port, protocol, or plaintext payload proof; run "
                    "in parallel with a trigger before analyzing the saved PCAP "
                    "artifact with tshark."
                ),
                output_indicators=["tcpdump", "pcap", "packet", "length"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)
