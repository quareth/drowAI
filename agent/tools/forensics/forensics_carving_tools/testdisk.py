"""
TestDisk Tool Implementation (Deprecated)

TestDisk is a powerful free data recovery software designed to help recover lost partitions
and/or make non-booting disks bootable again by fixing partition tables.

Deprecated: TestDisk is interactive/menu-driven and not suitable for LLM automation.
Use Sleuth Kit CLI tools for partition analysis.
"""

import subprocess
import json
import re
from typing import Optional, Dict, Any
from pydantic import Field
from enum import Enum

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult


class TestDiskMode(str, Enum):
    """TestDisk operation modes"""
    CREATE = "create"
    ANALYZE = "analyze"
    ADVANCED = "advanced"
    GEOMETRY = "geometry"
    OPTIONS = "options"
    IMAGE_CREATE = "image_create"
    IMAGE_USE = "image_use"
    QUIT = "quit"


class PartitionType(str, Enum):
    """Partition types that TestDisk can handle"""
    NONE = "none"
    PRIMARY = "primary"
    EXTENDED = "extended"
    LOGICAL = "logical"
    ALL = "all"


class FileSystemType(str, Enum):
    """File system types"""
    FAT12 = "fat12"
    FAT16 = "fat16"
    FAT32 = "fat32"
    NTFS = "ntfs"
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    HFS = "hfs"
    HFS_PLUS = "hfs_plus"
    UFS = "ufs"
    ALL = "all"


class OutputFormat(str, Enum):
    """Output format options"""
    TEXT = "text"
    JSON = "json"
    XML = "xml"
    HTML = "html"


