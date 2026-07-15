"""Zmap fast single-packet network scanner tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class RateLimit(str, Enum):
    """Zmap rate limiting options."""
    
    SLOW = "1000"
    NORMAL = "10000"
    FAST = "100000"
    VERY_FAST = "1000000"


class OutputFormat(str, Enum):
    """Zmap output format options."""
    
    JSON = "json"
    CSV = "csv"
    LIST = "list"
    BINARY = "binary"


class ProbeModule(str, Enum):
    """Zmap probe module options."""
    
    TCP_SYN = "tcp_synscan"
    TCP_ACK = "tcp_ackscan"
    UDP = "udp"
    ICMP_ECHO = "icmp_echoscan"


class ZmapArgs(BaseToolArgs):
    """Arguments for the Zmap tool."""

    ports: Optional[str] = Field(
        "80",
        description="Port specification (e.g., '80', '443', '22,80,443')",
    )
    rate: RateLimit = Field(
        RateLimit.NORMAL,
        description="Rate of packets per second to send",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    probe_module: ProbeModule = Field(
        ProbeModule.TCP_SYN,
        description="Probe module to use for scanning",
    )
    max_retries: int = Field(
        3,
        ge=1,
        le=10,
        description="Maximum number of retries for failed packets",
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
    gateway_mac: Optional[str] = Field(
        None,
        description="Gateway MAC address",
    )
    gateway_ip: Optional[str] = Field(
        None,
        description="Gateway IP address",
    )
    blacklist_file: Optional[str] = Field(
        None,
        description="File containing IP ranges to exclude",
    )
    whitelist_file: Optional[str] = Field(
        None,
        description="File containing IP ranges to include",
    )
    verbosity: int = Field(
        3,
        ge=0,
        le=5,
        description="Verbosity level (0-5)",
    )
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors",
    )


def parse_zmap_json(json_text: str) -> Dict[str, Any]:
    """Parse zmap JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"open_ports": [], "hosts": []}
    
    try:
        # Zmap outputs one JSON object per line
        lines = json_text.strip().split('\n')
        for line in lines:
            if line.strip():
                data = json.loads(line)
                metadata["open_ports"].append({
                    "ip": data.get("ip"),
                    "port": data.get("port"),
                    "protocol": data.get("protocol", "tcp"),
                    "timestamp": data.get("timestamp")
                })
                metadata["hosts"].append({
                    "ip": data.get("ip"),
                    "timestamp": data.get("timestamp")
                })
    except (json.JSONDecodeError, KeyError) as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


class ZmapTool(BaseTool):
    """Run zmap scans and parse the results."""

    args_model = ZmapArgs

    def build_command(self, args: ZmapArgs) -> List[str]:
        """Build zmap command arguments.
        
        Args:
            args: Validated ZmapArgs
            
        Returns:
            List of command arguments for zmap
        """
        cmd = ["zmap"]
        
        # Add probe module
        cmd.extend(["-M", args.probe_module.value])
        
        # Add ports
        if args.ports:
            cmd.extend(["-p", args.ports])
        
        # Add rate limit
        cmd.extend(["-r", args.rate.value])
        
        # Add output fields for JSON/CSV
        if args.output_format == OutputFormat.JSON:
            cmd.extend(["-O", "json"])
        elif args.output_format == OutputFormat.CSV:
            cmd.extend(["-O", "csv"])
        
        # Add retries
        cmd.extend(["--max-retries", str(args.max_retries)])
        
        # Add interface if specified
        if args.interface:
            cmd.extend(["-i", args.interface])
        
        # Add source IP if specified
        if args.source_ip:
            cmd.extend(["-S", args.source_ip])
        
        # Add source port if specified
        if args.source_port:
            cmd.extend(["-s", args.source_port])
        
        # Add gateway MAC if specified
        if args.gateway_mac:
            cmd.extend(["-G", args.gateway_mac])
        
        # Add gateway IP if specified
        if args.gateway_ip:
            cmd.extend(["-g", args.gateway_ip])
        
        # Add blacklist file if specified
        if args.blacklist_file:
            cmd.extend(["-b", args.blacklist_file])
        
        # Add whitelist file if specified
        if args.whitelist_file:
            cmd.extend(["-w", args.whitelist_file])
        
        # Add verbosity
        cmd.extend(["-v", str(args.verbosity)])
        
        # Add quiet option
        if args.quiet:
            cmd.append("-q")
        
        # Add target/subnet (usually last)
        cmd.append(args.target)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ZmapArgs,
    ) -> Dict[str, Any]:
        """Parse zmap output into structured metadata."""
        if args.output_format == OutputFormat.JSON and stdout:
            return parse_zmap_json(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: ZmapArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create zmap artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            ext = "json" if args.output_format == OutputFormat.JSON else "txt"
            artifact_path = f"artifacts/zmap_{ts}.{ext}"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: ZmapArgs) -> ToolResult:
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
        tool_id="information_gathering.network_discovery.zmap",
        display_name="ZMap",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="fast_port_scanning",
                description="Scan one port across very large IPv4 ranges at line rate; returns responsive IPs; prefer for internet-scale single-port surveys, not for routine multi-port scans",
                output_indicators=["open", "IP"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp", "icmp"],
        execution_priority=8,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
