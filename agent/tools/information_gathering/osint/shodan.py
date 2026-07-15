"""Shodan OSINT information gathering tool using Pydantic models."""

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


class SearchType(str, Enum):
    """Shodan search types."""
    
    HOST = "host"
    SEARCH = "search"
    COUNT = "count"
    FACETS = "facets"
    TOKENS = "tokens"
    DOMAIN = "domain"
    DNS_RESOLVE = "dns-resolve"
    DNS_REVERSE = "dns-reverse"
    PORTS = "ports"
    PROTOCOLS = "protocols"
    SCAN = "scan"


class OutputFormat(str, Enum):
    """Shodan output format options."""
    
    JSON = "json"
    XML = "xml"
    CSV = "csv"
    TEXT = "text"


class ShodanArgs(BaseToolArgs):
    """Arguments for the Shodan tool."""

    search_type: SearchType = Field(
        SearchType.SEARCH,
        description="Type of Shodan search to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    api_key: Optional[str] = Field(
        None,
        description="Shodan API key for authentication",
    )
    limit: int = Field(
        100,
        ge=1,
        le=1000,
        description="Maximum number of results to return",
    )
    facets: Optional[str] = Field(
        None,
        description="Facets to include in results (comma-separated)",
    )
    filters: Optional[str] = Field(
        None,
        description="Additional filters to apply to search",
    )
    minify: bool = Field(
        False,
        description="Return minified JSON output",
    )
    history: bool = Field(
        False,
        description="Include historical data in results",
    )
    timeout: int = Field(
        30,
        ge=5,
        le=300,
        description="Timeout in seconds for API requests",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )


def parse_shodan_json(json_text: str) -> Dict[str, Any]:
    """Parse Shodan JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"results": [], "summary": {}}
    
    try:
        data = json.loads(json_text)
        
        # Handle different response types
        if isinstance(data, list):
            metadata["results"] = data
        elif isinstance(data, dict):
            if "data" in data:
                metadata["results"] = data["data"]
            else:
                metadata["results"] = [data]
            
            # Extract summary information
            if "total" in data:
                metadata["summary"]["total"] = data["total"]
            if "facets" in data:
                metadata["summary"]["facets"] = data["facets"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


def parse_shodan_xml(xml_text: str) -> Dict[str, Any]:
    """Parse Shodan XML output into structured metadata."""
    
    metadata: Dict[str, Any] = {"results": [], "summary": {}}
    
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        
        # Parse results
        for result in root.findall(".//result"):
            result_data = {}
            for child in result:
                result_data[child.tag] = child.text or ""
            metadata["results"].append(result_data)
        
        # Parse summary
        summary = root.find(".//summary")
        if summary is not None:
            for child in summary:
                metadata["summary"][child.tag] = child.text or ""
        
    except Exception as e:
        metadata["error"] = f"Failed to parse XML: {str(e)}"
    
    return metadata


class ShodanTool(BaseTool):
    """Run Shodan searches and parse the results."""

    args_model = ShodanArgs

    def build_command(self, args: ShodanArgs) -> List[str]:
        """Build shodan command arguments.
        
        Args:
            args: Validated ShodanArgs
            
        Returns:
            List of command arguments for shodan CLI
        """
        cmd = ["shodan"]
        
        # Add search type subcommand
        cmd.append(args.search_type.value)
        
        # Add limit for search commands
        if args.search_type == SearchType.SEARCH:
            cmd.extend(["--limit", str(args.limit)])
        
        # Add facets if specified for search
        if args.facets and args.search_type == SearchType.SEARCH:
            cmd.extend(["--facets", args.facets])
        
        # Add history flag for host lookups
        if args.history and args.search_type == SearchType.HOST:
            cmd.append("--history")
        
        # Add target/query (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ShodanArgs,
    ) -> Dict[str, Any]:
        """Parse shodan output into structured metadata."""
        if stdout:
            # Try JSON first
            try:
                return parse_shodan_json(stdout)
            except Exception:
                return {
                    "raw_output": stdout,
                    "search_type": args.search_type.value,
                    "target": args.target
                }
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: ShodanArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create shodan artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/shodan_{args.search_type.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: ShodanArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.shodan",
        display_name="Shodan CLI",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="internet_asset_lookup",
                description="Lookup internet-exposed hosts and banners via the Shodan API; returns matching IPs, ports, banners; use for passive external attack-surface intel",
                output_indicators=["IP", "Port", "Service"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=5,
        estimated_runtime_minutes=3,
    )
)