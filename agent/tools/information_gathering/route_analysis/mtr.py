"""MTR (My TraceRoute) route analysis tool using Pydantic models.

MTR combines ping and traceroute. Supports JSON output via --json flag.
"""

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


class ProtocolType(str, Enum):
    """MTR protocol types."""
    
    ICMP = "icmp"
    TCP = "tcp"
    UDP = "udp"


class MtrArgs(BaseToolArgs):
    """Arguments for the MTR tool.
    
    MTR combines traceroute and ping functionality.
    Supports JSON output via --json flag.
    """

    protocol: ProtocolType = Field(
        ProtocolType.ICMP,
        description="Protocol: icmp (default), tcp, or udp",
    )
    report_mode: bool = Field(
        True,
        description="Run in report mode (non-interactive, required for scripting)",
    )
    json_output: bool = Field(
        True,
        description="Output in JSON format (recommended for parsing)",
    )
    max_hops: int = Field(
        30,
        ge=1,
        le=255,
        description="Maximum number of hops to trace",
    )
    count: int = Field(
        10,
        ge=1,
        le=100,
        description="Number of pings to send per hop",
    )
    interval: float = Field(
        1.0,
        ge=0.1,
        le=10.0,
        description="Interval between probes in seconds",
    )
    port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Port number for TCP/UDP mode",
    )
    source_address: Optional[str] = Field(
        None,
        description="Source IP address to use",
    )
    numeric: bool = Field(
        False,
        description="Show numeric addresses only, skip DNS resolution",
    )


def parse_mtr_text(text_output: str) -> Dict[str, Any]:
    """Parse MTR text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "hops": [],
        "summary": {},
        "path": [],
        "statistics": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse hop information
            if re.match(r'^\s*\d+\s+', line):
                hop_info = {}
                parts = line.split()
                
                if len(parts) >= 2:
                    hop_info["hop_number"] = int(parts[0])
                    
                    # Extract IP addresses and hostnames
                    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                    if ip_match:
                        hop_info["ip"] = ip_match.group(1)
                    
                    # Extract hostname if present
                    hostname_match = re.search(r'\(([^)]+)\)', line)
                    if hostname_match:
                        hop_info["hostname"] = hostname_match.group(1)
                    
                    # Extract response times
                    times = re.findall(r'(\d+\.?\d*)\s*ms', line)
                    if times:
                        hop_info["response_times"] = [float(t) for t in times]
                        hop_info["avg_time"] = sum(hop_info["response_times"]) / len(hop_info["response_times"])
                    
                    # Extract packet loss
                    loss_match = re.search(r'(\d+)%', line)
                    if loss_match:
                        hop_info["packet_loss"] = int(loss_match.group(1))
                    
                    # Check if hop is unreachable
                    if "*" in line:
                        hop_info["status"] = "unreachable"
                    else:
                        hop_info["status"] = "reachable"
                    
                    metadata["hops"].append(hop_info)
                    metadata["path"].append(hop_info.get("ip", hop_info.get("hostname", f"hop_{hop_info['hop_number']}")))
            
            # Parse summary information
            elif "Start:" in line:
                start_match = re.search(r'Start: (\d+)', line)
                if start_match:
                    metadata["summary"]["start_time"] = int(start_match.group(1))
            elif "HOST:" in line:
                host_match = re.search(r'HOST: ([^\s]+)', line)
                if host_match:
                    metadata["summary"]["destination"] = host_match.group(1)
            elif "Loss%" in line:
                metadata["summary"]["total_packet_loss"] = line.split()[-1]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_hops": len(metadata["hops"]),
                "reachable_hops": len([h for h in metadata["hops"] if h.get("status") == "reachable"]),
                "unreachable_hops": len([h for h in metadata["hops"] if h.get("status") == "unreachable"])
            }
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse MTR output: {str(e)}"
    
    return metadata


def parse_mtr_json(json_text: str) -> Dict[str, Any]:
    """Parse MTR JSON output into structured metadata."""
    
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
            if "destination" in data:
                metadata["summary"]["destination"] = data["destination"]
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


class MtrTool(BaseTool):
    """Run MTR and parse the results.
    
    MTR combines ping and traceroute in one diagnostic tool.
    Requires report mode (-r) for non-interactive use.
    """

    args_model = MtrArgs

    def build_command(self, args: MtrArgs) -> List[str]:
        """Build mtr command arguments.
        
        Args:
            args: Validated MtrArgs
            
        Returns:
            List of command arguments for mtr
        """
        cmd = ["mtr"]
        
        # Report mode (required for non-interactive)
        if args.report_mode:
            cmd.append("-r")
        
        # JSON output
        if args.json_output:
            cmd.append("--json")
        
        # Protocol selection
        if args.protocol == ProtocolType.TCP:
            cmd.append("-T")
        elif args.protocol == ProtocolType.UDP:
            cmd.append("-u")
        # ICMP is default
        
        # Max hops
        cmd.extend(["-m", str(args.max_hops)])
        
        # Count (pings per hop)
        cmd.extend(["-c", str(args.count)])
        
        # Interval
        cmd.extend(["-i", str(args.interval)])
        
        # Port for TCP/UDP
        if args.port and args.protocol in [ProtocolType.TCP, ProtocolType.UDP]:
            cmd.extend(["-P", str(args.port)])
        
        # Source address
        if args.source_address:
            cmd.extend(["-a", args.source_address])
        
        # Numeric only
        if args.numeric:
            cmd.append("-n")
        
        # Target host (must be last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: MtrArgs,
    ) -> Dict[str, Any]:
        """Parse mtr output into structured metadata."""
        if stdout:
            if args.json_output:
                return parse_mtr_json(stdout)
            return parse_mtr_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: MtrArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create mtr artifact files from output."""
        artifacts: List[str] = []
        if stdout and len(stdout) > 50:
            ts = timestamp if timestamp is not None else int(time.time())
            ext = "json" if args.json_output else "txt"
            artifact_path = f"artifacts/mtr_{args.protocol.value}_{ts}.{ext}"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: MtrArgs) -> ToolResult:
        cmd = self.build_command(args)

        # Calculate timeout based on count and interval
        overall_timeout = int(args.count * args.interval * args.max_hops) + 60

        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=overall_timeout,
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
        tool_id="information_gathering.route_analysis.mtr",
        display_name="MTR",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="path_tracing",
                description="Trace a host path with continuous per-hop latency/loss sampling; returns hop, RTT, loss stats; prefer for sustained path quality checks",
                output_indicators=["hop", "ms", "loss"],
            ),
        ],
        required_services=[],
        target_protocols=["icmp", "udp", "tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=3,
    )
)
