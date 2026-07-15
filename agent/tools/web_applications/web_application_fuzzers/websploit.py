"""WebSploit - Advanced Web Application Fuzzing Tool."""

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
    """Output format options for WebSploit."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class ScanModule(str, Enum):
    """WebSploit scan modules."""
    WEB_SCANNER = "web_scanner"
    WEB_FUZZER = "web_fuzzer"
    WEB_SHELL = "web_shell"
    WEB_CRACKER = "web_cracker"
    WEB_DOS = "web_dos"
    WEB_FAKE_UPDATE = "web_fake_update"
    WEB_FAKE_UPDATE_2 = "web_fake_update_2"
    WEB_FAKE_UPDATE_3 = "web_fake_update_3"
    WEB_FAKE_UPDATE_4 = "web_fake_update_4"
    WEB_FAKE_UPDATE_5 = "web_fake_update_5"
    WEB_FAKE_UPDATE_6 = "web_fake_update_6"
    WEB_FAKE_UPDATE_7 = "web_fake_update_7"
    WEB_FAKE_UPDATE_8 = "web_fake_update_8"
    WEB_FAKE_UPDATE_9 = "web_fake_update_9"
    WEB_FAKE_UPDATE_10 = "web_fake_update_10"


class WebSploitArgs(BaseToolArgs):
    """Arguments for the WebSploit tool."""
    
    module: ScanModule = Field(
        default=ScanModule.WEB_SCANNER,
        description="WebSploit module to use for scanning"
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
    
    custom_payload: Optional[str] = Field(
        None,
        description="Custom payload to use for fuzzing"
    )
    
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string"
    )


def parse_websploit_output(output_text: str) -> Dict[str, Any]:
    """Parse WebSploit output into structured metadata."""
    metadata: Dict[str, Any] = {
        "vulnerabilities_found": 0,
        "scan_modules": [],
        "targets_scanned": 0,
        "execution_status": "unknown"
    }
    
    try:
        # Parse common WebSploit output patterns
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Look for vulnerability indicators
            if any(indicator in line.lower() for indicator in [
                "vulnerable", "found", "exploit", "injection", "xss", "sqli"
            ]):
                metadata["vulnerabilities_found"] += 1
                
            # Look for scan module information
            if "module:" in line.lower():
                metadata["scan_modules"].append(line.split(":")[-1].strip())
                
            # Look for target information
            if "target:" in line.lower() or "scanning:" in line.lower():
                metadata["targets_scanned"] += 1
                
            # Look for execution status
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
                
    except Exception:
        # If parsing fails, return basic metadata
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class WebSploitTool(BaseTool):
    """Run WebSploit web application fuzzing and vulnerability scanning."""
    
    args_model = WebSploitArgs
    
    def run(self, args: WebSploitArgs) -> ToolResult:
        # Build command array
        cmd = ["websploit"]
        
        # Add module selection
        cmd.extend(["-m", args.module.value])
        
        # Add output format
        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["-o", args.output_format.value])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("-v")
        
        # Add thread count
        if args.threads != 10:
            cmd.extend(["-t", str(args.threads)])
        
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
        metadata = parse_websploit_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/websploit_{timestamp}.txt"
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
        tool_id="web_applications.web_application_fuzzers.websploit",
        display_name="WebSploit",
        category=ToolCategory.WEB_FUZZING,
        applicable_phases=[
            PentestPhase.VULNERABILITY_ASSESSMENT,
            PentestPhase.EXPLOITATION,
        ],
        capabilities=[
            ToolCapability(
                name="module_execution",
                description="Run scripted web/network exploitation modules against a target host; use only when a specific module is required, not for discovery",
                output_indicators=["module", "target", "result"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)
