"""Amass subdomain enumeration tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
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
    """Amass output format options."""
    
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
        OutputFormat.JSON,
        description="Output format for parsing",
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
        description="Number of threads to use",
    )
    rate: int = Field(
        1000,
        ge=1,
        le=100000,
        description="Rate of DNS queries per second",
    )
    max_dns_queries: int = Field(
        1000,
        ge=1,
        le=100000,
        description="Maximum number of DNS queries",
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


class AmassTool(BaseTool):
    """Run amass subdomain enumeration and parse the results.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = AmassArgs

    def build_command(self, args: AmassArgs) -> List[str]:
        """Build amass command arguments.
        
        Args:
            args: Validated AmassArgs
            
        Returns:
            List of command arguments for amass
        """
        cmd = ["amass"]
        
        # Add mode
        if args.mode == Mode.ACTIVE:
            cmd.append("enum")
        elif args.mode == Mode.BRUTE:
            cmd.append("enum")
            cmd.append("-brute")
        elif args.mode == Mode.DNS:
            cmd.append("dns")
        elif args.mode == Mode.REVERSE_DNS:
            cmd.append("dns")
            cmd.append("-reverse")
        else:  # PASSIVE
            cmd.append("enum")
            cmd.append("-passive")
        
        # Add wordlist if specified
        if args.wordlist:
            cmd.extend(["-w", args.wordlist])
        
        # Add timeout
        cmd.extend(["-timeout", str(args.timeout)])
        
        # Add verbose option
        if args.verbose:
            cmd.append("-v")
        
        # Add quiet option
        if args.quiet:
            cmd.append("-q")
        
        # Add threads
        cmd.extend(["-t", str(args.threads)])
        
        # Add rate
        cmd.extend(["-r", str(args.rate)])
        
        # Add max DNS queries
        cmd.extend(["-max-dns-queries", str(args.max_dns_queries)])
        
        # Add DNS server if specified
        if args.dns_server:
            cmd.extend(["-dns", args.dns_server])
        
        # Add sources if specified
        if args.source:
            cmd.extend(["-src", ",".join(args.source)])
        
        # Add exclude sources if specified
        if args.exclude_source:
            cmd.extend(["-exclude", ",".join(args.exclude_source)])
        
        # Add output format
        if args.output_format == OutputFormat.JSON:
            cmd.extend(["-json", "-"])
        elif args.output_format == OutputFormat.CSV:
            cmd.extend(["-csv", "-"])
        elif args.output_format == OutputFormat.XML:
            cmd.extend(["-xml", "-"])
        else:
            cmd.extend(["-o", "-"])
        
        # Add target (usually last)
        cmd.append(args.target)
        
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
            return parse_amass_json(stdout)
        return {}

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
        
        if args.output_format == OutputFormat.JSON and stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/amass_{ts}.json"
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