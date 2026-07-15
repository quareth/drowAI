"""Deprecated: PHP Meterpreter relies on Metasploit tooling."""

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
    "PHP Meterpreter relies on Metasploit (msfvenom) and is deprecated here.",
    DeprecationWarning,
    stacklevel=2,
)

class PHPMeterpreterType(str, Enum):
    """Supported PHP Meterpreter types."""

    REVERSE_SHELL = "reverse_shell"
    BIND_SHELL = "bind_shell"
    WEB_SHELL = "web_shell"
    UPLOAD = "upload"


class PHPMeterpreterArgs(BaseToolArgs):
    """Arguments for the PHP Meterpreter tool."""

    type: PHPMeterpreterType = Field(
        PHPMeterpreterType.WEB_SHELL,
        description="Type of PHP backdoor to create",
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
    output_file: str = Field(
        "backdoor.php",
        description="Output file for the generated backdoor",
    )
    password: Optional[str] = Field(
        None,
        description="Password for web shell authentication",
    )
    obfuscate: bool = Field(
        False,
        description="Obfuscate the generated code",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_php_meterpreter_output(output_text: str) -> Dict[str, Any]:
    """Parse PHP Meterpreter output into structured metadata."""
    metadata: Dict[str, Any] = {
        "backdoor_created": False,
        "file_size": 0,
        "obfuscated": False,
        "type": "unknown",
        "errors": [],
    }
    
    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Parse success information
        if "created" in line.lower() or "generated" in line.lower():
            metadata["backdoor_created"] = True
        # Parse file size
        elif "size" in line.lower() and "bytes" in line.lower():
            try:
                import re
                match = re.search(r'(\d+)', line)
                if match:
                    metadata["file_size"] = int(match.group(1))
            except Exception:
                pass
        # Parse obfuscation status
        elif "obfuscated" in line.lower():
            metadata["obfuscated"] = True
        # Parse type information
        elif "type:" in line.lower():
            metadata["type"] = line.split(":")[-1].strip()
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class PHPMeterpreterTool(BaseTool):
    """PHP Meterpreter tool for creating PHP web backdoors."""
    
    args_model = PHPMeterpreterArgs
    
    def run(self, args: PHPMeterpreterArgs) -> ToolResult:
        # Build command array
        cmd = ["php-meterpreter"]
        
        # Add type
        cmd.extend(["--type", args.type.value])
        
        # Add local host if provided
        if args.lhost:
            cmd.extend(["--lhost", args.lhost])
        
        # Add local port
        cmd.extend(["--lport", str(args.lport)])
        
        # Add remote port
        cmd.extend(["--rport", str(args.rport)])
        
        # Add output file
        cmd.extend(["--output", args.output_file])
        
        # Add password if provided
        if args.password:
            cmd.extend(["--password", args.password])
        
        # Add obfuscation flag
        if args.obfuscate:
            cmd.append("--obfuscate")
        
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
        metadata = parse_php_meterpreter_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/php_meterpreter_{timestamp}.txt"
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
