"""TheHarvester OSINT information gathering tool using Pydantic models."""

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


class SearchEngine(str, Enum):
    """TheHarvester search engines."""
    
    GOOGLE = "google"
    BING = "bing"
    YAHOO = "yahoo"
    BAIDU = "baidu"
    SHODAN = "shodan"
    VIRUSTOTAL = "virustotal"
    THREATCROWD = "threatcrowd"
    CENSYS = "censys"
    CRTSH = "crt.sh"
    HUNTER = "hunter"
    NETCRAFT = "netcraft"
    SECURITYTRAILS = "securitytrails"
    PASSIVE_DNS = "passive_dns"


class OutputFormat(str, Enum):
    """TheHarvester output format options."""
    
    JSON = "json"
    XML = "xml"
    CSV = "csv"
    TEXT = "text"


class TheHarvesterArgs(BaseToolArgs):
    """Arguments for the TheHarvester tool."""

    search_engines: List[SearchEngine] = Field(
        default_factory=lambda: [SearchEngine.GOOGLE, SearchEngine.BING],
        description="Search engines to use for information gathering",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing",
    )
    limit: int = Field(
        500,
        ge=1,
        le=10000,
        description="Limit number of results",
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
    dns_server: Optional[str] = Field(
        None,
        description="DNS server to use for queries",
    )
    dns_lookup: bool = Field(
        True,
        description="Perform DNS lookups",
    )
    dns_brute: bool = Field(
        False,
        description="Perform DNS bruteforce",
    )
    dns_tld: bool = Field(
        False,
        description="Perform DNS TLD expansion",
    )
    shodan_query: Optional[str] = Field(
        None,
        description="Custom Shodan query",
    )
    take_screenshot: bool = Field(
        False,
        description="Take screenshots of discovered websites",
    )


def parse_theharvester_json(json_text: str) -> Dict[str, Any]:
    """Parse theHarvester JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"hosts": [], "emails": [], "ips": [], "subdomains": []}
    
    try:
        data = json.loads(json_text)
        if isinstance(data, dict):
            # Extract hosts
            if "hosts" in data:
                for host in data["hosts"]:
                    metadata["hosts"].append({
                        "hostname": host,
                        "source": "theharvester"
                    })
                    metadata["subdomains"].append({
                        "subdomain": host,
                        "source": "theharvester"
                    })
            
            # Extract emails
            if "emails" in data:
                for email in data["emails"]:
                    metadata["emails"].append({
                        "email": email,
                        "source": "theharvester"
                    })
            
            # Extract IPs
            if "ips" in data:
                for ip in data["ips"]:
                    metadata["ips"].append({
                        "ip": ip,
                        "source": "theharvester"
                    })
    except (json.JSONDecodeError, KeyError) as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class TheHarvesterTool(BaseTool):
    """Run theHarvester OSINT information gathering and parse the results.
    
    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = TheHarvesterArgs

    def build_command(self, args: TheHarvesterArgs) -> List[str]:
        """Build theHarvester command arguments.
        
        Args:
            args: Validated TheHarvesterArgs
            
        Returns:
            List of command arguments for theHarvester
        """
        cmd = ["theHarvester"]
        
        # Add search engines
        if args.search_engines:
            engines = ",".join([engine.value for engine in args.search_engines])
            cmd.extend(["-b", engines])
        
        # Add limit
        cmd.extend(["-l", str(args.limit)])
        
        # Add timeout
        cmd.extend(["-t", str(args.timeout)])
        
        # Add verbose option
        if args.verbose:
            cmd.append("-v")
        
        # Add quiet option
        if args.quiet:
            cmd.append("-q")
        
        # Add DNS server if specified
        if args.dns_server:
            cmd.extend(["-n", args.dns_server])
        
        # Add DNS lookup option
        if not args.dns_lookup:
            cmd.append("--no-dns")
        
        # Add DNS bruteforce option
        if args.dns_brute:
            cmd.append("--dns-bruteforce")
        
        # Add DNS TLD option
        if args.dns_tld:
            cmd.append("--dns-tld")
        
        # Add Shodan query if specified
        if args.shodan_query:
            cmd.extend(["-s", args.shodan_query])
        
        # Add screenshot option
        if args.take_screenshot:
            cmd.append("--screenshot")
        
        # Add output format
        if args.output_format == OutputFormat.JSON:
            cmd.extend(["-f", "json"])
        elif args.output_format == OutputFormat.XML:
            cmd.extend(["-f", "xml"])
        elif args.output_format == OutputFormat.CSV:
            cmd.extend(["-f", "csv"])
        else:
            cmd.extend(["-f", "text"])
        
        # Add target (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TheHarvesterArgs,
    ) -> Dict[str, Any]:
        """Parse theHarvester output into structured metadata.
        
        Args:
            stdout: Command stdout (JSON if output_format=JSON)
            stderr: Command stderr
            exit_code: Command exit code
            args: Original TheHarvesterArgs
            
        Returns:
            Metadata dict with hosts, emails, ips, and subdomains
        """
        if args.output_format == OutputFormat.JSON and stdout:
            return parse_theharvester_json(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: TheHarvesterArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create theHarvester artifact files from output.
        
        Args:
            stdout: Command stdout
            args: Original TheHarvesterArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []
        
        if args.output_format == OutputFormat.JSON and stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/theharvester_{ts}.json"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional
        
        return artifacts

    def run(self, args: TheHarvesterArgs) -> ToolResult:
        """Execute theHarvester OSINT information gathering.
        
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
        tool_id="information_gathering.osint.theharvester",
        display_name="theHarvester",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="email_subdomain_collection",
                description="Enumerate emails, hostnames, and employees for a domain from public OSINT; returns contact and host evidence; use for passive footprinting",
                output_indicators=["Email", "Domain"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=8,
    )
)