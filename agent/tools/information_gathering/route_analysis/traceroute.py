"""Traceroute route analysis tool.

Standard Linux traceroute - text output only.
Only 'target' is required. All other parameters optional.
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


class TracerouteArgs(BaseToolArgs):
    """Arguments for the Traceroute tool.
    
    Only 'target' is required. All other parameters are optional.
    Tool uses sensible defaults when parameters are omitted.
    """

    protocol: Optional[str] = Field(
        None,
        description="Protocol: udp, tcp, or icmp",
    )
    max_hops: Optional[int] = Field(
        None,
        description="Maximum number of hops",
    )
    wait_time: Optional[int] = Field(
        None,
        description="Wait time per probe in seconds",
    )
    port: Optional[int] = Field(
        None,
        description="Destination port for TCP/UDP",
    )
    source_interface: Optional[str] = Field(
        None,
        description="Network interface to use",
    )
    source_ip: Optional[str] = Field(
        None,
        description="Source IP address",
    )
    numeric: Optional[bool] = Field(
        None,
        description="Numeric output only (skip DNS)",
    )
    queries: Optional[int] = Field(
        None,
        description="Probes per hop",
    )


def parse_traceroute_text(text_output: str) -> Dict[str, Any]:
    """Parse traceroute text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "hops": [],
        "summary": {},
        "path": []
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
                    
                    # Check if hop is unreachable
                    if "*" in line:
                        hop_info["status"] = "unreachable"
                    else:
                        hop_info["status"] = "reachable"
                    
                    metadata["hops"].append(hop_info)
                    metadata["path"].append(hop_info.get("ip", hop_info.get("hostname", f"hop_{hop_info['hop_number']}")))
            
            # Parse summary information
            elif "traceroute to" in line.lower():
                metadata["summary"]["destination"] = line.split()[-1]
            elif "hops max" in line.lower():
                metadata["summary"]["max_hops"] = int(line.split()[0])
        
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
        metadata["error"] = f"Failed to parse traceroute output: {str(e)}"
    
    return metadata


class TracerouteTool(BaseTool):
    """Run traceroute and parse the results.
    
    Standard Linux traceroute. Output is always text format which gets
    parsed into structured metadata.
    """

    args_model = TracerouteArgs

    def build_command(self, args: TracerouteArgs) -> List[str]:
        """Build traceroute command for PTY execution.

        Only includes flags for parameters explicitly provided by the caller.
        """
        cmd: List[str] = ["traceroute"]

        protocol = (args.protocol or "udp").lower()
        if args.protocol is not None:
            if protocol == "tcp":
                cmd.append("-T")
            elif protocol == "icmp":
                cmd.append("-I")
            # UDP is default, no flag needed

        if args.max_hops is not None:
            cmd.extend(["-m", str(args.max_hops)])
        if args.wait_time is not None:
            cmd.extend(["-w", str(args.wait_time)])
        if args.queries is not None:
            cmd.extend(["-q", str(args.queries)])
        if args.port is not None:
            cmd.extend(["-p", str(args.port)])
        if args.source_interface is not None:
            cmd.extend(["-i", args.source_interface])
        if args.source_ip is not None:
            cmd.extend(["-s", args.source_ip])
        if args.numeric is True:
            cmd.append("-n")

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TracerouteArgs,
    ) -> Dict[str, Any]:
        """Parse traceroute output into structured metadata."""
        output_text = stdout or ""
        if not output_text and stderr:
            output_text = stderr
        return parse_traceroute_text(output_text) if output_text else {}

    def create_artifacts(
        self,
        stdout: str,
        args: TracerouteArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Persist traceroute output to a workspace artifact."""
        if not stdout or len(stdout) < 50:
            return []
        ts = int(timestamp) if timestamp is not None else int(time.time())
        protocol = (args.protocol or "udp").lower()
        artifact_path = f"artifacts/traceroute_{protocol}_{ts}.txt"
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: TracerouteArgs) -> ToolResult:
        # Reuse PTY command builder for consistency (single source of truth)
        cmd = self.build_command(args)

        # Calculate a reasonable overall timeout.
        # Use caller override only if explicitly provided; otherwise derive from defaults.
        max_hops = args.max_hops if args.max_hops is not None else 30
        wait_time = args.wait_time if args.wait_time is not None else 5
        queries = args.queries if args.queries is not None else 3
        derived_timeout = max_hops * queries * wait_time + 30
        overall_timeout = args.timeout if "timeout" in getattr(args, "model_fields_set", set()) else derived_timeout

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
        tool_id="information_gathering.route_analysis.traceroute",
        display_name="Traceroute",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="path_tracing",
                description="Trace IP route with TTL probes; returns hops and RTT; prefer for quick route diagnostics, not for port scanning or sustained loss analysis",
                output_indicators=["hop", "ms", "ttl"],
            ),
            ToolCapability(
                name="latency_measurement",
                description="Measure round-trip time to each intermediate router",
                output_indicators=["ms", "time"],
            ),
        ],
        required_services=[],
        target_protocols=["udp", "tcp", "icmp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=4,
        estimated_runtime_minutes=2,
    )
)
