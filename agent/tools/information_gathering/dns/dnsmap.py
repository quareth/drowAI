"""DNSMap - DNS subdomain enumeration and mapping tool using brute force techniques."""

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

# Define enums for DNSMap options
class ScanMode(str, Enum):
    """DNSMap scan modes."""
    BRUTE_FORCE = "brute_force"
    WORDLIST = "wordlist"
    RECURSIVE = "recursive"
    REVERSE = "reverse"

class OutputFormat(str, Enum):
    """DNSMap output format options."""
    TEXT = "text"
    XML = "xml"
    JSON = "json"
    CSV = "csv"

class DnsMapArgs(BaseToolArgs):
    """Arguments for the DNSMap tool."""
    
    # Primary options
    target_domain: str = Field(
        ...,
        description="Target domain to enumerate subdomains for"
    )
    
    # Scan configuration
    scan_mode: ScanMode = Field(
        ScanMode.BRUTE_FORCE,
        description="Scan mode to use for enumeration"
    )
    
    wordlist_file: Optional[str] = Field(
        None,
        description="Path to custom wordlist file for subdomain enumeration"
    )
    
    # DNS options
    dns_server: Optional[str] = Field(
        None,
        description="Custom DNS server to use for queries"
    )
    
    timeout: int = Field(
        30,
        ge=1,
        le=300,
        description="Timeout in seconds for DNS queries"
    )
    
    # Output options
    output_format: OutputFormat = Field(
        OutputFormat.TEXT,
        description="Output format for results"
    )
    
    output_file: Optional[str] = Field(
        None,
        description="Path to save output results"
    )
    
    # Enumeration options
    max_threads: int = Field(
        10,
        ge=1,
        le=100,
        description="Maximum number of concurrent threads"
    )
    
    recursive_depth: Optional[int] = Field(
        None,
        ge=1,
        le=5,
        description="Recursive enumeration depth"
    )
    
    # Filtering options
    filter_wildcards: bool = Field(
        True,
        description="Filter out wildcard DNS responses"
    )
    
    verbose: bool = Field(
        False,
        description="Enable verbose output"
    )
    
    # Common options
    common_timeout: int = Field(
        300,
        ge=30,
        le=3600,
        description="Timeout in seconds for the entire scan"
    )

def parse_dnsmap_output(output_text: str) -> Dict[str, Any]:
    """Parse DNSMap output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "subdomains_found": [],
        "total_subdomains": 0,
        "wildcard_domains": [],
        "dns_servers_used": [],
        "scan_summary": {}
    }
    
    try:
        # Extract subdomains from output
        subdomain_pattern = r"(\S+\.\S+)"
        subdomains = re.findall(subdomain_pattern, output_text)
        
        # Filter out common false positives
        valid_subdomains = []
        for subdomain in subdomains:
            if subdomain and not subdomain.startswith(('.', '-', '_')):
                valid_subdomains.append(subdomain.strip())
        
        metadata["subdomains_found"] = list(set(valid_subdomains))
        metadata["total_subdomains"] = len(metadata["subdomains_found"])
        
        # Extract wildcard information
        wildcard_pattern = r"wildcard|Wildcard|WILDCARD"
        if re.search(wildcard_pattern, output_text):
            metadata["wildcard_domains"] = ["Wildcard DNS detected"]
        
        # Extract DNS server information
        dns_pattern = r"DNS server[:\s]+([^\s]+)"
        dns_servers = re.findall(dns_pattern, output_text)
        metadata["dns_servers_used"] = dns_servers
        
        # Extract scan summary
        if "found" in output_text.lower():
            found_match = re.search(r"(\d+)\s+found", output_text.lower())
            if found_match:
                metadata["scan_summary"]["subdomains_found"] = int(found_match.group(1))
        
        # Extract timing information
        time_pattern = r"(\d+\.?\d*)\s*(seconds?|minutes?)"
        time_matches = re.findall(time_pattern, output_text)
        if time_matches:
            metadata["scan_summary"]["execution_time"] = time_matches[0]
            
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata

class DnsMapTool(BaseTool):
    """DNSMap - DNS subdomain enumeration and mapping tool using brute force techniques."""
    
    args_model = DnsMapArgs
    
    def build_command(self, args: DnsMapArgs) -> List[str]:
        """Build dnsmap command arguments.
        
        Args:
            args: Validated DnsMapArgs
            
        Returns:
            List of command arguments for dnsmap
        """
        cmd = ["dnsmap"]
        
        # Add scan mode
        if args.scan_mode == ScanMode.WORDLIST and args.wordlist_file:
            cmd.extend(["-w", args.wordlist_file])
        elif args.scan_mode == ScanMode.RECURSIVE:
            cmd.append("-r")
            if args.recursive_depth:
                cmd.extend(["-d", str(args.recursive_depth)])
        
        # Add DNS server if specified
        if args.dns_server:
            cmd.extend(["-s", args.dns_server])
        
        # Add timeout (dnsmap uses delay between requests in ms)
        if args.timeout != 30:
            cmd.extend(["-d", str(args.timeout * 10)])  # Convert to ms delay
        
        # Add output options
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        
        # Add verbose output
        if args.verbose:
            cmd.append("-v")
        
        # Add target domain (usually last)
        cmd.append(args.target_domain)
        
        return cmd
    
    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DnsMapArgs,
    ) -> Dict[str, Any]:
        """Parse dnsmap output into structured metadata."""
        if stdout:
            return parse_dnsmap_output(stdout)
        return {}
    
    def create_artifacts(
        self,
        stdout: str,
        args: DnsMapArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create dnsmap artifact files from output."""
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/dnsmap_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts
    
    def run(self, args: DnsMapArgs) -> ToolResult:
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
        tool_id="information_gathering.dns.dnsmap",
        display_name="DNSMap",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="subdomain_bruteforce",
                description="Discover subdomains for a domain via wordlist DNS bruteforce; returns matched hostnames and IPs; use for offline-style subdomain bruteforce",
                output_indicators=["subdomain", "IP"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=15,
    )
)