"""IKE-scan OSINT information gathering tool using Pydantic models."""

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


class ScanType(str, Enum):
    """IKE-scan scan types."""
    
    AGGRESSIVE = "aggressive"
    MAIN = "main"
    QUICK = "quick"
    TRANS = "trans"
    VERSION = "version"
    BACKOFF = "backoff"


class OutputFormat(str, Enum):
    """IKE-scan output format options."""
    
    JSON = "json"
    TEXT = "text"
    XML = "xml"


class IkeScanArgs(BaseToolArgs):
    """Arguments for the IKE-scan tool."""

    scan_type: ScanType = Field(
        ScanType.MAIN,
        description="Type of IKE-scan to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    port: int = Field(
        500,
        ge=1,
        le=65535,
        description="Port number for IKE service",
    )
    timeout: int = Field(
        30,
        ge=5,
        le=300,
        description="Timeout in seconds for scan",
    )
    retries: int = Field(
        3,
        ge=1,
        le=10,
        description="Number of retries for failed packets",
    )
    interval: float = Field(
        0.1,
        ge=0.01,
        le=10.0,
        description="Interval between packets in seconds",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    debug: bool = Field(
        False,
        description="Enable debug mode",
    )
    raw: bool = Field(
        False,
        description="Show raw packet data",
    )


def parse_ike_scan_text(text_output: str) -> Dict[str, Any]:
    """Parse IKE-scan text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "hosts": [],
        "summary": {},
        "transforms": [],
        "vendors": []
    }
    
    try:
        lines = text_output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse host information
            if re.match(r'^\d+\.\d+\.\d+\.\d+', line):
                host_info = {}
                parts = line.split()
                if len(parts) >= 2:
                    host_info["ip"] = parts[0]
                    host_info["status"] = parts[1]
                    
                    # Extract additional information
                    if len(parts) > 2:
                        host_info["details"] = " ".join(parts[2:])
                    
                    metadata["hosts"].append(host_info)
            
            # Parse transform information
            elif "Transform:" in line:
                transform_info = {}
                if ":" in line:
                    key, value = line.split(":", 1)
                    transform_info["type"] = key.strip()
                    transform_info["value"] = value.strip()
                    metadata["transforms"].append(transform_info)
            
            # Parse vendor information
            elif "Vendor:" in line:
                vendor_info = {}
                if ":" in line:
                    key, value = line.split(":", 1)
                    vendor_info["vendor"] = key.strip()
                    vendor_info["id"] = value.strip()
                    metadata["vendors"].append(vendor_info)
            
            # Parse summary information
            elif "Ending" in line or "Starting" in line:
                if ":" in line:
                    key, value = line.split(":", 1)
                    metadata["summary"][key.strip()] = value.strip()
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_hosts": len(metadata["hosts"]),
                "total_transforms": len(metadata["transforms"]),
                "total_vendors": len(metadata["vendors"])
            }
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse IKE-scan output: {str(e)}"
    
    return metadata


def parse_ike_scan_json(json_text: str) -> Dict[str, Any]:
    """Parse IKE-scan JSON output into structured metadata."""
    
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
            if "scan_type" in data:
                metadata["summary"]["scan_type"] = data["scan_type"]
            if "status" in data:
                metadata["summary"]["status"] = data["status"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class IkeScanTool(BaseTool):
    """Run IKE-scan and parse the results."""

    args_model = IkeScanArgs

    def build_command(self, args: IkeScanArgs) -> List[str]:
        """Build ike-scan command arguments.
        
        Args:
            args: Validated IkeScanArgs
            
        Returns:
            List of command arguments for ike-scan
        """
        cmd = ["ike-scan"]
        
        # Add scan type
        if args.scan_type == ScanType.AGGRESSIVE:
            cmd.append("--aggressive")
        elif args.scan_type == ScanType.MAIN:
            cmd.append("--main")  # Main mode (default)
        
        # Add destination port (default 500)
        if args.port != 500:
            cmd.extend(["--destport", str(args.port)])
        
        # Add retries
        cmd.extend(["--retry", str(args.retries)])
        
        # Add interval (in milliseconds for ike-scan)
        interval_ms = int(args.interval * 1000)
        cmd.extend(["--interval", str(interval_ms)])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("--verbose")
        
        # Request vendor ID info
        cmd.append("--showbackoff")
        
        # Add target (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: IkeScanArgs,
    ) -> Dict[str, Any]:
        """Parse ike-scan output into structured metadata."""
        if stdout:
            return parse_ike_scan_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: IkeScanArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create ike-scan artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/ike_scan_{args.scan_type.value}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: IkeScanArgs) -> ToolResult:
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
        tool_id="information_gathering.osint.ike_scan",
        display_name="IKE-Scan",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="vpn_discovery",
                description="Discover and fingerprint IKE/IPsec VPN endpoints on UDP/500 for a host; returns IKE transforms and vendor IDs; use for VPN-specific enumeration",
                output_indicators=["IKE", "transform", "vendor"],
            ),
        ],
        required_services=["ike"],
        target_protocols=["udp"],
        execution_priority=4,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)