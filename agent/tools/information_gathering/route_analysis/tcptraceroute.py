"""TCPTraceroute - TCP-based route analysis tool for network path discovery.

Uses TCP SYN packets instead of UDP/ICMP, useful for tracing through firewalls.
Output is always text format.
"""

from __future__ import annotations

import os
import subprocess
import time
import re
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class TcpTracerouteArgs(BaseToolArgs):
    """Arguments for the TCPTraceroute tool.
    
    tcptraceroute uses TCP SYN packets to trace routes, useful when
    ICMP/UDP traceroute is blocked by firewalls.
    """
    
    destination_port: int = Field(
        80,
        ge=1,
        le=65535,
        description="Destination TCP port to probe (default: 80)",
    )
    max_hops: int = Field(
        30,
        ge=1,
        le=255,
        description="Maximum number of hops",
    )
    first_hop: int = Field(
        1,
        ge=1,
        le=30,
        description="First TTL to start from",
    )
    queries: int = Field(
        3,
        ge=1,
        le=10,
        description="Number of probe packets per hop",
    )
    wait_time: int = Field(
        5,
        ge=1,
        le=60,
        description="Wait time in seconds for response",
    )
    source_address: Optional[str] = Field(
        None,
        description="Source IP address for outgoing probes",
    )
    source_port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Source port for outgoing probes",
    )
    numeric: bool = Field(
        False,
        description="Show numeric addresses only, skip DNS resolution",
    )

def parse_tcptraceroute_output(output_text: str) -> Dict[str, Any]:
    """Parse TCPTraceroute output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "hops": [],
        "route_summary": {},
        "target_info": {}
    }
    
    try:
        lines = output_text.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse hop information
            hop_pattern = r"^\s*(\d+)\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)"
            hop_match = re.match(hop_pattern, line)
            
            if hop_match:
                hop_num = int(hop_match.group(1))
                host = hop_match.group(2)
                ip = hop_match.group(3)
                time_ms = hop_match.group(4)
                
                hop_data = {
                    "hop_number": hop_num,
                    "hostname": host,
                    "ip_address": ip,
                    "response_time": time_ms
                }
                
                # Extract additional information if available
                if "ms" in line:
                    time_match = re.search(r"(\d+\.?\d*)\s*ms", line)
                    if time_match:
                        hop_data["response_time_ms"] = float(time_match.group(1))
                
                metadata["hops"].append(hop_data)
            
            # Extract target information
            if "target" in line.lower() or "destination" in line.lower():
                target_match = re.search(r"([^\s]+)\s*\(([^)]+)\)", line)
                if target_match:
                    metadata["target_info"]["hostname"] = target_match.group(1)
                    metadata["target_info"]["ip_address"] = target_match.group(2)
        
        # Calculate route summary
        if metadata["hops"]:
            metadata["route_summary"]["total_hops"] = len(metadata["hops"])
            metadata["route_summary"]["max_response_time"] = max(
                [hop.get("response_time_ms", 0) for hop in metadata["hops"]]
            )
            metadata["route_summary"]["min_response_time"] = min(
                [hop.get("response_time_ms", 0) for hop in metadata["hops"]]
            )
            metadata["route_summary"]["avg_response_time"] = sum(
                [hop.get("response_time_ms", 0) for hop in metadata["hops"]]
            ) / len(metadata["hops"])
        
        # Extract timing information
        time_pattern = r"(\d+\.?\d*)\s*(seconds?|minutes?)"
        time_matches = re.findall(time_pattern, output_text)
        if time_matches:
            metadata["route_summary"]["execution_time"] = time_matches[0]
            
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata

class TcpTracerouteTool(BaseTool):
    """TCPTraceroute - TCP-based route analysis for firewall traversal.
    
    Uses TCP SYN packets instead of ICMP/UDP, allowing route tracing
    through firewalls that block traditional traceroute.
    """
    
    args_model = TcpTracerouteArgs

    def build_command(self, args: TcpTracerouteArgs) -> List[str]:
        """Build tcptraceroute command arguments.
        
        Args:
            args: Validated TcpTracerouteArgs
            
        Returns:
            List of command arguments for tcptraceroute
        """
        cmd = ["tcptraceroute"]
        
        # Max hops
        cmd.extend(["-m", str(args.max_hops)])
        
        # First hop TTL
        if args.first_hop > 1:
            cmd.extend(["-f", str(args.first_hop)])
        
        # Queries per hop
        cmd.extend(["-q", str(args.queries)])
        
        # Wait time
        cmd.extend(["-w", str(args.wait_time)])
        
        # Source address
        if args.source_address:
            cmd.extend(["-s", args.source_address])
        
        # Source port
        if args.source_port:
            cmd.extend(["-p", str(args.source_port)])
        
        # Numeric only
        if args.numeric:
            cmd.append("-n")
        
        # Target host and port (positional args at end)
        cmd.append(args.target)
        cmd.append(str(args.destination_port))
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TcpTracerouteArgs,
    ) -> Dict[str, Any]:
        """Parse tcptraceroute output into structured metadata."""
        if stdout:
            return parse_tcptraceroute_output(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: TcpTracerouteArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create tcptraceroute artifact files from output."""
        artifacts: List[str] = []
        if stdout and len(stdout) > 50:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/tcptraceroute_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: TcpTracerouteArgs) -> ToolResult:
        cmd = self.build_command(args)
        
        # Calculate overall timeout
        overall_timeout = args.max_hops * args.queries * args.wait_time + 30

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
        tool_id="information_gathering.route_analysis.tcptraceroute",
        display_name="TCPTraceroute",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="tcp_path_tracing",
                description="Trace path to a host:port using TCP SYN probes; returns hops and reachability; prefer when ICMP/UDP traceroute is filtered or port-specific path matters",
                output_indicators=["hop", "ms", "port"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
