"""Swaks (Swiss Army Knife for SMTP) tool using Pydantic models."""

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


class SwaksMode(str, Enum):
    """Swaks operation modes."""
    
    SEND = "send"
    TEST = "test"
    PROBE = "probe"
    VERIFY = "verify"
    ENUM = "enum"


class OutputFormat(str, Enum):
    """Swaks output format options."""
    
    JSON = "json"
    TEXT = "text"
    XML = "xml"


class SwaksArgs(BaseToolArgs):
    """Arguments for the Swaks tool."""

    mode: SwaksMode = Field(
        SwaksMode.TEST,
        description="Operation mode for Swaks",
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
        30,
        ge=5,
        le=300,
        description="Timeout in seconds for SMTP operations",
    )
    from_address: Optional[str] = Field(
        None,
        description="From email address",
    )
    to_address: Optional[str] = Field(
        None,
        description="To email address",
    )
    subject: Optional[str] = Field(
        None,
        description="Email subject line",
    )
    body: Optional[str] = Field(
        None,
        description="Email body content",
    )
    attachment: Optional[str] = Field(
        None,
        description="File to attach",
    )
    header: Optional[str] = Field(
        None,
        description="Custom email header",
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
    username: Optional[str] = Field(
        None,
        description="Username for authentication",
    )
    password: Optional[str] = Field(
        None,
        description="Password for authentication",
    )


def parse_swaks_text(text_output: str) -> Dict[str, Any]:
    """Parse Swaks text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "connection_info": {},
        "email_info": {},
        "server_response": {},
        "summary": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse connection information
            if "Connecting to" in line:
                metadata["connection_info"]["target"] = line.split()[-1]
            elif "Connected to" in line:
                metadata["connection_info"]["connected"] = line.split()[-1]
            elif "Connection established" in line:
                metadata["connection_info"]["status"] = "established"
            
            # Parse email information
            elif "From:" in line:
                from_match = re.search(r'From: ([^\s]+)', line)
                if from_match:
                    metadata["email_info"]["from"] = from_match.group(1)
            elif "To:" in line:
                to_match = re.search(r'To: ([^\s]+)', line)
                if to_match:
                    metadata["email_info"]["to"] = to_match.group(1)
            elif "Subject:" in line:
                subject_match = re.search(r'Subject: (.+)', line)
                if subject_match:
                    metadata["email_info"]["subject"] = subject_match.group(1)
            
            # Parse server response
            elif re.match(r'^\d{3}', line):
                code_match = re.search(r'^(\d{3})', line)
                if code_match:
                    code = code_match.group(1)
                    if code not in metadata["server_response"]:
                        metadata["server_response"][code] = []
                    metadata["server_response"][code].append(line)
            
            # Parse summary information
            elif "Result: SUCCESS" in line:
                metadata["summary"]["result"] = "SUCCESS"
            elif "Result: FAIL" in line:
                metadata["summary"]["result"] = "FAIL"
            elif "Message-ID:" in line:
                msgid_match = re.search(r'Message-ID: ([^\s]+)', line)
                if msgid_match:
                    metadata["summary"]["message_id"] = msgid_match.group(1)
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "result": "UNKNOWN",
                "server_responses": len(metadata["server_response"])
            }
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse Swaks output: {str(e)}"
    
    return metadata


def parse_swaks_json(json_text: str) -> Dict[str, Any]:
    """Parse Swaks JSON output into structured metadata."""
    
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
            if "result" in data:
                metadata["summary"]["result"] = data["result"]
            if "message_id" in data:
                metadata["summary"]["message_id"] = data["message_id"]
            if "server_response" in data:
                metadata["summary"]["server_response"] = data["server_response"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class SwaksTool(BaseTool):
    """Run Swaks and parse the results."""

    args_model = SwaksArgs

    def build_command(self, args: SwaksArgs) -> List[str]:
        """Build swaks command arguments.
        
        Args:
            args: Validated SwaksArgs
            
        Returns:
            List of command arguments for swaks
        """
        cmd = ["swaks"]
        
        # Add target server
        cmd.extend(["--to", args.to_address if args.to_address else f"test@{args.target}"])
        cmd.extend(["--server", args.target])
        
        # Add port
        cmd.extend(["--port", str(args.port)])
        
        # Add timeout
        cmd.extend(["--timeout", str(args.timeout)])
        
        # Add from address
        if args.from_address:
            cmd.extend(["--from", args.from_address])
        
        # Add subject/body for sending
        if args.mode == SwaksMode.SEND:
            if args.subject:
                cmd.extend(["--header", f"Subject: {args.subject}"])
            if args.body:
                cmd.extend(["--body", args.body])
            if args.attachment:
                cmd.extend(["--attach", args.attachment])
        
        # Add custom header
        if args.header:
            cmd.extend(["--header", args.header])
        
        # Add TLS options
        if args.ssl:
            cmd.append("--tls")
        elif args.starttls:
            cmd.append("--tls-on-connect")
        
        # Add authentication
        if args.auth and args.username and args.password:
            cmd.extend(["--auth-user", args.username])
            cmd.extend(["--auth-password", args.password])
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SwaksArgs,
    ) -> Dict[str, Any]:
        """Parse swaks output into structured metadata."""
        if stdout:
            return parse_swaks_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: SwaksArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create swaks artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/swaks_{args.mode.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: SwaksArgs) -> ToolResult:
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
        tool_id="information_gathering.smtp_analysis.swaks",
        display_name="swaks",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="smtp_interaction",
                description="Test and interact with SMTP servers",
                output_indicators=["220", "250", "Relay"],
            ),
        ],
        required_services=["smtp"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=3,
    )
)