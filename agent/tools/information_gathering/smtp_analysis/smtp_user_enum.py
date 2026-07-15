"""SMTP User Enumeration tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
import re
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class EnumMethod(str, Enum):
    """SMTP user enumeration methods."""
    
    VRFY = "vrfy"
    EXPN = "expn"
    RCPT = "rcpt"
    MAIL = "mail"
    HELO = "helo"


class OutputFormat(str, Enum):
    """SMTP user enumeration output format options."""
    
    JSON = "json"
    TEXT = "text"
    CSV = "csv"


class SmtpUserEnumArgs(BaseToolArgs):
    """Arguments for the SMTP User Enumeration tool."""

    enum_method: EnumMethod = Field(
        EnumMethod.VRFY,
        description="SMTP command to use for enumeration",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    port: int = Field(
        25,
        ge=1,
        le=65535,
        description="SMTP port to connect to",
    )
    timeout: int = Field(
        10,
        ge=1,
        le=60,
        description="Timeout in seconds for SMTP operations",
    )
    username_list: Optional[str] = Field(
        None,
        description="File containing list of usernames to test",
    )
    common_users: bool = Field(
        True,
        description="Test common usernames",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    ssl: bool = Field(
        False,
        description="Use SSL/TLS connection",
    )
    starttls: bool = Field(
        False,
        description="Use STARTTLS",
    )
    auth: bool = Field(
        False,
        description="Attempt authentication",
    )


def parse_smtp_user_enum_text(text_output: str) -> Dict[str, Any]:
    """Parse SMTP user enumeration text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "valid_users": [],
        "invalid_users": [],
        "summary": {},
        "server_info": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse valid users
            if "250" in line or "252" in line:
                user_match = re.search(r'(\w+@[\w\.-]+)', line)
                if user_match:
                    metadata["valid_users"].append(user_match.group(1))
            
            # Parse invalid users
            elif "550" in line or "553" in line:
                user_match = re.search(r'(\w+@[\w\.-]+)', line)
                if user_match:
                    metadata["invalid_users"].append(user_match.group(1))
            
            # Parse server information
            elif "220" in line and "SMTP" in line:
                metadata["server_info"]["banner"] = line
            elif "250" in line and "HELO" in line:
                metadata["server_info"]["helo_response"] = line
            
            # Parse summary information
            elif "Total users tested:" in line:
                total_match = re.search(r'Total users tested: (\d+)', line)
                if total_match:
                    metadata["summary"]["total_tested"] = int(total_match.group(1))
            elif "Valid users found:" in line:
                valid_match = re.search(r'Valid users found: (\d+)', line)
                if valid_match:
                    metadata["summary"]["valid_found"] = int(valid_match.group(1))
            elif "Invalid users found:" in line:
                invalid_match = re.search(r'Invalid users found: (\d+)', line)
                if invalid_match:
                    metadata["summary"]["invalid_found"] = int(invalid_match.group(1))
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_tested": len(metadata["valid_users"]) + len(metadata["invalid_users"]),
                "valid_found": len(metadata["valid_users"]),
                "invalid_found": len(metadata["invalid_users"])
            }
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse SMTP user enumeration output: {str(e)}"
    
    return metadata


def parse_smtp_user_enum_json(json_text: str) -> Dict[str, Any]:
    """Parse SMTP user enumeration JSON output into structured metadata."""
    
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
            if "valid_users" in data:
                metadata["summary"]["valid_users"] = data["valid_users"]
            if "invalid_users" in data:
                metadata["summary"]["invalid_users"] = data["invalid_users"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class SmtpUserEnumTool(BaseTool):
    """Run SMTP user enumeration and parse the results."""

    args_model = SmtpUserEnumArgs

    def build_command(self, args: SmtpUserEnumArgs) -> List[str]:
        """Build smtp-user-enum command arguments.
        
        Args:
            args: Validated SmtpUserEnumArgs
            
        Returns:
            List of command arguments for smtp-user-enum
        """
        cmd = ["smtp-user-enum"]
        
        # Add enumeration mode (-M for mode: VRFY, EXPN, RCPT)
        cmd.extend(["-M", args.enum_method.value.upper()])
        
        # Add username list if specified
        if args.username_list:
            cmd.extend(["-U", args.username_list])
        else:
            # Use default wordlist
            cmd.extend(["-U", "/usr/share/wordlists/metasploit/unix_users.txt"])
        
        # Add target server
        cmd.extend(["-t", args.target])
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SmtpUserEnumArgs,
    ) -> Dict[str, Any]:
        """Parse smtp-user-enum output into structured metadata."""
        if stdout:
            return parse_smtp_user_enum_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: SmtpUserEnumArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create smtp-user-enum artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/smtp_user_enum_{args.enum_method.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: SmtpUserEnumArgs) -> ToolResult:
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
        tool_id="information_gathering.smtp_analysis.smtp_user_enum",
        display_name="SMTP User Enum",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="smtp_user_enumeration",
                description="Enumerate valid SMTP users via VRFY/EXPN/RCPT commands",
                output_indicators=["valid", "invalid", "user"],
            ),
        ],
        required_services=["smtp"],
        target_protocols=["tcp"],
        execution_priority=4,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)