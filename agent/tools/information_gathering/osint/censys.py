"""Censys OSINT information gathering tool using Pydantic models."""

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
    """Censys search types."""
    
    HOSTS = "hosts"
    WEBSITES = "websites"
    CERTIFICATES = "certificates"
    DOMAINS = "domains"
    IPV4 = "ipv4"
    IPV6 = "ipv6"


class OutputFormat(str, Enum):
    """Censys output format options."""
    
    JSON = "json"
    CSV = "csv"
    TEXT = "text"


class CensysArgs(BaseToolArgs):
    """Arguments for the Censys tool."""

    search_type: SearchType = Field(
        SearchType.HOSTS,
        description="Type of Censys search to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    api_id: Optional[str] = Field(
        None,
        description="Censys API ID for authentication",
    )
    api_secret: Optional[str] = Field(
        None,
        description="Censys API secret for authentication",
    )
    max_records: int = Field(
        100,
        ge=1,
        le=1000,
        description="Maximum number of records to return",
    )
    fields: Optional[str] = Field(
        None,
        description="Fields to include in results (comma-separated)",
    )
    sort: Optional[str] = Field(
        None,
        description="Sort results by specified field",
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
    raw: bool = Field(
        False,
        description="Return raw API response",
    )


def parse_censys_json(json_text: str) -> Dict[str, Any]:
    """Parse Censys JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"results": [], "summary": {}}
    
    try:
        data = json.loads(json_text)
        
        # Handle different response types
        if isinstance(data, list):
            metadata["results"] = data
        elif isinstance(data, dict):
            if "results" in data:
                metadata["results"] = data["results"]
            elif "data" in data:
                metadata["results"] = data["data"]
            else:
                metadata["results"] = [data]
            
            # Extract summary information
            if "total" in data:
                metadata["summary"]["total"] = data["total"]
            if "query" in data:
                metadata["summary"]["query"] = data["query"]
            if "backend_time" in data:
                metadata["summary"]["backend_time"] = data["backend_time"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class CensysTool(BaseTool):
    """Run Censys searches and parse the results."""

    args_model = CensysArgs

    def build_command(self, args: CensysArgs) -> List[str]:
        """Build censys command arguments.
        
        Args:
            args: Validated CensysArgs
            
        Returns:
            List of command arguments for censys CLI
        """
        cmd = ["censys"]
        
        # Add subcommand based on search type
        if args.search_type == SearchType.HOSTS:
            cmd.append("search")
        elif args.search_type == SearchType.CERTIFICATES:
            cmd.append("certs")
        else:
            cmd.append("search")
        
        # Add max results
        cmd.extend(["--max-records", str(args.max_records)])
        
        # Add fields if specified
        if args.fields:
            cmd.extend(["--fields", args.fields])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("--verbose")
        
        # Add target/query (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: CensysArgs,
    ) -> Dict[str, Any]:
        """Parse censys output into structured metadata."""
        if args.output_format == OutputFormat.JSON and stdout:
            return parse_censys_json(stdout)
        return {
            "raw_output": stdout,
            "format": args.output_format.value,
            "search_type": args.search_type.value,
            "target": args.target
        }

    def create_artifacts(
        self,
        stdout: str,
        args: CensysArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create censys artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/censys_{args.search_type.value}_{ts}.{args.output_format.value}"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: CensysArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.censys",
        display_name="Censys",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="internet_search",
                description="Lookup internet-exposed hosts, services, and certs via the Censys API; returns matching IPs, hostnames, certs; use for external attack-surface intel",
                output_indicators=["IP", "certificate", "service"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=5,
        estimated_runtime_minutes=3,
        description="Requires API key for full functionality",
    )
)