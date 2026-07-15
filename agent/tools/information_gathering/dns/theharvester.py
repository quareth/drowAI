"""TheHarvester DNS subdomain enumeration tool for information gathering."""

from __future__ import annotations

import os
import subprocess
import time
import re
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

# Define enums for TheHarvester DNS options
class SearchEngine(str, Enum):
    """TheHarvester search engines for DNS enumeration."""
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

class TheHarvesterDnsArgs(BaseToolArgs):
    """Arguments for the TheHarvester DNS tool."""
    
    # Primary options
    target_domain: str = Field(
        ...,
        description="Target domain to enumerate subdomains for"
    )
    
    # Search configuration
    search_engines: List[SearchEngine] = Field(
        default_factory=lambda: [SearchEngine.GOOGLE, SearchEngine.BING],
        description="Search engines to use for enumeration"
    )
    
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for results"
    )
    
    # Query options
    limit: int = Field(
        500,
        ge=1,
        le=10000,
        description="Limit number of results"
    )
    
    timeout: int = Field(
        300,
        ge=1,
        le=3600,
        description="Timeout in seconds for the scan"
    )
    
    # DNS options
    dns_server: Optional[str] = Field(
        None,
        description="DNS server to use for queries"
    )
    
    dns_lookup: bool = Field(
        True,
        description="Perform DNS lookups"
    )
    
    dns_brute: bool = Field(
        False,
        description="Perform DNS bruteforce"
    )
    
    dns_tld: bool = Field(
        False,
        description="Perform DNS TLD expansion"
    )
    
    # Output options
    output_file: Optional[str] = Field(
        None,
        description="Path to save output results"
    )
    
    # Shodan options
    shodan_query: Optional[str] = Field(
        None,
        description="Custom Shodan query"
    )
    
    # Screenshot options
    take_screenshot: bool = Field(
        False,
        description="Take screenshots of discovered websites"
    )
    
    # Filtering options
    filter_results: Optional[str] = Field(
        None,
        description="Filter results by specific criteria"
    )
    
    verbose: bool = Field(
        False,
        description="Enable verbose output"
    )
    
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors"
    )
    
    # Common options
    common_timeout: int = Field(
        300,
        ge=30,
        le=3600,
        description="Timeout in seconds for the entire operation"
    )

def parse_theharvester_dns_output(output_text: str) -> Dict[str, Any]:
    """Parse TheHarvester DNS output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "subdomains": [],
        "emails": [],
        "hosts": [],
        "ips": [],
        "urls": [],
        "summary": {}
    }
    
    try:
        # Extract subdomains
        subdomain_pattern = r"([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}"
        subdomains = re.findall(subdomain_pattern, output_text)
        metadata["subdomains"] = list(set([s[0] for s in subdomains if s[0]]))
        
        # Extract emails
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, output_text)
        metadata["emails"] = list(set(emails))
        
        # Extract IP addresses
        ip_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}\b'
        ips = re.findall(ip_pattern, output_text)
        metadata["ips"] = list(set(ips))
        
        # Extract URLs
        url_pattern = r'https?://[^\s<>"]+|www\.[^\s<>"]+'
        urls = re.findall(url_pattern, output_text)
        metadata["urls"] = list(set(urls))
        
        # Extract hosts (domains without protocol)
        host_pattern = r'(?:www\.)?([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
        hosts = re.findall(host_pattern, output_text)
        metadata["hosts"] = list(set([h[0] for h in hosts if h[0]]))
        
        # Extract summary information
        metadata["summary"]["total_subdomains"] = len(metadata["subdomains"])
        metadata["summary"]["total_emails"] = len(metadata["emails"])
        metadata["summary"]["total_ips"] = len(metadata["ips"])
        metadata["summary"]["total_urls"] = len(metadata["urls"])
        metadata["summary"]["total_hosts"] = len(metadata["hosts"])
        
        # Extract search engine information
        engine_pattern = r"Searching\s+([^:]+):"
        engines = re.findall(engine_pattern, output_text)
        metadata["summary"]["search_engines_used"] = engines
        
        # Extract timing information
        time_pattern = r"(\d+\.?\d*)\s*(seconds?|minutes?)"
        time_matches = re.findall(time_pattern, output_text)
        if time_matches:
            metadata["summary"]["execution_time"] = time_matches[0]
            
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata

class TheHarvesterDnsTool(BaseTool):
    """TheHarvester DNS subdomain enumeration tool for information gathering."""
    
    args_model = TheHarvesterDnsArgs
    
    def build_command(self, args: TheHarvesterDnsArgs) -> List[str]:
        """Build theHarvester command arguments.
        
        Args:
            args: Validated TheHarvesterDnsArgs
            
        Returns:
            List of command arguments for theHarvester
        """
        cmd = ["theHarvester"]
        
        # Add target domain
        cmd.extend(["-d", args.target_domain])
        
        # Add search engines
        if args.search_engines:
            engines = ",".join([engine.value for engine in args.search_engines])
            cmd.extend(["-b", engines])
        
        # Add limit
        cmd.extend(["-l", str(args.limit)])
        
        # Add DNS options
        if args.dns_server:
            cmd.extend(["--dns-server", args.dns_server])
        
        if not args.dns_lookup:
            cmd.append("-n")  # Correct flag for no DNS lookup
        
        if args.dns_brute:
            cmd.append("-c")  # DNS brute force
        
        if args.dns_tld:
            cmd.append("-t")  # DNS TLD expansion
        
        # Add output file
        if args.output_file:
            cmd.extend(["-f", args.output_file])
        
        # Add Shodan option
        if SearchEngine.SHODAN in args.search_engines:
            cmd.append("-s")  # Use Shodan
        
        # Add screenshot option
        if args.take_screenshot:
            cmd.append("--screenshot")
        
        # Add verbose option
        if args.verbose:
            cmd.append("-v")
        
        return cmd
    
    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TheHarvesterDnsArgs,
    ) -> Dict[str, Any]:
        """Parse theHarvester output into structured metadata."""
        if stdout:
            return parse_theharvester_dns_output(stdout)
        return {}
    
    def create_artifacts(
        self,
        stdout: str,
        args: TheHarvesterDnsArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create theHarvester artifact files from output."""
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/theharvester_dns_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts
    
    def run(self, args: TheHarvesterDnsArgs) -> ToolResult:
        cmd = self.build_command(args)
        
        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.common_timeout,
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


from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.dns.theharvester",
        display_name="theHarvester (DNS)",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[
            PentestPhase.RECONNAISSANCE,
            PentestPhase.ENUMERATION,
        ],
        capabilities=[
            ToolCapability(
                name="subdomain_collection",
                description="Enumerate DNS hostnames for a domain from public OSINT sources; returns matched hostnames; use for passive DNS-side recon, not active bruteforce",
                output_indicators=["hostname", "subdomain"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["dns"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=5,
        estimated_runtime_minutes=5,
    )
)
