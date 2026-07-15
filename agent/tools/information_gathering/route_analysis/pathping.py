"""Pathping route analysis tool using Pydantic models.

NOTE: pathping is a WINDOWS-ONLY command. On Linux/Kali, use 'mtr' instead
which provides similar functionality (combined ping + traceroute with stats).
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
import re
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class PathpingArgs(BaseToolArgs):
    """Arguments for the Windows Pathping tool.
    
    NOTE: This tool only works on Windows. Use 'mtr' on Linux.
    
    Windows pathping combines traceroute with ping statistics,
    sending multiple pings to each hop to measure packet loss.
    """

    max_hops: int = Field(
        30,
        ge=1,
        le=255,
        description="Maximum number of hops (-h)",
    )
    queries: int = Field(
        100,
        ge=1,
        le=1000,
        description="Number of queries per hop (-q)",
    )
    timeout: int = Field(
        4000,
        ge=1000,
        le=60000,
        description="Timeout in milliseconds for each ping (-w)",
    )
    numeric: bool = Field(
        False,
        description="Do not resolve addresses to hostnames (-n)",
    )
    source_address: Optional[str] = Field(
        None,
        description="Source address to use (-i)",
    )


def parse_pathping_text(text_output: str) -> Dict[str, Any]:
    """Parse pathping text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "hops": [],
        "summary": {},
        "path": [],
        "statistics": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect sections
            if "Tracing route to" in line:
                current_section = "tracing"
                metadata["summary"]["destination"] = line.split()[-1]
            elif "Computing statistics" in line:
                current_section = "statistics"
            elif "Trace complete" in line:
                current_section = "complete"
            
            # Parse hop information
            elif re.match(r'^\s*\d+\s+', line) and current_section == "tracing":
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
                    
                    # Check if hop is unreachable
                    if "*" in line:
                        hop_info["status"] = "unreachable"
                    else:
                        hop_info["status"] = "reachable"
                    
                    metadata["hops"].append(hop_info)
                    metadata["path"].append(hop_info.get("ip", hop_info.get("hostname", f"hop_{hop_info['hop_number']}")))
            
            # Parse statistics
            elif current_section == "statistics" and re.match(r'^\s*\d+\s+', line):
                stat_info = {}
                parts = line.split()
                
                if len(parts) >= 4:
                    stat_info["hop_number"] = int(parts[0])
                    stat_info["ip"] = parts[1]
                    
                    # Extract packet loss
                    loss_match = re.search(r'(\d+)%', line)
                    if loss_match:
                        stat_info["packet_loss"] = int(loss_match.group(1))
                    
                    # Extract response times
                    time_match = re.search(r'(\d+\.?\d*)/(\d+\.?\d*)/(\d+\.?\d*)', line)
                    if time_match:
                        stat_info["min_time"] = float(time_match.group(1))
                        stat_info["avg_time"] = float(time_match.group(2))
                        stat_info["max_time"] = float(time_match.group(3))
                    
                    metadata["statistics"][stat_info["hop_number"]] = stat_info
            
            # Parse summary information
            elif "Packets: Sent =" in line:
                sent_match = re.search(r'Sent = (\d+)', line)
                received_match = re.search(r'Received = (\d+)', line)
                lost_match = re.search(r'Lost = (\d+)', line)
                
                if sent_match:
                    metadata["summary"]["packets_sent"] = int(sent_match.group(1))
                if received_match:
                    metadata["summary"]["packets_received"] = int(received_match.group(1))
                if lost_match:
                    metadata["summary"]["packets_lost"] = int(lost_match.group(1))
        
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
        metadata["error"] = f"Failed to parse pathping output: {str(e)}"
    
    return metadata


class PathpingTool(BaseTool):
    """Run Windows pathping and parse the results.
    
    NOTE: This is a Windows-only tool. On Linux, use 'mtr' instead.
    """

    args_model = PathpingArgs

    def run(self, args: PathpingArgs) -> ToolResult:
        start = time.time()
        
        # Check if running on Windows
        if platform.system() != "Windows":
            return ToolResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr="pathping is a Windows-only command. On Linux/Kali, use 'mtr' instead which provides similar functionality (combined traceroute + ping statistics).",
                artifacts=[],
                metadata={"error": "windows_only", "alternative": "mtr"},
                execution_time=time.time() - start,
            )
        
        cmd = ["pathping"]
        
        # Max hops
        cmd.extend(["-h", str(args.max_hops)])
        
        # Number of queries per hop
        cmd.extend(["-q", str(args.queries)])
        
        # Timeout in milliseconds
        cmd.extend(["-w", str(args.timeout)])
        
        # Numeric only
        if args.numeric:
            cmd.append("-n")
        
        # Source address
        if args.source_address:
            cmd.extend(["-i", args.source_address])
        
        # Target host (must be last)
        cmd.append(args.target)
        
        # Calculate overall timeout (pathping can take a long time)
        overall_timeout = (args.queries * args.max_hops * args.timeout // 1000) + 300

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
        
        # Parse text output
        metadata = parse_pathping_text(proc.stdout) if proc.stdout else {}
        
        # Save artifact
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 50:
            timestamp = int(start)
            artifact_path = f"artifacts/pathping_{timestamp}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(proc.stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        
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
        tool_id="information_gathering.route_analysis.pathping",
        display_name="Pathping",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="path_statistics",
                description="Trace a host path while aggregating per-hop loss over multiple rounds; returns loss and latency; prefer when repeated loss statistics are needed",
                output_indicators=["hop", "loss%", "ms"],
            ),
        ],
        required_services=[],
        target_protocols=["icmp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=5,
    )
)
