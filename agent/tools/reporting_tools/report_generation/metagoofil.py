"""Metagoofil tool for metadata extraction and analysis from documents."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class MetagoofilMode(str, Enum):
    """Supported Metagoofil modes."""

    EXTRACT = "extract"
    ANALYZE = "analyze"
    SEARCH = "search"
    EXPORT = "export"


class MetagoofilArgs(BaseToolArgs):
    """Arguments for the Metagoofil tool."""

    mode: MetagoofilMode = Field(
        MetagoofilMode.EXTRACT,
        description="Metagoofil mode to use",
    )
    file_types: List[str] = Field(
        default_factory=lambda: ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"],
        description="File types to search for and extract metadata",
    )
    output_directory: Optional[str] = Field(
        None,
        description="Output directory for extracted metadata",
    )
    search_depth: int = Field(
        3,
        description="Search depth for recursive file discovery",
    )
    max_files: int = Field(
        100,
        description="Maximum number of files to process",
    )
    metadata_types: List[str] = Field(
        default_factory=lambda: ["author", "creator", "company", "email", "phone"],
        description="Types of metadata to extract",
    )
    export_format: Literal["json", "csv", "xml", "html"] = Field(
        "json",
        description="Export format for extracted metadata",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    timeout: int = Field(
        60,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_metagoofil_output(output_text: str) -> Dict[str, Any]:
    """Parse Metagoofil output into structured metadata."""
    metadata: Dict[str, Any] = {
        "extraction_successful": False,
        "files_processed": 0,
        "metadata_found": [],
        "file_types_found": [],
        "total_metadata_items": 0,
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
        # Parse files processed
        elif "files" in line.lower() and "processed" in line.lower():
            try:
                import re
                match = re.search(r'(\d+)', line)
                if match:
                    metadata["files_processed"] = int(match.group(1))
            except Exception:
                pass
        # Parse metadata found
        elif "metadata" in line.lower() and ":" in line:
            metadata_item = line.split(":")[-1].strip()
            metadata["metadata_found"].append(metadata_item)
            metadata["total_metadata_items"] += 1
        # Parse file types found
        elif "file type" in line.lower() and ":" in line:
            file_type = line.split(":")[-1].strip()
            metadata["file_types_found"].append(file_type)
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class MetagoofilTool(BaseTool):
    """Metagoofil tool for metadata extraction and analysis from documents."""
    
    args_model = MetagoofilArgs
    
    def run(self, args: MetagoofilArgs) -> ToolResult:
        # Build command array
        cmd = ["metagoofil"]
        
        # Add mode
        cmd.extend(["--mode", args.mode.value])
        
        # Add file types
        for file_type in args.file_types:
            cmd.extend(["--type", file_type])
        
        # Add output directory if provided
        if args.output_directory:
            cmd.extend(["--output", args.output_directory])
        
        # Add search depth
        cmd.extend(["--depth", str(args.search_depth)])
        
        # Add max files
        cmd.extend(["--max-files", str(args.max_files)])
        
        # Add metadata types
        for metadata_type in args.metadata_types:
            cmd.extend(["--metadata", metadata_type])
        
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
        metadata = parse_metagoofil_output(proc.stdout)
        
        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/metagoofil_{timestamp}.txt"
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
