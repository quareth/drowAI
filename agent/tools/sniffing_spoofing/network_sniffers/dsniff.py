"""DSniff - Collection of tools for network auditing and penetration testing."""

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
DEFAULT_INTERFACE = "any"
DEFAULT_TIMEOUT = 60


class DSniffBinary(str, Enum):
    """DSniff tools available."""

    DSNIFF = "dsniff"
    URLSNARF = "urlsnarf"
    FILESNARF = "filesnarf"
    MAILSNARF = "mailsnarf"
    MSGSNARF = "msgsnarf"


class DSniffProtocol(str, Enum):
    """Protocols that DSniff can analyze."""

    HTTP = "http"
    HTTPS = "ssl"
    FTP = "ftp"
    TELNET = "telnet"
    SMTP = "smtp"
    POP3 = "pop"
    IMAP = "imap"
    SSH = "ssh"
    DNS = "dns"
    ARP = "arp"


class DSniffArgs(BaseToolArgs):
    """Arguments for the DSniff tool."""

    tool: DSniffBinary = Field(
        DSniffBinary.DSNIFF,
        description="DSniff tool binary to invoke.",
    )
    interface: Optional[str] = Field(
        None,
        description="Network interface to use.",
    )
    protocol: Optional[DSniffProtocol] = Field(
        None,
        description="Protocol to focus on.",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file path.",
    )
    bpf_filter: Optional[str] = Field(
        None,
        description="BPF filter string.",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_dsniff_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse DSniff output into structured metadata."""
    metadata: Dict[str, Any] = {
        "credentials_found": [],
        "urls_captured": [],
        "hosts_detected": [],
        "protocols_analyzed": [],
        "packets_processed": 0,
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()

    try:
        for line in lines:
            for pattern in [
                r"(\w+://[^\s]+):(\w+)@([^\s]+)",
                r"username:\s*(\S+)",
                r"password:\s*(\S+)",
                r"login:\s*(\S+)",
                r"pass:\s*(\S+)",
            ]:
                matches = re.findall(pattern, line, re.IGNORECASE)
                if matches:
                    metadata["credentials_found"].extend(
                        matches if isinstance(matches[0], tuple) else matches
                    )

            metadata["urls_captured"].extend(
                re.findall(r"https?://[^\s]+", line, re.IGNORECASE)
            )

            metadata["hosts_detected"].extend(
                re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line)
            )

            for protocol_pattern in [
                r"(\w+)\s+packet",
                r"(\w+)\s+traffic",
                r"(\w+)\s+connection",
            ]:
                metadata["protocols_analyzed"].extend(
                    re.findall(protocol_pattern, line, re.IGNORECASE)
                )

            packet_match = re.search(r"(\d+)\s+packets?", line, re.IGNORECASE)
            if packet_match:
                metadata["packets_processed"] = int(packet_match.group(1))

            lowered = line.lower()
            if "error" in lowered:
                metadata["errors"].append(line.strip())
            elif "warning" in lowered:
                metadata["warnings"].append(line.strip())

        metadata["credentials_found"] = list(set(metadata["credentials_found"]))
        metadata["urls_captured"] = list(set(metadata["urls_captured"]))
        metadata["hosts_detected"] = list(set(metadata["hosts_detected"]))
        metadata["protocols_analyzed"] = list(set(metadata["protocols_analyzed"]))
    except Exception as exc:
        metadata["errors"].append(f"Failed to parse output: {exc}")

    return metadata


class DSniffTool(BaseTool):
    """Run DSniff network auditing tools and parse the output."""

    args_model = DSniffArgs

    def build_command(self, args: DSniffArgs) -> List[str]:
        cmd: List[str] = [args.tool.value]

        interface = args.interface or DEFAULT_INTERFACE
        cmd.extend(["-i", interface])

        if args.protocol:
            cmd.extend(["-p", args.protocol.value])
        if args.bpf_filter:
            cmd.extend(["-f", args.bpf_filter])
        if args.output_file:
            cmd.extend(["-w", args.output_file])
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
        args: DSniffArgs,
    ) -> Dict[str, Any]:
        metadata = parse_dsniff_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["tool"] = args.tool.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DSniffArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/dsniff_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: DSniffArgs) -> ToolResult:
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
                stderr=f"{args.tool.value} command not found. Ensure dsniff is installed.",
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
        tool_id="sniffing_spoofing.network_sniffers.dsniff",
        display_name="DSniff",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="credential_sniffing",
                description="Sniff credentials and URLs from plain-text protocols (HTTP, FTP, Telnet, POP3, IMAP); returns captured usernames, passwords, and URLs; passive — encrypted traffic is invisible.",
                output_indicators=["password", "login", "http"],
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
