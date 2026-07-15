"""Sublist3r subdomain enumeration tool using Pydantic models."""

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
    """Sublist3r search engines."""
    
    GOOGLE = "google"
    BING = "bing"
    YAHOO = "yahoo"
    BAIDU = "baidu"
    ASK = "ask"
    NETCRAFT = "netcraft"
    VIRUSTOTAL = "virustotal"
    THREATCROWD = "threatcrowd"
    SSL = "ssl"
    PASSIVE_DNS = "passive_dns"


class OutputFormat(str, Enum):
    """Sublist3r output format options."""
    
    JSON = "json"
    CSV = "csv"
    TEXT = "text"


class Sublist3rArgs(BaseToolArgs):
    """Arguments for the Sublist3r tool."""

    search_engines: List[SearchEngine] = Field(
        default_factory=lambda: [SearchEngine.GOOGLE, SearchEngine.BING],
        description="Search engines to use for enumeration",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing",
    )
    threads: int = Field(
        40,
        ge=1,
        le=100,
        description="Number of threads to use",
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
    save_results: bool = Field(
        True,
        description="Save results to file",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file path",
    )
    bruteforce: bool = Field(
        False,
        description="Enable bruteforce subdomain enumeration",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Custom wordlist for bruteforce",
    )


def parse_sublist3r_json(json_text: str) -> Dict[str, Any]:
    """Parse sublist3r JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"subdomains": [], "hosts": [], "ips": []}
    
    try:
        data = json.loads(json_text)
        if isinstance(data, list):
            for subdomain in data:
                metadata["subdomains"].append({
                    "subdomain": subdomain,
                    "source": "sublist3r"
                })
                metadata["hosts"].append({
                    "hostname": subdomain
                })
        elif isinstance(data, dict) and "subdomains" in data:
            for subdomain in data["subdomains"]:
                metadata["subdomains"].append({
                    "subdomain": subdomain,
                    "source": "sublist3r"
                })
                metadata["hosts"].append({
                    "hostname": subdomain
                })
    except (json.JSONDecodeError, KeyError) as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class Sublist3rTool(BaseTool):
    """Run sublist3r subdomain enumeration and parse the results."""

    args_model = Sublist3rArgs

    def build_command(self, args: Sublist3rArgs) -> List[str]:
        """Build sublist3r command arguments.
        
        Args:
            args: Validated Sublist3rArgs
            
        Returns:
            List of command arguments for sublist3r
        """
        cmd = ["sublist3r"]
        
        # Add target domain (-d flag)
        cmd.extend(["-d", args.target])
        
        # Add search engines (-e flag)
        if args.search_engines:
            engines = ",".join([engine.value for engine in args.search_engines])
            cmd.extend(["-e", engines])
        
        # Add threads
        cmd.extend(["-t", str(args.threads)])
        
        # Add verbose option
        if args.verbose:
            cmd.append("-v")
        
        # Add bruteforce option
        if args.bruteforce:
            cmd.append("-b")
        
        # Add output file if specified (required for JSON output)
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: Sublist3rArgs,
    ) -> Dict[str, Any]:
        """Parse sublist3r output into structured metadata."""
        if args.output_format == OutputFormat.JSON and stdout:
            return parse_sublist3r_json(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: Sublist3rArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create sublist3r artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            ext = args.output_format.value
            artifact_path = f"artifacts/sublist3r_{ts}.{ext}"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: Sublist3rArgs) -> ToolResult:
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

        metadata = {}
        artifacts: List[str] = []
        
        if args.output_format == OutputFormat.JSON and proc.stdout:
            metadata = parse_sublist3r_json(proc.stdout)
            timestamp = int(start)
            artifact_path = f"artifacts/sublist3r_{timestamp}.json"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(proc.stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional

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
        tool_id="information_gathering.dns.sublist3r",
        display_name="Sublist3r",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="subdomain_enumeration",
                description="Enumerate subdomains for a domain via search engines and CT logs; returns hostnames only; use for fast passive subdomain discovery",
                output_indicators=["subdomain", "Found"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=10,
    )
)