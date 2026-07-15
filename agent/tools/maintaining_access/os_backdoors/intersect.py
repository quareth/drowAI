"""Deprecated: Intersect is not a standard supported tool."""

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
    "Intersect is not a standard supported tool and is deprecated.",
    DeprecationWarning,
    stacklevel=2,
)

class IntersectMode(str, Enum):
    """Supported Intersect modes."""

    INJECT = "inject"
    HOOK = "hook"
    PATCH = "patch"
    SHELLCODE = "shellcode"


class IntersectArgs(BaseToolArgs):
    """Arguments for the Intersect tool."""

    mode: IntersectMode = Field(
        IntersectMode.INJECT,
        description="Intersect mode to use",
    )
    process_name: Optional[str] = Field(
        None,
        description="Target process name for injection",
    )
    process_id: Optional[int] = Field(
        None,
        description="Target process ID for injection",
    )
    shellcode_file: Optional[str] = Field(
        None,
        description="Path to shellcode file",
    )
    dll_file: Optional[str] = Field(
        None,
        description="Path to DLL file for injection",
    )
    hook_function: Optional[str] = Field(
        None,
        description="Function name to hook",
    )
    patch_address: Optional[str] = Field(
        None,
        description="Memory address to patch",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file for results",
    )
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_intersect_output(output_text: str) -> Dict[str, Any]:
    """Parse Intersect output into structured metadata."""
    metadata: Dict[str, Any] = {
        "operation_successful": False,
        "process_targeted": None,
        "mode_used": "unknown",
        "memory_address": None,
        "errors": [],
    }
    
    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Parse operation success
        if "success" in line.lower() and ("injected" in line.lower() or "hooked" in line.lower() or "patched" in line.lower()):
            metadata["operation_successful"] = True
        # Parse process information
        elif "process" in line.lower() and ":" in line:
            metadata["process_targeted"] = line.split(":")[-1].strip()
        # Parse mode information
        elif "mode" in line.lower() and ":" in line:
            metadata["mode_used"] = line.split(":")[-1].strip()
        # Parse memory address
        elif "address" in line.lower() and "0x" in line:
            try:
                import re
                match = re.search(r'0x[0-9a-fA-F]+', line)
                if match:
                    metadata["memory_address"] = match.group(0)
            except Exception:
                pass
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class IntersectTool(BaseTool):
    """Intersect tool for process injection and backdoor creation."""
    
    args_model = IntersectArgs
    
    def run(self, args: IntersectArgs) -> ToolResult:
        # Build command array
        cmd = ["intersect"]
        
        # Add mode
        cmd.extend(["--mode", args.mode.value])
        
        # Add process information
        if args.process_name:
            cmd.extend(["--process", args.process_name])
        if args.process_id:
            cmd.extend(["--pid", str(args.process_id)])
        
        # Add shellcode file if provided
        if args.shellcode_file:
            cmd.extend(["--shellcode", args.shellcode_file])
        
        # Add DLL file if provided
        if args.dll_file:
            cmd.extend(["--dll", args.dll_file])
        
        # Add hook function if provided
        if args.hook_function:
            cmd.extend(["--hook", args.hook_function])
        
        # Add patch address if provided
        if args.patch_address:
            cmd.extend(["--patch", args.patch_address])
        
        # Add verbose flag
        if args.verbose:
            cmd.append("--verbose")
        
        # Add output file if provided
        if args.output_file:
            cmd.extend(["--output", args.output_file])
        
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
        metadata = parse_intersect_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/intersect_{timestamp}.txt"
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
