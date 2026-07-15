"""OWASP ZAP (Zed Attack Proxy) web application security testing tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class ScanMode(str, Enum):
    """Supported ZAP scan modes."""

    BASELINE = "baseline"
    FULL = "full"
    API = "api"
    CUSTOM = "custom"


class ScanLevel(str, Enum):
    """ZAP scan levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    INSANE = "insane"


class ZapProxyArgs(BaseToolArgs):
    """Arguments for the OWASP ZAP tool."""

    scan_mode: ScanMode = Field(
        ScanMode.BASELINE,
        description="Scan mode to perform (baseline, full, api, custom)",
    )
    scan_level: ScanLevel = Field(
        ScanLevel.MEDIUM,
        description="Scan level (low, medium, high, insane)",
    )
    context_file: Optional[str] = Field(
        None,
        description="Path to ZAP context file for authentication and session management",
    )
    policy_file: Optional[str] = Field(
        None,
        description="Path to custom scan policy file",
    )
    exclude_urls: Optional[List[str]] = Field(
        None,
        description="URLs to exclude from scanning",
    )
    include_urls: Optional[List[str]] = Field(
        None,
        description="URLs to specifically include in scanning",
    )
    output_format: Literal["json", "xml", "html", "sarif"] = Field(
        "json",
        description="Output format for the scan results",
    )
    report_file: Optional[str] = Field(
        None,
        description="Path to save the scan report",
    )
    api_key: Optional[str] = Field(
        None,
        description="ZAP API key for authentication",
    )
    port: Optional[int] = Field(
        8080,
        description="Port for ZAP proxy to listen on",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for debugging",
    )


def parse_zaproxy_output(output_text: str) -> Dict[str, Any]:
    """Parse ZAP output into structured metadata."""

    metadata: Dict[str, Any] = {
        "alerts": [],
        "urls_scanned": 0,
        "scan_status": "unknown"
    }
    
    try:
        # Try to parse as JSON first
        if output_text.strip().startswith("{"):
            data = json.loads(output_text)
            metadata.update({
                "alerts": data.get("alerts", []),
                "urls_scanned": data.get("urls_scanned", 0),
                "scan_status": data.get("status", "unknown"),
                "scan_duration": data.get("duration", 0),
                "high_alerts": len([a for a in data.get("alerts", []) if a.get("risk") == "High"]),
                "medium_alerts": len([a for a in data.get("alerts", []) if a.get("risk") == "Medium"]),
                "low_alerts": len([a for a in data.get("alerts", []) if a.get("risk") == "Low"]),
                "info_alerts": len([a for a in data.get("alerts", []) if a.get("risk") == "Info"])
            })
        else:
            # Parse text output
            lines = output_text.split("\n")
            for line in lines:
                if "alert" in line.lower():
                    metadata["alerts"].append(line.strip())
                elif "url" in line.lower() and "scanned" in line.lower():
                    try:
                        metadata["urls_scanned"] = int(line.split()[-1])
                    except (ValueError, IndexError):
                        pass
                elif "status:" in line.lower():
                    metadata["scan_status"] = line.split(":", 1)[1].strip()
                    
    except (json.JSONDecodeError, Exception):
        # If parsing fails, extract basic info from text
        metadata["raw_output_length"] = len(output_text)
        metadata["lines_processed"] = len(output_text.split("\n"))
    
    return metadata


class ZapProxyTool(BaseTool):
    """Run OWASP ZAP web application security scans and parse the results."""

    args_model = ZapProxyArgs

    def run(self, args: ZapProxyArgs) -> ToolResult:
        # Build command array
        cmd = ["zap-cli"]
        
        # Add scan mode
        cmd.extend(["--mode", args.scan_mode.value])
        
        # Add scan level
        cmd.extend(["--level", args.scan_level.value])
        
        # Add context file if specified
        if args.context_file:
            cmd.extend(["--context", args.context_file])
        
        # Add policy file if specified
        if args.policy_file:
            cmd.extend(["--policy", args.policy_file])
        
        # Add exclude URLs
        if args.exclude_urls:
            for url in args.exclude_urls:
                cmd.extend(["--exclude", url])
        
        # Add include URLs
        if args.include_urls:
            for url in args.include_urls:
                cmd.extend(["--include", url])
        
        # Add output format
        cmd.extend(["--output-format", args.output_format])
        
        # Add report file
        if args.report_file:
            cmd.extend(["--output", args.report_file])
        
        # Add API key
        if args.api_key:
            cmd.extend(["--api-key", args.api_key])
        
        # Add port
        if args.port:
            cmd.extend(["--port", str(args.port)])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("--verbose")
        
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
        metadata = parse_zaproxy_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/zaproxy_{timestamp}.{args.output_format}"
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
        tool_id="web_applications.web_application_proxies.zaproxy",
        display_name="OWASP ZAP",
        category=ToolCategory.APPLICATION_PROXY,
        applicable_phases=[PentestPhase.VULNERABILITY_ASSESSMENT],
        capabilities=[
            ToolCapability(
                name="proxy_scanning",
                description="Intercepting proxy with active/passive scanning",
                output_indicators=["Alert", "Risk"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=30,
    )
)