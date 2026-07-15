"""Wrapper for the ``dnsrecon`` enumeration utility."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class DNSReconArgs(BaseToolArgs):
    """Arguments for dnsrecon."""

    dns_server: Optional[str] = Field(
        None,
        description="Optional DNS server to query (dnsrecon -n). Uses system resolver when omitted.",
    )
    wordlist: Optional[str] = Field(
        None, description="Path to wordlist for brute forcing subdomains"
    )
    output_file: Optional[str] = Field(
        None,
        description="Optional output file path. When provided, dnsrecon writes results there (-j JSON).",
    )
    enable_bruteforce: bool = Field(
        False,
        description="Enable brute force enumeration using the wordlist (-D). Requires wordlist when true.",
    )


def _parse_dnsrecon_output(stdout: str) -> Dict[str, Any]:
    """Best-effort parse of dnsrecon stdout into a list of record lines.

    dnsrecon output is mostly human-readable; we keep parsing resilient and extract
    the high-signal '[+]' result lines.
    """
    records: List[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[+]"):
            records.append(line[3:].strip())
    return {"records": records}


class DNSReconTool(BaseTool):
    """Run dnsrecon to enumerate DNS records."""

    args_model = DNSReconArgs

    def build_command(self, args: DNSReconArgs) -> List[str]:
        cmd: List[str] = ["dnsrecon", "-d", args.target]

        if args.dns_server:
            cmd.extend(["-n", args.dns_server])

        if args.enable_bruteforce:
            if args.wordlist:
                cmd.extend(["-D", args.wordlist, "-t", "brt"])
            else:
                # Avoid building an invalid command; validation should prevent this.
                pass
        elif args.wordlist:
            # Preserve previous behavior: providing wordlist implies bruteforce.
            cmd.extend(["-D", args.wordlist, "-t", "brt"])

        # Optional JSON artifact output. dnsrecon supports -j for JSON on many versions.
        if args.output_file:
            cmd.extend(["-j", args.output_file])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DNSReconArgs,
    ) -> Dict[str, Any]:
        metadata = _parse_dnsrecon_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DNSReconArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        if stdout and len(stdout) > 200:
            ts = int(timestamp or time.time())
            os.makedirs("artifacts", exist_ok=True)
            path = f"artifacts/dnsrecon_{ts}.txt"
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(path)
            except OSError:
                pass

        return artifacts

    def run(self, args: DNSReconArgs) -> ToolResult:
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
        tool_id="information_gathering.dns.dnsrecon",
        display_name="DNSRecon",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="dns_record_enumeration",
                description="Enumerate DNS records and zone transfers for a domain; returns A, MX, NS, AXFR evidence; use for general DNS recon, not pure subdomain bruteforce",
                output_indicators=["A", "MX", "NS"],
            ),
            ToolCapability(
                name="zone_transfer_check",
                description="Check for DNS zone transfers",
                output_indicators=["AXFR"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=8,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)