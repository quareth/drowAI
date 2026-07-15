"""Dumpzilla tool for Firefox/Thunderbird data extraction and analysis."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class DumpzillaMode(str, Enum):
    """Supported Dumpzilla modes."""

    EXTRACT = "extract"
    ANALYZE = "analyze"
    EXPORT = "export"
    SEARCH = "search"


class DumpzillaArgs(BaseToolArgs):
    """Arguments for the Dumpzilla tool."""

    mode: DumpzillaMode = Field(
        DumpzillaMode.EXTRACT,
        description="Dumpzilla mode to use",
    )
    profile_path: Optional[str] = Field(
        None,
        description="Path to Firefox/Thunderbird profile directory",
    )
    output_directory: Optional[str] = Field(
        None,
        description="Output directory for extracted data",
    )
    data_types: List[str] = Field(
        default_factory=lambda: ["cookies", "history", "bookmarks", "passwords"],
        description="Types of data to extract",
    )
    search_term: Optional[str] = Field(
        None,
        description="Search term for data analysis",
    )
    export_format: Literal["json", "csv", "xml", "html"] = Field(
        "json",
        description="Export format for extracted data",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    timeout: int = Field(
        60,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_dumpzilla_output(output_text: str) -> Dict[str, Any]:
    """Parse Dumpzilla output into structured metadata."""
    metadata: Dict[str, Any] = {
        "extraction_successful": False,
        "data_types_found": [],
        "total_records": 0,
        "profile_path": None,
        "output_directory": None,
        "errors": [],
    }
    
    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Parse extraction success
        if "extracted" in line.lower() and "success" in line.lower():
            metadata["extraction_successful"] = True
        # Parse data types found
        elif "found" in line.lower() and ":" in line:
            data_type = line.split(":")[-1].strip()
            metadata["data_types_found"].append(data_type)
        # Parse total records
        elif "records" in line.lower() and "total" in line.lower():
            try:
                import re
                match = re.search(r'(\d+)', line)
                if match:
                    metadata["total_records"] = int(match.group(1))
            except Exception:
                pass
        # Parse profile path
        elif "profile" in line.lower() and "path" in line.lower():
            metadata["profile_path"] = line.split(":")[-1].strip()
        # Parse output directory
        elif "output" in line.lower() and "directory" in line.lower():
            metadata["output_directory"] = line.split(":")[-1].strip()
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class DumpzillaTool(BaseTool):
    """Dumpzilla tool for Firefox/Thunderbird data extraction and analysis."""
    
    args_model = DumpzillaArgs
    
    def run(self, args: DumpzillaArgs) -> ToolResult:
        # Build command array
        cmd = ["dumpzilla"]
        
        # Add mode
        cmd.extend(["--mode", args.mode.value])
        
        # Add profile path if provided
        if args.profile_path:
            cmd.extend(["--profile", args.profile_path])
        
        # Add output directory if provided
        if args.output_directory:
            cmd.extend(["--output", args.output_directory])
        
        # Add data types
        for data_type in args.data_types:
            cmd.extend(["--type", data_type])
        
        # Add search term if provided
        if args.search_term:
            cmd.extend(["--search", args.search_term])
        
        # Add export format
        cmd.extend(["--format", args.export_format])
        
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
        metadata = parse_dumpzilla_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/dumpzilla_{timestamp}.txt"
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
