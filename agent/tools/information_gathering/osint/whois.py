"""WHOIS OSINT information gathering tool using Pydantic models."""

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


class QueryType(str, Enum):
    """WHOIS query types."""
    
    DOMAIN = "domain"
    IP = "ip"
    ASN = "asn"
    ORG = "org"
    PERSON = "person"


class OutputFormat(str, Enum):
    """WHOIS output format options."""
    
    JSON = "json"
    TEXT = "text"
    XML = "xml"


class WhoisArgs(BaseToolArgs):
    """Arguments for the WHOIS tool."""

    query_type: QueryType = Field(
        QueryType.DOMAIN,
        description="Type of WHOIS query to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    server: Optional[str] = Field(
        None,
        description="WHOIS server to query",
    )
    port: int = Field(
        43,
        ge=1,
        le=65535,
        description="Port number for WHOIS server",
    )
    timeout: int = Field(
        30,
        ge=5,
        le=300,
        description="Timeout in seconds for WHOIS query",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    recursive: bool = Field(
        False,
        description="Perform recursive queries",
    )
    raw: bool = Field(
        False,
        description="Return raw WHOIS data",
    )


def parse_whois_text(text_output: str) -> Dict[str, Any]:
    """Parse WHOIS text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "domain_info": {},
        "registrar_info": {},
        "nameservers": [],
        "status": [],
        "dates": {},
        "contacts": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('%'):
                continue
            
            # Detect sections
            if "Domain Name:" in line:
                current_section = "domain_info"
            elif "Registrar:" in line:
                current_section = "registrar_info"
            elif "Name Server:" in line:
                current_section = "nameservers"
            elif "Status:" in line:
                current_section = "status"
            elif "Created Date:" in line or "Updated Date:" in line or "Expiration Date:" in line:
                current_section = "dates"
            elif "Registrant:" in line or "Admin:" in line or "Tech:" in line:
                current_section = "contacts"
            
            # Parse content based on current section
            elif current_section == "domain_info":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["domain_info"][key.strip()] = value.strip()
            
            elif current_section == "registrar_info":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["registrar_info"][key.strip()] = value.strip()
            
            elif current_section == "nameservers":
                if ":" in line:
                    key, value = line.split(":", 1)
                    if value.strip():
                        metadata["nameservers"].append(value.strip())
                elif line and not line.startswith("Name Server"):
                    metadata["nameservers"].append(line)
            
            elif current_section == "status":
                if ":" in line:
                    key, value = line.split(":", 1)
                    if value.strip():
                        metadata["status"].append(value.strip())
                elif line and not line.startswith("Status"):
                    metadata["status"].append(line)
            
            elif current_section == "dates":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["dates"][key.strip()] = value.strip()
            
            elif current_section == "contacts":
                if ":" in line:
                    key, value = line.split(":", 1)
                    contact_type = key.strip().lower()
                    if contact_type not in metadata["contacts"]:
                        metadata["contacts"][contact_type] = {}
                    metadata["contacts"][contact_type][key.strip()] = value.strip()
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse WHOIS output: {str(e)}"
    
    return metadata


def parse_whois_json(json_text: str) -> Dict[str, Any]:
    """Parse WHOIS JSON output into structured metadata."""
    
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
            if "server" in data:
                metadata["summary"]["server"] = data["server"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class WhoisTool(BaseTool):
    """Run WHOIS queries and parse the results."""

    args_model = WhoisArgs

    def build_command(self, args: WhoisArgs) -> List[str]:
        """Build whois command arguments.
        
        Args:
            args: Validated WhoisArgs
            
        Returns:
            List of command arguments for whois
        """
        cmd = ["whois"]
        
        # Add server if specified (allows targeting specific registrar)
        if args.server:
            cmd.extend(["-h", args.server])
            
            # Port only makes sense with explicit server
            if args.port != 43:
                cmd.extend(["-p", str(args.port)])
        
        # Add target - keep it simple, whois auto-detects query type
        cmd.append(args.target)
        
        # Note: We intentionally avoid flags like -t, -i, -r, -v as they vary
        # between whois implementations and can cause errors
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: WhoisArgs,
    ) -> Dict[str, Any]:
        """Parse whois output into structured metadata."""
        if stdout:
            return parse_whois_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: WhoisArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create whois artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/whois_{args.query_type.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: WhoisArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.whois",
        display_name="WHOIS",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="registry_lookup",
                description="Lookup domain or IP registration via WHOIS; returns registrar, name servers, and contact metadata; use for ownership and origin intel",
                output_indicators=["Domain Name", "Registrar", "Name Server"],
            ),
            ToolCapability(
                name="contact_extraction",
                description="Extract administrative, technical, and registrant contact metadata from WHOIS payloads",
                output_indicators=["Registrant", "Admin", "Tech"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=5,
        estimated_runtime_minutes=2,
        best_combined_with=["information_gathering.dns.amass"],
    )
)
