"""OWASP Amass v5 command adapter and result parser."""

from __future__ import annotations

import os
import subprocess
import time
import json
import re
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class Mode(str, Enum):
    """Amass scan modes."""
    
    PASSIVE = "passive"
    ACTIVE = "active"
    BRUTE = "brute"
    DNS = "dns"
    REVERSE_DNS = "reverse"


class OutputFormat(str, Enum):
    """Requested presentation format retained for schema compatibility."""
    
    JSON = "json"
    CSV = "csv"
    TEXT = "text"
    XML = "xml"


class AmassArgs(BaseToolArgs):
    """Arguments for the Amass tool."""

    mode: Mode = Field(
        Mode.PASSIVE,
        description="Scan mode to use",
    )
    output_format: OutputFormat = Field(
        OutputFormat.TEXT,
        description="Preferred output format; Amass v5 enumeration emits terminal text",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Custom wordlist for bruteforce",
    )
    timeout: int = Field(
        300,
        ge=1,
        le=3600,
        description="Timeout in seconds for the scan",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors",
    )
    threads: int = Field(
        10,
        ge=1,
        le=100,
        description="Deprecated Amass v4 concurrency setting; ignored by Amass v5",
        json_schema_extra={"deprecated": True},
    )
    rate: int = Field(
        1000,
        ge=1,
        le=100000,
        description="Deprecated Amass v4 rate setting; ignored by Amass v5",
        json_schema_extra={"deprecated": True},
    )
    max_dns_queries: int = Field(
        1000,
        ge=1,
        le=100000,
        description="Deprecated Amass v4 query setting; ignored by Amass v5",
        json_schema_extra={"deprecated": True},
    )
    dns_server: Optional[str] = Field(
        None,
        description="DNS server to use for queries",
    )
    source: Optional[List[str]] = Field(
        None,
        description="Data sources to use",
    )
    exclude_source: Optional[List[str]] = Field(
        None,
        description="Data sources to exclude",
    )


def parse_amass_json(json_text: str) -> Dict[str, Any]:
    """Parse amass JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"subdomains": [], "hosts": [], "ips": []}
    
    try:
        # Amass outputs one JSON object per line
        lines = json_text.strip().split('\n')
        for line in lines:
            if line.strip():
                data = json.loads(line)
                if "name" in data:
                    subdomain_info = {
                        "subdomain": data["name"],
                        "ip": data.get("address", []),
                        "source": data.get("source", "amass"),
                        "type": data.get("type", "A")
                    }
                    metadata["subdomains"].append(subdomain_info)
                    metadata["hosts"].append({
                        "hostname": data["name"],
                        "ip": data.get("address", [])
                    })
                    # Add unique IPs
                    for ip in data.get("address", []):
                        if ip not in metadata["ips"]:
                            metadata["ips"].append(ip)
    except (json.JSONDecodeError, KeyError) as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


def parse_amass_text(output_text: str) -> Dict[str, Any]:
    """Extract discovered domain names and IPs from Amass terminal output."""
    subdomains: List[Dict[str, Any]] = []
    hosts: List[Dict[str, Any]] = []
    ips: List[str] = []
    seen_names: set[str] = set()

    domain_pattern = re.compile(
        r"(?:subdomain|name)\s*:\s*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)",
        re.IGNORECASE,
    )
    resolved_pattern = re.compile(
        r"^([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+)\s*(?:→|->)\s*"
        r"((?:\d{1,3}\.){3}\d{1,3})$"
    )

    for raw_line in (output_text or "").splitlines():
        line = raw_line.strip()
        domain_match = domain_pattern.search(line)
        resolved_match = resolved_pattern.search(line)
        name = domain_match.group(1) if domain_match else None
        address = None
        if resolved_match:
            name = resolved_match.group(1)
            address = resolved_match.group(2)
        if not name or name in seen_names:
            continue

        seen_names.add(name)
        addresses = [address] if address else []
        subdomains.append(
            {
                "subdomain": name,
                "ip": addresses,
                "source": "amass",
                "type": "A",
            }
        )
        hosts.append({"hostname": name, "ip": addresses})
        if address and address not in ips:
            ips.append(address)

    return {"subdomains": subdomains, "hosts": hosts, "ips": ips}


class AmassTool(BaseTool):
    """Run amass subdomain enumeration and parse the results.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = AmassArgs

    def build_command(self, args: AmassArgs) -> List[str]:
        """Build an Amass v5 enumeration command.
        
        Args:
            args: Validated AmassArgs
            
        Returns:
            List of command arguments for amass
        """
        cmd = ["amass", "enum"]

        if args.mode == Mode.ACTIVE:
            cmd.append("-active")
        elif args.mode == Mode.BRUTE:
            cmd.append("-brute")
        elif args.mode == Mode.REVERSE_DNS:
            pass
        elif args.mode == Mode.PASSIVE:
            cmd.append("-passive")

        if args.wordlist:
            if "-brute" not in cmd:
                cmd.append("-brute")
            cmd.extend(["-w", args.wordlist])

        timeout_minutes = max(1, (args.timeout + 59) // 60)
        cmd.extend(["-timeout", str(timeout_minutes)])

        if args.verbose:
            cmd.append("-v")

        if args.quiet:
            cmd.append("-silent")

        if args.dns_server:
            cmd.extend(["-r", args.dns_server])

        if args.source:
            cmd.extend(["-include", ",".join(args.source)])

        if args.exclude_source:
            cmd.extend(["-exclude", ",".join(args.exclude_source)])

        cmd.append("-nocolor")
        if args.mode == Mode.REVERSE_DNS:
            cmd.extend(["-addr", args.target])
        else:
            cmd.extend(["-d", args.target])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: AmassArgs,
    ) -> Dict[str, Any]:
        """Parse amass output into structured metadata.
        
        Args:
            stdout: Command stdout (JSON if output_format=JSON)
            stderr: Command stderr
            exit_code: Command exit code
            args: Original AmassArgs
            
        Returns:
            Metadata dict with subdomains, hosts, and ips
        """
        if args.output_format == OutputFormat.JSON and stdout:
            parsed_json = parse_amass_json(stdout)
            if "error" not in parsed_json:
                return parsed_json
        return parse_amass_text(stdout)

    def create_artifacts(
        self,
        stdout: str,
        args: AmassArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create amass artifact files from output.
        
        Args:
            stdout: Command stdout
            args: Original AmassArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []
        
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/amass_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional
        
        return artifacts

    def run(self, args: AmassArgs) -> ToolResult:
        """Execute amass subdomain enumeration.
        
        Uses build_command(), parse_output(), and create_artifacts() for
        consistent behavior with PTY execution path.
        """
        cmd = self.build_command(args)

        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout,
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

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start))

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
        tool_id="information_gathering.dns.amass",
        display_name="Amass",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="subdomain_enumeration",
                description="Enumerate subdomains for a domain via passive intel and active DNS bruteforce; returns discovered hostnames; use for thorough subdomain coverage",
                output_indicators=["Found", "Subdomain"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=15,
    )
)
