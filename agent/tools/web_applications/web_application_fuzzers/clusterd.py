"""Clusterd - Web Application Server Enumeration and Exploitation Tool."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class OutputFormat(str, Enum):
    """Output format options for Clusterd."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class ServerType(str, Enum):
    """Supported server types for Clusterd."""
    TOMCAT = "tomcat"
    JBOSS = "jboss"
    WEBLOGIC = "weblogic"
    WEBSPHERE = "websphere"
    GLASSFISH = "glassfish"
    JETTY = "jetty"
    RESIN = "resin"
    COLD_FUSION = "coldfusion"
    RAILS = "rails"
    DJANGO = "django"
    FLASK = "flask"
    NODEJS = "nodejs"
    PHP = "php"
    ASP = "asp"
    ASPX = "aspx"


class ClusterdArgs(BaseToolArgs):
    """Arguments for the Clusterd tool."""
    
    server_type: ServerType = Field(
        default=ServerType.TOMCAT,
        description="Type of application server to enumerate"
    )
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for the tool"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    timeout: int = Field(
        default=300,
        description="Timeout in seconds for the scan",
        ge=30,
        le=3600
    )
    
    threads: int = Field(
        default=10,
        description="Number of threads to use",
        ge=1,
        le=50
    )
    
    exploit: bool = Field(
        default=False,
        description="Attempt exploitation after enumeration"
    )
    
    custom_payload: Optional[str] = Field(
        None,
        description="Custom payload to use for exploitation"
    )
    
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string"
    )


def parse_clusterd_output(output_text: str) -> Dict[str, Any]:
    """Parse Clusterd output into structured metadata."""
    metadata: Dict[str, Any] = {
        "servers_found": 0,
        "vulnerabilities_found": 0,
        "exploits_attempted": 0,
        "server_type": "unknown",
        "execution_status": "unknown"
    }
    
    try:
        # Parse common Clusterd output patterns
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Look for server type information
            if any(server in line.lower() for server in [
                "tomcat", "jboss", "weblogic", "websphere", "glassfish"
            ]):
                metadata["server_type"] = line.split(":")[-1].strip()
                
            # Look for server discovery
            if "found" in line.lower() and "server" in line.lower():
                metadata["servers_found"] += 1
                
            # Look for vulnerability indicators
            if any(indicator in line.lower() for indicator in [
                "vulnerable", "exploit", "cve", "vulnerability"
            ]):
                metadata["vulnerabilities_found"] += 1
                
            # Look for exploit attempts
            if "exploit" in line.lower() and "attempt" in line.lower():
                metadata["exploits_attempted"] += 1
                
            # Look for execution status
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
                
    except Exception:
        # If parsing fails, return basic metadata
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class ClusterdTool(BaseTool):
    """Run Clusterd web application server enumeration and exploitation."""
    
    args_model = ClusterdArgs
    
    def run(self, args: ClusterdArgs) -> ToolResult:
        # Build command array
        cmd = ["clusterd"]
        
        # Add server type
        cmd.extend(["-s", args.server_type.value])
        
        # Add output format
        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["-o", args.output_format.value])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("-v")
        
        # Add thread count
        if args.threads != 10:
            cmd.extend(["-t", str(args.threads)])
        
        # Add exploit flag
        if args.exploit:
            cmd.append("-e")
        
        # Add custom payload if specified
        if args.custom_payload:
            cmd.extend(["-p", args.custom_payload])
        
        # Add custom user agent if specified
        if args.user_agent:
            cmd.extend(["-u", args.user_agent])
        
        # Add target (usually last)
        cmd.append(args.target)
        
        # Execute with timing
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
        
        # Parse output for metadata
        metadata = parse_clusterd_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/clusterd_{timestamp}.txt"
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


from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_application_fuzzers.clusterd",
        display_name="Clusterd",
        category=ToolCategory.WEB_FUZZING,
        applicable_phases=[
            PentestPhase.ENUMERATION,
            PentestPhase.VULNERABILITY_ASSESSMENT,
        ],
        capabilities=[
            ToolCapability(
                name="app_server_fingerprinting",
                description="Fingerprint and exploit Java/.NET app servers (JBoss, ColdFusion, Tomcat, Weblogic, Axis2, Rails); use for stack-specific enumeration, not generic fuzzing",
                output_indicators=["server", "version", "module"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)
