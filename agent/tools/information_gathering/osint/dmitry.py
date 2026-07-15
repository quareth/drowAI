"""Dmitry OSINT information gathering tool using Pydantic models."""

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


class SearchType(str, Enum):
    """Dmitry search types."""
    
    WEBSITE = "website"
    EMAIL = "email"
    SUBDOMAIN = "subdomain"
    WHOIS = "whois"
    DNS = "dns"
    BACKUP = "backup"
    ADMIN = "admin"
    ALL = "all"


class OutputFormat(str, Enum):
    """Dmitry output format options."""
    
    JSON = "json"
    TEXT = "text"
    CSV = "csv"


class DmitryArgs(BaseToolArgs):
    """Arguments for the Dmitry tool."""

    search_type: SearchType = Field(
        SearchType.WEBSITE,
        description="Type of Dmitry search to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    depth: int = Field(
        3,
        ge=1,
        le=10,
        description="Depth of search (1-10)",
    )
    timeout: int = Field(
        30,
        ge=5,
        le=300,
        description="Timeout in seconds for requests",
    )
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects",
    )
    save_output: bool = Field(
        True,
        description="Save output to file",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    recursive: bool = Field(
        False,
        description="Perform recursive searches",
    )


def parse_dmitry_text(text_output: str) -> Dict[str, Any]:
    """Parse Dmitry text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "website_info": {},
        "emails": [],
        "subdomains": [],
        "backup_files": [],
        "admin_pages": [],
        "dns_records": {},
        "whois_info": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect sections
            if "Website Information" in line:
                current_section = "website_info"
            elif "Email Addresses" in line:
                current_section = "emails"
            elif "Subdomains" in line:
                current_section = "subdomains"
            elif "Backup Files" in line:
                current_section = "backup_files"
            elif "Admin Pages" in line:
                current_section = "admin_pages"
            elif "DNS Records" in line:
                current_section = "dns_records"
            elif "WHOIS Information" in line:
                current_section = "whois_info"
            
            # Parse content based on current section
            elif current_section == "website_info":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["website_info"][key.strip()] = value.strip()
            
            elif current_section == "emails":
                # Extract email addresses using regex
                emails = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', line)
                metadata["emails"].extend(emails)
            
            elif current_section == "subdomains":
                # Extract subdomains
                if line and not line.startswith("Subdomains"):
                    metadata["subdomains"].append(line.strip())
            
            elif current_section == "backup_files":
                # Extract backup file URLs
                if line and not line.startswith("Backup Files"):
                    metadata["backup_files"].append(line.strip())
            
            elif current_section == "admin_pages":
                # Extract admin page URLs
                if line and not line.startswith("Admin Pages"):
                    metadata["admin_pages"].append(line.strip())
            
            elif current_section == "dns_records":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["dns_records"][key.strip()] = value.strip()
            
            elif current_section == "whois_info":
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["whois_info"][key.strip()] = value.strip()
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse text output: {str(e)}"
    
    return metadata


class DmitryTool(BaseTool):
    """Run Dmitry searches and parse the results."""

    args_model = DmitryArgs

    def build_command(self, args: DmitryArgs) -> List[str]:
        """Build dmitry command arguments.
        
        Args:
            args: Validated DmitryArgs
            
        Returns:
            List of command arguments for dmitry
        """
        cmd = ["dmitry"]
        
        # Add search type flags
        if args.search_type == SearchType.EMAIL:
            cmd.append("-e")
        elif args.search_type == SearchType.SUBDOMAIN:
            cmd.append("-s")
        elif args.search_type == SearchType.WHOIS:
            cmd.append("-w")
        elif args.search_type == SearchType.DNS:
            cmd.append("-n")
        elif args.search_type == SearchType.BACKUP:
            cmd.append("-b")
        elif args.search_type == SearchType.ADMIN:
            cmd.append("-p")  # Port scan for admin pages
        elif args.search_type == SearchType.ALL:
            cmd.extend(["-winsepo"])  # whois, iana, netcraft, subdomain, email, port, output
        elif args.search_type == SearchType.WEBSITE:
            cmd.extend(["-winseo"])  # Standard website info gathering
        
        # Add save output flag with target name
        if args.save_output:
            cmd.extend(["-o", f"{args.target}.txt"])
        
        # Add target (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DmitryArgs,
    ) -> Dict[str, Any]:
        """Parse dmitry output into structured metadata."""
        if stdout:
            return parse_dmitry_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: DmitryArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create dmitry artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/dmitry_{args.search_type.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: DmitryArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.dmitry",
        display_name="DMitry",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="multi_osint",
                description="Enumerate WHOIS, emails, subdomains, and shallow ports for a domain; returns mixed OSINT evidence; use for quick combined recon, not focused scans",
                output_indicators=["email", "subdomain", "whois"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["tcp", "udp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=10,
    )
)