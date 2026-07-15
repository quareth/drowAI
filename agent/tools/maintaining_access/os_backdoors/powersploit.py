"""Deprecated: PowerSploit requires Windows PowerShell."""

from __future__ import annotations

import os
import subprocess
import time
import warnings
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

warnings.warn(
    "PowerSploit is Windows-only and not supported in this environment.",
    DeprecationWarning,
    stacklevel=2,
)

class PowerSploitModule(str, Enum):
    """Supported PowerSploit modules."""

    INVOKE_SHELLCODE = "Invoke-Shellcode"
    INVOKE_REVERSE_SHELL = "Invoke-ReverseShell"
    INVOKE_BIND_SHELL = "Invoke-BindShell"
    INVOKE_MIMIKATZ = "Invoke-Mimikatz"
    GET_PASSWORDS = "Get-Passwords"
    GET_KEYS = "Get-Keys"


class PowerSploitArgs(BaseToolArgs):
    """Arguments for the PowerSploit tool."""

    module: PowerSploitModule = Field(
        PowerSploitModule.INVOKE_REVERSE_SHELL,
        description="PowerSploit module to use",
    )
    lhost: Optional[str] = Field(
        None,
        description="Local host for reverse shell connections",
    )
    lport: int = Field(
        4444,
        description="Local port for reverse shell connections (default: 4444)",
    )
    rport: int = Field(
        4444,
        description="Remote port for bind shell connections (default: 4444)",
    )
    shellcode: Optional[str] = Field(
        None,
        description="Shellcode to inject (hex format)",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file for generated payload",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_powersploit_output(output_text: str) -> Dict[str, Any]:
    """Parse PowerSploit output into structured metadata."""
    metadata: Dict[str, Any] = {
        "module_executed": False,
        "payload_generated": False,
        "credentials_found": [],
        "shell_established": False,
        "errors": [],
    }
    
    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Parse module execution
        if "executed" in line.lower() or "success" in line.lower():
            metadata["module_executed"] = True
        # Parse payload generation
        elif "payload" in line.lower() and "generated" in line.lower():
            metadata["payload_generated"] = True
        # Parse credentials
        elif "password" in line.lower() or "credential" in line.lower():
            metadata["credentials_found"].append(line)
        # Parse shell establishment
        elif "shell" in line.lower() and "established" in line.lower():
            metadata["shell_established"] = True
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class PowerSploitTool(BaseTool):
    """PowerSploit tool for PowerShell exploitation and backdoor creation."""
    
    args_model = PowerSploitArgs
    
    def run(self, args: PowerSploitArgs) -> ToolResult:
        # Build command array
        cmd = ["powersploit"]
        
        # Add module
        cmd.extend(["--module", args.module.value])
        
        # Add local host if provided
        if args.lhost:
            cmd.extend(["--lhost", args.lhost])
        
        # Add local port
        cmd.extend(["--lport", str(args.lport)])
        
        # Add remote port
        cmd.extend(["--rport", str(args.rport)])
        
        # Add shellcode if provided
        if args.shellcode:
            cmd.extend(["--shellcode", args.shellcode])
        
        # Add output file if provided
        if args.output_file:
            cmd.extend(["--output", args.output_file])
        
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
        metadata = parse_powersploit_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/powersploit_{timestamp}.txt"
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
