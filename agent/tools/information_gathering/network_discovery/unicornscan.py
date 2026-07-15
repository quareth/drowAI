"""Unicornscan asynchronous TCP/UDP port scanner tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import re
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class ScanType(str, Enum):
    """Unicornscan scan types."""
    
    TCP_SYN = "T"
    TCP_ACK = "t"
    UDP = "U"
    ICMP_ECHO = "I"


class OutputFormat(str, Enum):
    """Unicornscan output format options."""
    
    NORMAL = "normal"
    VERBOSE = "verbose"
    XML = "xml"
    JSON = "json"


class UnicornscanArgs(BaseToolArgs):
    """Arguments for the Unicornscan tool."""

    ports: Optional[str] = Field(
        "1-65535",
        description="Port specification (e.g., '80', '1-1000', '22,80,443')",
    )
    scan_type: ScanType = Field(
        ScanType.TCP_SYN,
        description="Type of scan to perform",
    )
    output_format: OutputFormat = Field(
        OutputFormat.NORMAL,
        description="Output format for parsing",
    )
    rate: int = Field(
        1000,
        ge=1,
        le=100000,
        description="Rate of packets per second to send",
    )
    timeout: int = Field(
        300,
        ge=1,
        le=3600,
        description="Timeout in seconds for the scan",
    )
    interface: Optional[str] = Field(
        None,
        description="Network interface to use for scanning",
    )
    source_ip: Optional[str] = Field(
        None,
        description="Source IP address to use for scanning",
    )
    source_port: Optional[str] = Field(
        None,
        description="Source port to use for scanning",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors",
    )
    resolve_hostnames: bool = Field(
        True,
        description="Resolve hostnames to IP addresses",
    )


def parse_unicornscan_output(output_text: str) -> Dict[str, Any]:
    """Parse unicornscan output into structured metadata."""
    
    metadata: Dict[str, Any] = {"open_ports": [], "hosts": []}
    
    try:
        # Parse the output line by line
        lines = output_text.strip().split('\n')
        for line in lines:
            if line.strip():
                # Look for port scan results
                # Format: [IP]:[port]/[protocol] [status]
                match = re.match(r'\[([^\]]+)\]:(\d+)/(\w+)\s+(\w+)', line)
                if match:
                    ip, port, protocol, status = match.groups()
                    if status.lower() == "open":
                        metadata["open_ports"].append({
                            "ip": ip,
                            "port": int(port),
                            "protocol": protocol.lower(),
                            "status": status
                        })
                        # Add to hosts if not already present
                        if not any(h["ip"] == ip for h in metadata["hosts"]):
                            metadata["hosts"].append({
                                "ip": ip,
                                "ports_count": 1
                            })
                        else:
                            # Update existing host's port count
                            for host in metadata["hosts"]:
                                if host["ip"] == ip:
                                    host["ports_count"] += 1
                                    break
    except Exception as e:
        metadata["error"] = f"Failed to parse output: {str(e)}"
    
    return metadata


class UnicornscanTool(BaseTool):
    """Run unicornscan scans and parse the results."""

    args_model = UnicornscanArgs

    def build_command(self, args: UnicornscanArgs) -> List[str]:
        """Build unicornscan command arguments.
        
        Args:
            args: Validated UnicornscanArgs
            
        Returns:
            List of command arguments for unicornscan
        """
        cmd = ["unicornscan"]
        
        # Add scan type (e.g., -mT for TCP SYN, -mU for UDP)
        cmd.append(f"-m{args.scan_type.value}")
        
        # Add ports
        if args.ports:
            cmd.extend(["-p", args.ports])
        
        # Add rate
        cmd.extend(["-r", str(args.rate)])
        
        # Add interface if specified
        if args.interface:
            cmd.extend(["-i", args.interface])
        
        # Add source IP if specified
        if args.source_ip:
            cmd.extend(["-s", args.source_ip])
        
        # Add source port if specified
        if args.source_port:
            cmd.extend(["-S", args.source_port])
        
        # Add verbose option
        if args.verbose:
            cmd.append("-v")
        
        # Add quiet option
        if args.quiet:
            cmd.append("-q")
        
        # Add target with port specification
        # unicornscan uses target:port format
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: UnicornscanArgs,
    ) -> Dict[str, Any]:
        """Parse unicornscan output into structured metadata."""
        if stdout:
            return parse_unicornscan_output(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: UnicornscanArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create unicornscan artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/unicornscan_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: UnicornscanArgs) -> ToolResult:
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
        tool_id="information_gathering.network_discovery.unicornscan",
        display_name="Unicornscan",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="port_discovery",
                description="Discover TCP/UDP ports asynchronously; returns open ports; prefer for specialized stateless scanning, not for routine service enumeration",
                output_indicators=["open", "closed"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=5,
    )
)
