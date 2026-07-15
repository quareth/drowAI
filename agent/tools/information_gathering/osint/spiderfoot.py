"""SpiderFoot OSINT information gathering tool using Pydantic models."""

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


class ScanType(str, Enum):
    """SpiderFoot scan types."""
    
    SF_DOMAIN = "sf_domain"
    SF_IP = "sf_ip"
    SF_EMAIL = "sf_email"
    SF_PHONE = "sf_phone"
    SF_USERNAME = "sf_username"
    SF_PERSON = "sf_person"
    SF_WEBSITE = "sf_website"
    SF_IP_RANGE = "sf_ip_range"
    SF_ASN = "sf_asn"
    SF_BTC = "sf_btc"


class OutputFormat(str, Enum):
    """SpiderFoot output format options."""
    
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    TEXT = "text"


class SpiderFootArgs(BaseToolArgs):
    """Arguments for the SpiderFoot tool."""

    scan_type: ScanType = Field(
        ScanType.SF_DOMAIN,
        description="Type of SpiderFoot scan to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    api_key: Optional[str] = Field(
        None,
        description="SpiderFoot API key for authentication",
    )
    server_url: Optional[str] = Field(
        None,
        description="SpiderFoot server URL",
    )
    max_results: int = Field(
        100,
        ge=1,
        le=1000,
        description="Maximum number of results to return",
    )
    modules: Optional[str] = Field(
        None,
        description="Comma-separated list of modules to use",
    )
    exclude_modules: Optional[str] = Field(
        None,
        description="Comma-separated list of modules to exclude",
    )
    timeout: int = Field(
        60,
        ge=10,
        le=600,
        description="Timeout in seconds for scan execution",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    debug: bool = Field(
        False,
        description="Enable debug mode",
    )
    no_banner: bool = Field(
        True,
        description="Suppress banner output",
    )


def parse_spiderfoot_json(json_text: str) -> Dict[str, Any]:
    """Parse SpiderFoot JSON output into structured metadata."""
    
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
            if "scan_id" in data:
                metadata["summary"]["scan_id"] = data["scan_id"]
            if "status" in data:
                metadata["summary"]["status"] = data["status"]
            if "modules" in data:
                metadata["summary"]["modules"] = data["modules"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


def parse_spiderfoot_text(text_output: str) -> Dict[str, Any]:
    """Parse SpiderFoot text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "results": [],
        "summary": {},
        "modules": [],
        "scan_info": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect sections
            if "Results:" in line:
                current_section = "results"
            elif "Modules:" in line:
                current_section = "modules"
            elif "Scan Info:" in line:
                current_section = "scan_info"
            elif "Summary:" in line:
                current_section = "summary"
            
            # Parse content based on current section
            elif current_section == "results":
                if line and not line.startswith("Results:"):
                    metadata["results"].append(line)
            
            elif current_section == "modules":
                if line and not line.startswith("Modules:"):
                    metadata["modules"].append(line)
            
            elif current_section == "scan_info":
                if ":" in line and not line.startswith("Scan Info:"):
                    key, value = line.split(":", 1)
                    metadata["scan_info"][key.strip()] = value.strip()
            
            elif current_section == "summary":
                if ":" in line and not line.startswith("Summary:"):
                    key, value = line.split(":", 1)
                    metadata["summary"][key.strip()] = value.strip()
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse text output: {str(e)}"
    
    return metadata


class SpiderFootTool(BaseTool):
    """Run SpiderFoot scans and parse the results."""

    args_model = SpiderFootArgs

    def build_command(self, args: SpiderFootArgs) -> List[str]:
        """Build spiderfoot command arguments.
        
        Args:
            args: Validated SpiderFootArgs
            
        Returns:
            List of command arguments for spiderfoot CLI
        """
        cmd = ["spiderfoot"]
        
        # Use CLI scan mode (-s for scan target)
        cmd.extend(["-s", args.target])
        
        # Add scan type (defines what to look for)
        # Map scan types to spiderfoot types
        type_map = {
            ScanType.SF_DOMAIN: "DOMAIN_NAME",
            ScanType.SF_IP: "IP_ADDRESS",
            ScanType.SF_EMAIL: "EMAILADDR",
            ScanType.SF_PHONE: "PHONE_NUMBER",
            ScanType.SF_USERNAME: "USERNAME",
            ScanType.SF_PERSON: "HUMAN_NAME",
            ScanType.SF_WEBSITE: "INTERNET_NAME",
            ScanType.SF_IP_RANGE: "NETBLOCK_OWNER",
            ScanType.SF_ASN: "ASN",
            ScanType.SF_BTC: "BITCOIN_ADDRESS",
        }
        sf_type = type_map.get(args.scan_type, "DOMAIN_NAME")
        cmd.extend(["-t", sf_type])
        
        # Add modules if specified
        if args.modules:
            cmd.extend(["-m", args.modules])
        
        # Add output in quiet/parseable mode
        cmd.append("-q")
        
        # Add output format
        if args.output_format == OutputFormat.JSON:
            cmd.extend(["-o", "json"])
        elif args.output_format == OutputFormat.CSV:
            cmd.extend(["-o", "csv"])
        else:
            cmd.extend(["-o", "tab"])
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SpiderFootArgs,
    ) -> Dict[str, Any]:
        """Parse spiderfoot output into structured metadata."""
        if stdout:
            if args.output_format == OutputFormat.JSON:
                return parse_spiderfoot_json(stdout)
            return parse_spiderfoot_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: SpiderFootArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create spiderfoot artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            ext = args.output_format.value if args.output_format != OutputFormat.TEXT else "txt"
            artifact_path = f"artifacts/spiderfoot_{args.scan_type.value}_{ts}.{ext}"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: SpiderFootArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.spiderfoot",
        display_name="SpiderFoot",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="automated_osint",
                description="Run automated OSINT correlation across multiple sources; returns aggregated hosts, emails, IPs; use for broad passive recon, not focused lookups",
                output_indicators=["domain", "email", "IP", "social"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=30,
    )
)