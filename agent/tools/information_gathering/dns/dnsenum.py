from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class DNSEnumArgs(BaseToolArgs):
    """Arguments for dnsenum."""

    dns_server: Optional[str] = Field(
        None,
        description="Optional DNS server to query (e.g., 8.8.8.8). Uses system resolver when omitted.",
    )
    wordlist_file: Optional[str] = Field(
        None,
        description="Optional wordlist file path for subdomain brute force (dnsenum -f).",
    )
    recursive: bool = Field(
        False,
        description="Enable recursive subdomain brute forcing (dnsenum -r).",
    )
    delay_seconds: Optional[float] = Field(
        None,
        ge=0,
        le=30,
        description="Optional delay between requests in seconds (dnsenum -d).",
    )
    disable_reverse: bool = Field(
        False,
        description="Disable reverse lookups / reverse brute force (dnsenum --noreverse).",
    )
    enum_all: bool = Field(
        True,
        description="Run the full enumeration suite when true (dnsenum --enum).",
    )
    output_file: Optional[str] = Field(
        None,
        description="Optional output file path for dnsenum to write results (-o).",
    )


def _parse_dnsenum_output(stdout: str, target: str) -> Dict[str, Any]:
    """Best-effort parsing of dnsenum output into structured metadata.

    dnsenum output formats can vary across versions; we keep parsing resilient and
    focus on extracting high-signal indicators (hostnames, IPs, nameservers, MX).
    """
    metadata: Dict[str, Any] = {
        "target": target,
        "hostnames": [],
        "ip_addresses": [],
        "nameservers": [],
        "mx_hosts": [],
    }

    if not stdout:
        return metadata

    # Hostname candidates (including subdomains of target)
    hostname_pattern = re.compile(rf"\b([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.{re.escape(target)})\b")
    hostnames = sorted(set(m.group(1).lower() for m in hostname_pattern.finditer(stdout)))
    metadata["hostnames"] = hostnames

    # IP addresses
    ip_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    ips = sorted(set(ip_pattern.findall(stdout)))
    metadata["ip_addresses"] = ips

    # Nameservers / MX lines often contain "NS:" / "MX:" indicators
    for line in stdout.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if re.search(r"\bNS\b", line_stripped, re.IGNORECASE) and target in line_stripped:
            # Pull hostnames on the line that look like they belong to the target or common ns names.
            ns_matches = re.findall(r"\b[a-zA-Z0-9_.-]+\.[a-zA-Z]{2,}\b", line_stripped)
            for ns in ns_matches:
                metadata["nameservers"].append(ns.lower())
        if re.search(r"\bMX\b", line_stripped, re.IGNORECASE) and target in line_stripped:
            mx_matches = re.findall(r"\b[a-zA-Z0-9_.-]+\.[a-zA-Z]{2,}\b", line_stripped)
            for mx in mx_matches:
                metadata["mx_hosts"].append(mx.lower())

    metadata["nameservers"] = sorted(set(metadata["nameservers"]))
    metadata["mx_hosts"] = sorted(set(metadata["mx_hosts"]))

    return metadata


class DNSEnumTool(BaseTool):
    """Run dnsenum to enumerate DNS information."""

    args_model = DNSEnumArgs

    def build_command(self, args: DNSEnumArgs) -> List[str]:
        cmd: List[str] = ["dnsenum"]

        # Full enumeration suite (commonly supported)
        if args.enum_all:
            cmd.append("--enum")

        if args.dns_server:
            cmd.extend(["--dnsserver", args.dns_server])

        if args.wordlist_file:
            cmd.extend(["-f", args.wordlist_file])

        if args.recursive:
            cmd.append("-r")

        if args.delay_seconds is not None:
            # dnsenum accepts seconds; keep float-friendly for user, but pass as string
            cmd.extend(["-d", str(args.delay_seconds)])

        if args.disable_reverse:
            cmd.append("--noreverse")

        if args.output_file:
            cmd.extend(["-o", args.output_file])

        # Target domain is required by BaseToolArgs
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DNSEnumArgs,
    ) -> Dict[str, Any]:
        metadata = _parse_dnsenum_output(stdout or "", target=args.target)
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DNSEnumArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        if not stdout:
            return artifacts

        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/dnsenum_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write(stdout)
            artifacts.append(artifact_path)
        except OSError:
            pass
        return artifacts

    def run(self, args: DNSEnumArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=args.timeout
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
        tool_id="information_gathering.dns.dnsenum",
        display_name="DNSEnum",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="dns_record_enumeration",
                description="Enumerate common DNS records and host entries for a domain; returns A, MX, NS, TXT records; use for lightweight DNS recon",
                output_indicators=["A", "MX", "NS", "TXT"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=6,
    )
)