class TestDiskArgs(BaseToolArgs):
    """Arguments for TestDisk tool"""
    
    input_file: str = Field(
        description="Input file or device to analyze",
        examples=["/dev/sda", "/path/to/image.dd", "/path/to/file.bin"]
    )
    
    mode: TestDiskMode = Field(
        default=TestDiskMode.ANALYZE,
        description="TestDisk operation mode"
    )
    
    partition_type: Optional[PartitionType] = Field(
        default=None,
        description="Type of partition to search for",
        examples=["primary", "extended", "logical"]
    )
    
    file_system_type: Optional[FileSystemType] = Field(
        default=None,
        description="File system type to search for",
        examples=["ntfs", "fat32", "ext4"]
    )
    
    output_directory: Optional[str] = Field(
        default=None,
        description="Directory to save output files",
        examples=["/tmp/testdisk_output", "./testdisk_results"]
    )
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for results"
    )
    
    start_sector: Optional[int] = Field(
        default=None,
        description="Start sector for analysis",
        examples=[0, 63, 2048]
    )
    
    end_sector: Optional[int] = Field(
        default=None,
        description="End sector for analysis",
        examples=[1024, 1048576, -1]
    )
    
    sector_size: Optional[int] = Field(
        default=None,
        description="Sector size in bytes",
        examples=[512, 4096, 8192]
    )
    
    geometry_mode: bool = Field(
        default=False,
        description="Use geometry mode for analysis"
    )
    
    advanced_mode: bool = Field(
        default=False,
        description="Use advanced mode for analysis"
    )
    
    create_image: bool = Field(
        default=False,
        description="Create disk image during analysis"
    )
    
    image_file: Optional[str] = Field(
        default=None,
        description="Path to disk image file",
        examples=["/path/to/image.dd", "./disk_image.img"]
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
    
    timeout: int = Field(
        default=3600,
        description="Timeout in seconds for the analysis process",
        examples=[300, 1800, 3600]
    )


def parse_testdisk_output(output: str, error: str) -> Dict[str, Any]:
    """
    Parse TestDisk output and extract meaningful information
    
    Args:
        output: Standard output from TestDisk
        error: Standard error from TestDisk
    
    Returns:
        Dictionary containing parsed information
    """
    result = {
        "partitions_found": [],
        "partition_table": {},
        "file_systems": [],
        "geometry": {},
        "errors": [],
        "error_count": 0,
        "analysis_summary": {
            "total_partitions": 0,
            "recoverable_partitions": 0,
            "deleted_partitions": 0
        },
        "performance": {
            "processing_time": None,
            "sectors_analyzed": 0
        }
    }
    
    # Extract partition information
    partition_pattern = r"Partition\s+(\d+)\s*:\s*(\d+)\s*-\s*(\d+)\s*\((\d+)\s*sectors\)\s*([^\s]+)"
    partition_matches = re.findall(partition_pattern, output, re.IGNORECASE)
    
    for match in partition_matches:
        part_num = int(match[0])
        start_sector = int(match[1])
        end_sector = int(match[2])
        sector_count = int(match[3])
        fs_type = match[4].strip()
        
        partition_info = {
            "partition_number": part_num,
            "start_sector": start_sector,
            "end_sector": end_sector,
            "sector_count": sector_count,
            "file_system": fs_type,
            "size_bytes": sector_count * 512  # Assuming 512-byte sectors
        }
        
        result["partitions_found"].append(partition_info)
        result["analysis_summary"]["total_partitions"] += 1
        
        if "deleted" in fs_type.lower():
            result["analysis_summary"]["deleted_partitions"] += 1
        else:
            result["analysis_summary"]["recoverable_partitions"] += 1
    
    # Extract file system information
    fs_pattern = r"File\s+system\s*:\s*([^\n]+)"
    fs_matches = re.findall(fs_pattern, output, re.IGNORECASE)
    result["file_systems"] = [fs.strip() for fs in fs_matches]
    
    # Extract geometry information
    geometry_pattern = r"Geometry\s*:\s*(\d+)\s+heads,\s*(\d+)\s+cylinders,\s*(\d+)\s+sectors"
    geometry_match = re.search(geometry_pattern, output, re.IGNORECASE)
    if geometry_match:
        heads, cylinders, sectors = map(int, geometry_match.groups())
        result["geometry"] = {
            "heads": heads,
            "cylinders": cylinders,
            "sectors": sectors,
            "total_sectors": heads * cylinders * sectors
        }
    
    # Extract performance information
    time_pattern = r"Analysis\s+completed\s+in\s+(\d+):(\d+):(\d+)"
    time_match = re.search(time_pattern, output)
    if time_match:
        hours, minutes, seconds = map(int, time_match.groups())
        result["performance"]["processing_time"] = hours * 3600 + minutes * 60 + seconds
    
    # Extract sectors analyzed
    sectors_pattern = r"(\d+)\s+sectors\s+analyzed"
    sectors_match = re.search(sectors_pattern, output)
    if sectors_match:
        result["performance"]["sectors_analyzed"] = int(sectors_match.group(1))
    
    # Extract errors
    error_pattern = r"Error\s*:\s*(.+)"
    error_matches = re.findall(error_pattern, output, re.IGNORECASE)
    result["errors"] = error_matches
    result["error_count"] = len(error_matches)
    
    # Create partition table summary
    for partition in result["partitions_found"]:
        fs_type = partition["file_system"]
        if fs_type not in result["partition_table"]:
            result["partition_table"][fs_type] = []
        result["partition_table"][fs_type].append(partition)
    
    return result


class TestDiskTool(BaseTool):
    """TestDisk partition recovery tool"""

    args_model = TestDiskArgs
    
    name: str = "testdisk"
    description: str = "Analyze and recover lost partitions using TestDisk"
    
    def run(self, args: TestDiskArgs) -> ToolResult:
        """
        Run TestDisk partition analysis
        
        Args:
            args: TestDisk arguments
            
        Returns:
            ToolResult with analysis information
        """
        try:
            # Build command
            cmd = ["testdisk"]
            
            # Add input file
            cmd.append(args.input_file)
            
            # Add mode
            if args.mode == TestDiskMode.CREATE:
                cmd.append("/create")
            elif args.mode == TestDiskMode.ADVANCED:
                cmd.append("/advanced")
            elif args.mode == TestDiskMode.GEOMETRY:
                cmd.append("/geometry")
            elif args.mode == TestDiskMode.OPTIONS:
                cmd.append("/options")
            elif args.mode == TestDiskMode.IMAGE_CREATE:
                cmd.append("/image_create")
            elif args.mode == TestDiskMode.IMAGE_USE:
                cmd.append("/image_use")
            
            # Add partition type filter
            if args.partition_type:
                cmd.extend(["/partition", args.partition_type])
            
            # Add file system type filter
            if args.file_system_type:
                cmd.extend(["/filesystem", args.file_system_type])
            
            # Add output directory
            if args.output_directory:
                cmd.extend(["/output", args.output_directory])
            
            # Add output format
            if args.output_format == OutputFormat.JSON:
                cmd.append("/json")
            elif args.output_format == OutputFormat.XML:
                cmd.append("/xml")
            elif args.output_format == OutputFormat.HTML:
                cmd.append("/html")
            
            # Add sector parameters
            if args.start_sector is not None:
                cmd.extend(["/start", str(args.start_sector)])
            
            if args.end_sector is not None:
                cmd.extend(["/end", str(args.end_sector)])
            
            if args.sector_size is not None:
                cmd.extend(["/sector", str(args.sector_size)])
            
            # Add mode flags
            if args.geometry_mode:
                cmd.append("/geometry_mode")
            
            if args.advanced_mode:
                cmd.append("/advanced_mode")
            
            # Add image options
            if args.create_image:
                cmd.append("/create_image")
            
            if args.image_file:
                cmd.extend(["/image", args.image_file])
            
            # Add verbosity options
            if args.verbose:
                cmd.append("/verbose")
            
            if args.debug:
                cmd.append("/debug")
            
            if args.quiet:
                cmd.append("/quiet")
            
            # Execute command
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout
            )
            
            # Parse output
            parsed_output = parse_testdisk_output(result.stdout, result.stderr)
            
            # Save significant output to artifacts
            if parsed_output["partitions_found"] or parsed_output["analysis_summary"]["total_partitions"] > 0:
                artifact_content = {
                    "command": " ".join(cmd),
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "parsed_results": parsed_output
                }
                
                artifact_path = self.save_artifact(
                    f"testdisk_analysis_{args.input_file.replace('/', '_')}.json",
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
                output={"error": "TestDisk analysis timed out"},
                error=f"TestDisk analysis timed out after {args.timeout} seconds"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output={"error": str(e)},
                error=f"TestDisk analysis failed: {str(e)}"
            ) 
