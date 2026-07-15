"""Fierce DNS reconnaissance tool.

Implements the execution-model hooks (`build_command`, `parse_output`, `create_artifacts`)
so the same logic works across direct, file-comm, and PTY execution.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class FierceArgs(BaseToolArgs):
    """Arguments for the Fierce tool."""

    dns_servers: Optional[List[str]] = Field(
        None,
        description="Optional DNS servers to query (comma-separated in CLI).",
    )
    wordlist_file: Optional[str] = Field(
        None,
        description="Optional wordlist file for brute-force subdomain enumeration.",
    )
    subdomains: Optional[List[str]] = Field(
        None,
        description="Optional explicit subdomains to test (instead of brute forcing).",
    )
    traverse: Optional[int] = Field(
        None,
        ge=0,
        le=50,
        description="Optional traverse depth for related domains (fierce --traverse).",
    )
    wide: bool = Field(
        False,
        description="Enable wide scanning mode if supported (fierce --wide).",
    )
    connect: bool = Field(
        False,
        description="Attempt to connect to discovered hosts if supported (fierce --connect).",
    )


def _parse_fierce_output(output_text: str, target_domain: str) -> Dict[str, Any]:
    """Best-effort parsing for Fierce output across versions.

    Fierce output varies by implementation; we extract:
    - hostnames under the target domain
    - IP addresses
    """
    metadata: Dict[str, Any] = {
        "target": target_domain,
        "hostnames": [],
        "ip_addresses": [],
    }
    if not output_text:
        return metadata

    hostname_pattern = re.compile(
        rf"\b([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.{re.escape(target_domain)})\b"
    )
    ip_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    metadata["hostnames"] = sorted(
        set(m.group(1).lower() for m in hostname_pattern.finditer(output_text))
    )
    metadata["ip_addresses"] = sorted(set(ip_pattern.findall(output_text)))
    return metadata


class FierceTool(BaseTool):
    """Run fierce DNS reconnaissance and parse the results."""

    args_model = FierceArgs

    def build_command(self, args: FierceArgs) -> List[str]:
        # Modern Fierce (mschwager/fierce) uses --domain; we align to that interface.
        cmd: List[str] = ["fierce", "--domain", args.target]

        if args.dns_servers:
            cmd.extend(["--dns-servers", ",".join(args.dns_servers)])

        if args.wordlist_file:
            cmd.extend(["--wordlist", args.wordlist_file])

        if args.subdomains:
            cmd.extend(["--subdomains", ",".join(args.subdomains)])

        if args.traverse is not None:
            cmd.extend(["--traverse", str(args.traverse)])

        if args.wide:
            cmd.append("--wide")

        if args.connect:
            cmd.append("--connect")

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FierceArgs,
    ) -> Dict[str, Any]:
        metadata = _parse_fierce_output(stdout or "", target_domain=args.target)
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: FierceArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/fierce_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write(stdout)
            return [artifact_path]
        except OSError:
            return []

    def run(self, args: FierceArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except FileNotFoundError:
            # Binary not installed in the current execution environment (common in minimal images).
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=(
                    "fierce command not found in PATH.\n"
                    "Install inside the execution environment (e.g., Kali container) and retry.\n"
                    "Common options:\n"
                    "- Debian/Kali: apt-get update && apt-get install -y fierce\n"
                    "- Python: pip install fierce\n"
                ),
                artifacts=[],
                metadata={"error_type": "missing_binary", "binary": "fierce"},
                execution_time=time.time() - start,
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
        tool_id="information_gathering.dns.fierce",
        display_name="Fierce",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="dns_bruteforce",
                description="Enumerate subdomains for a domain via DNS bruteforce and zone-transfer probes; returns matched hostnames; use for legacy DNS recon flows",
                output_indicators=["Found"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=10,
    )
)