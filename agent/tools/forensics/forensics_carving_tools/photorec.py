"""
PhotoRec Tool Implementation (Deprecated)

PhotoRec is a file data recovery software designed to recover lost files including video,
documents and archives from hard disks, CD-ROMs, and lost pictures from digital camera memory.

Deprecated: PhotoRec is interactive/menu-driven and not suitable for LLM automation.
Use foremost or scalpel for CLI file carving.
"""

import subprocess
import json
import re
from typing import Optional, Dict, Any, List
from pydantic import Field
from enum import Enum

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult


class FileType(str, Enum):
    """File types that PhotoRec can recover"""
    IMAGES = "images"
    VIDEOS = "videos"
    AUDIO = "audio"
    DOCUMENTS = "documents"
    ARCHIVES = "archives"
    DATABASES = "databases"
    EXECUTABLES = "executables"
    ALL = "all"


class RecoveryMode(str, Enum):
    """PhotoRec recovery modes"""
    QUICK = "quick"
    THOROUGH = "thorough"
    AGGRESSIVE = "aggressive"


class OutputFormat(str, Enum):
    """Output format options"""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class PhotorecArgs(BaseToolArgs):
    """Arguments for PhotoRec tool"""
    
    input_file: str = Field(
        description="Input file or device to recover files from",
        examples=["/dev/sda1", "/path/to/image.dd", "/path/to/file.bin"]
    )
    
    output_directory: str = Field(
        description="Directory to save recovered files",
        examples=["/tmp/recovered", "./recovered_files"]
    )
    
    file_types: Optional[List[FileType]] = Field(
        default=None,
        description="Specific file types to recover",
        examples=[["images", "videos"], ["documents", "archives"]]
    )
    
    recovery_mode: RecoveryMode = Field(
        default=RecoveryMode.THOROUGH,
        description="Recovery mode to use"
    )
    
    start_offset: Optional[int] = Field(
        default=None,
        description="Start offset in bytes for recovery",
        examples=[0, 512, 4096]
    )
    
    end_offset: Optional[int] = Field(
        default=None,
        description="End offset in bytes for recovery",
        examples=[1024, 1048576, -1]
    )
    
    min_file_size: Optional[int] = Field(
        default=None,
        description="Minimum file size in bytes to recover",
        examples=[1024, 4096, 8192]
    )
    
    max_file_size: Optional[int] = Field(
        default=None,
        description="Maximum file size in bytes to recover",
        examples=[1048576, 10485760, 1073741824]
    )
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for results"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    debug: bool = Field(
        default=False,
        description="Enable debug output"
    )
    
    quiet: bool = Field(
        default=False,
        description="Suppress output messages"
    )
    
    recover_deleted: bool = Field(
        default=True,
        description="Attempt to recover deleted files"
    )
    
    recover_fragmented: bool = Field(
        default=True,
        description="Attempt to recover fragmented files"
    )
    
    timeout: int = Field(
        default=3600,
        description="Timeout in seconds for the recovery process",
        examples=[300, 1800, 3600]
    )


