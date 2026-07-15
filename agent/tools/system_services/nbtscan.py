"""NBTScan tool for NetBIOS over TCP/IP enumeration and Windows machine discovery."""

from __future__ import annotations

import os
import subprocess
import time
import re
from typing import List, Optional, Dict, Any

from pydantic import Field

from ..base_tool import BaseTool
from ..schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120

class NBTScanArgs(BaseToolArgs):
    """Arguments for the NBTScan tool."""

    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    use_local_port: bool = Field(
        default=False,
        description="Use local port 137 for scans (-r)",
    )
    separator: Optional[str] = Field(
        default=None,
        description="Output separator for parsing (-s)",
    )

def parse_nbtscan_output(output_text: str) -> Dict[str, Any]:
    """Parse NBTScan output into structured metadata."""
    metadata: Dict[str, Any] = {
        "hosts_found": 0,
        "services_discovered": [],
        "netbios_names": [],
        "mac_addresses": [],
        "workgroups": [],
        "domains": [],
        "errors": [],
        "warnings": []
    }
    
    try:
        lines = output_text.strip().split("\n")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse host information
            host_match = re.search(
                r"(\d+\.\d+\.\d+\.\d+)\s+(\S+)\s+(\w+)\s+([0-9A-Fa-f:]+)",
                line,
            )
            if host_match:
                ip = host_match.group(1)
                netbios_name = host_match.group(2)
                service_type = host_match.group(3)
                mac_address = host_match.group(4)
                
                metadata["hosts_found"] += 1
                metadata["netbios_names"].append(netbios_name)
                metadata["mac_addresses"].append(mac_address)
                
                service_info = {
                    "ip": ip,
                    "name": netbios_name,
                    "service_type": service_type,
                    "mac": mac_address
                }
                metadata["services_discovered"].append(service_info)
                
                # Categorize by service type
                if service_type in ["00", "20"]:
                    metadata["workgroups"].append(netbios_name)
                elif service_type in ["1C", "1D", "1E"]:
                    metadata["domains"].append(netbios_name)
            
            # Parse error messages
            if "error" in line.lower() or "failed" in line.lower():
                metadata["errors"].append(line)
            
            # Parse warning messages
            if "warning" in line.lower():
                metadata["warnings"].append(line)
    
    except Exception as e:
        metadata["errors"].append(f"Parsing error: {str(e)}")
    
    return metadata

class NBTScanTool(BaseTool):
    """NBTScan tool for NetBIOS over TCP/IP enumeration and Windows machine discovery."""
    
    args_model = NBTScanArgs

    def build_command(self, args: NBTScanArgs) -> List[str]:
        cmd = ["nbtscan"]
        if args.verbose:
            cmd.append("-v")
        if args.use_local_port:
            cmd.append("-r")
        if args.separator:
            cmd.extend(["-s", args.separator])
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NBTScanArgs,
    ) -> Dict[str, Any]:
        metadata = parse_nbtscan_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: NBTScanArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/nbtscan_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: NBTScanArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
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
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="nbtscan command not found. Ensure nbtscan is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(
            proc.stdout, args=args, timestamp=int(start), stderr=proc.stderr
        )

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
from ..enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="system_services.nbtscan",
        display_name="nbtscan",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="netbios_enumeration",
                description="Enumerates NetBIOS names and MAC addresses.",
                output_indicators=["netbios", "mac", "workgroup"],
            ),
        ],
        required_services=["netbios"],
        target_protocols=["udp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