def parse_photorec_output(output: str, error: str) -> Dict[str, Any]:
    """
    Parse PhotoRec output and extract meaningful information
    
    Args:
        output: Standard output from PhotoRec
        error: Standard error from PhotoRec
    
    Returns:
        Dictionary containing parsed information
    """
    result = {
        "files_recovered": {},
        "total_files": 0,
        "total_size_bytes": 0,
        "errors": [],
        "error_count": 0,
        "performance": {
            "processing_time": None,
            "bytes_processed": 0,
            "recovery_rate": 0.0
        },
        "file_types_found": [],
        "recovery_summary": {}
    }
    
    # Extract file recovery information
    file_pattern = r"File\s+(\d+)\s*-\s*(\d+)\s*bytes\s*-\s*(.+)"
    file_matches = re.findall(file_pattern, output, re.IGNORECASE)
    
    for match in file_matches:
        file_num = int(match[0])
        file_size = int(match[1])
        file_name = match[2].strip()
        
        # Determine file type from extension
        file_ext = file_name.split('.')[-1].lower() if '.' in file_name else 'unknown'
        
        if file_ext not in result["files_recovered"]:
            result["files_recovered"][file_ext] = []
        
        result["files_recovered"][file_ext].append({
            "file_number": file_num,
            "size_bytes": file_size,
            "filename": file_name
        })
        
        result["total_files"] += 1
        result["total_size_bytes"] += file_size
    
    # Extract performance information
    time_pattern = r"Elapsed\s+time\s*:\s*(\d+):(\d+):(\d+)"
    time_match = re.search(time_pattern, output)
    if time_match:
        hours, minutes, seconds = map(int, time_match.groups())
        result["performance"]["processing_time"] = hours * 3600 + minutes * 60 + seconds
    
    # Extract bytes processed
    bytes_pattern = r"(\d+)\s+bytes\s+processed"
    bytes_match = re.search(bytes_pattern, output)
    if bytes_match:
        result["performance"]["bytes_processed"] = int(bytes_match.group(1))
    
    # Calculate recovery rate
    if result["performance"]["bytes_processed"] > 0:
        result["performance"]["recovery_rate"] = (
            result["total_size_bytes"] / result["performance"]["bytes_processed"]
        ) * 100
    
    # Extract errors
    error_pattern = r"Error\s*:\s*(.+)"
    error_matches = re.findall(error_pattern, output, re.IGNORECASE)
    result["errors"] = error_matches
    result["error_count"] = len(error_matches)
    
    # Extract file types found
    result["file_types_found"] = list(result["files_recovered"].keys())
    
    # Create recovery summary
    for file_type, files in result["files_recovered"].items():
        total_size = sum(f["size_bytes"] for f in files)
        result["recovery_summary"][file_type] = {
            "count": len(files),
            "total_size_bytes": total_size
        }
    
    return result


class PhotorecTool(BaseTool):
    """PhotoRec file recovery tool"""

    args_model = PhotorecArgs
    
    name: str = "photorec"
    description: str = "Recover lost files from disk images and devices using PhotoRec"
    
    def run(self, args: PhotorecArgs) -> ToolResult:
        """
        Run PhotoRec file recovery
        
        Args:
            args: PhotoRec arguments
            
        Returns:
            ToolResult with recovery information
        """
        try:
            # Build command
            cmd = ["photorec"]
            
            # Add input file
            cmd.extend(["/d", args.output_directory])
            cmd.append(args.input_file)
            
            # Add file type filters if specified
            if args.file_types:
                for file_type in args.file_types:
                    if file_type != FileType.ALL:
                        cmd.extend(["/ext", file_type])
            
            # Add recovery mode
            if args.recovery_mode == RecoveryMode.QUICK:
                cmd.append("/quick")
            elif args.recovery_mode == RecoveryMode.AGGRESSIVE:
                cmd.append("/aggressive")
            
            # Add offset parameters
            if args.start_offset is not None:
                cmd.extend(["/start", str(args.start_offset)])
            
            if args.end_offset is not None:
                cmd.extend(["/end", str(args.end_offset)])
            
            # Add file size limits
            if args.min_file_size is not None:
                cmd.extend(["/min", str(args.min_file_size)])
            
            if args.max_file_size is not None:
                cmd.extend(["/max", str(args.max_file_size)])
            
            # Add output format
            if args.output_format == OutputFormat.JSON:
                cmd.append("/json")
            elif args.output_format == OutputFormat.XML:
                cmd.append("/xml")
            
            # Add verbosity options
            if args.verbose:
                cmd.append("/verbose")
            
            if args.debug:
                cmd.append("/debug")
            
            if args.quiet:
                cmd.append("/quiet")
            
            # Add recovery options
            if not args.recover_deleted:
                cmd.append("/nodeleted")
            
            if not args.recover_fragmented:
                cmd.append("/nofragmented")
            
            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout
            )
            
            # Parse output
            parsed_output = parse_photorec_output(result.stdout, result.stderr)
            
            # Save significant output to artifacts
            if parsed_output["total_files"] > 0:
                artifact_content = {
                    "command": " ".join(cmd),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "parsed_results": parsed_output
                }
                
                artifact_path = self.save_artifact(
                    f"photorec_recovery_{args.input_file.replace('/', '_')}.json",
                    json.dumps(artifact_content, indent=2)
                )
                parsed_output["artifact_path"] = artifact_path
            
            return ToolResult(
                success=result.returncode == 0,
                output=parsed_output,
                error=result.stderr if result.returncode != 0 else None
            )
            
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output={"error": "PhotoRec recovery timed out"},
                error=f"PhotoRec recovery timed out after {args.timeout} seconds"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"error": str(e)},
                error=f"PhotoRec recovery failed: {str(e)}"
            ) 
