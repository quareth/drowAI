"""
Binwalk Tool Implementation

This module provides a Python interface for Binwalk, a tool for analyzing,
reverse engineering, and extracting firmware images. Binwalk is used for
firmware analysis, malware research, and digital forensics.

Author: AI Assistant
Date: 2024
"""

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 300
ARTIFACTS_DIR = "artifacts"
MIN_ARTIFACT_BYTES = 200


class BinwalkMode(str, Enum):
    """Binwalk operation modes"""
    SCAN = "scan"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    SIGNATURE = "signature"
    ENTROPY = "entropy"
    DIFF = "diff"
    FILTER = "filter"
    DECODE = "decode"
    DISASSEMBLE = "disassemble"


class BinwalkSignatureType(str, Enum):
    """Types of signatures to scan for"""
    ALL = "all"
    COMPRESSED = "compressed"
    ENCRYPTED = "encrypted"
    EXECUTABLE = "executable"
    ARCHIVE = "archive"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class BinwalkArgs(BaseToolArgs):
    """Arguments for Binwalk tool"""

    target: str = Field(
        ...,
        description="Path to the binary or firmware image to analyze",
    )
    mode: BinwalkMode = Field(
        default=BinwalkMode.SCAN,
        description="Operation mode for Binwalk"
    )

    signature_type: Optional[BinwalkSignatureType] = Field(
        default=None,
        description="Type of signatures to scan for"
    )
    
    output_directory: Optional[str] = Field(
        default=None,
        description="Output directory for extracted files"
    )
    
    signature_file: Optional[str] = Field(
        default=None,
        description="Custom signature file to use"
    )
    
    offset: Optional[int] = Field(
        default=None,
        gt=0,
        description="Start scanning at this offset (bytes)"
    )
    
    length: Optional[int] = Field(
        default=None,
        gt=0,
        description="Scan only this many bytes"
    )
    
    block_size: Optional[int] = Field(
        default=None,
        gt=0,
        description="Block size for scanning (bytes)"
    )
    
    entropy_threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Entropy threshold for analysis (0.0-1.0)"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    quiet: bool = Field(
        default=False,
        description="Suppress output"
    )
    
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        description="Timeout in seconds for the operation",
    )
    
    json_output: bool = Field(
        default=False,
        description="Output results in JSON format"
    )




def parse_binwalk_output(output_text: str) -> Dict[str, Any]:
    """
    Parse Binwalk command output and extract structured information.
    
    Args:
        output_text: Raw output from Binwalk
        
    Returns:
        Dictionary containing parsed information
    """
    result = {
        "status": "unknown",
        "signatures": [],
        "extracted_files": [],
        "entropy_analysis": {},
        "file_info": {},
        "errors": [],
        "warnings": []
    }
    
    try:
        # Parse status
        if "scan results" in output_text.lower():
            result["status"] = "scanned"
        elif "extraction complete" in output_text.lower():
            result["status"] = "extracted"
        elif "analysis complete" in output_text.lower():
            result["status"] = "analyzed"
        elif "error" in output_text.lower():
            result["status"] = "error"
        
        # Extract signatures
        signature_matches = re.findall(r'(\d+)\s+([0-9A-Fa-f]+)\s+([^\n]+)', output_text)
        for offset, hex_data, description in signature_matches:
            result["signatures"].append({
                "offset": int(offset),
                "hex_data": hex_data,
                "description": description.strip()
            })
        
        # Extract file information
        file_info_matches = re.findall(r'File:\s+([^\n]+)', output_text)
        if file_info_matches:
            result["file_info"]["path"] = file_info_matches[0].strip()
        
        # Extract entropy analysis
        entropy_matches = re.findall(r'Entropy:\s+([0-9.]+)', output_text)
        if entropy_matches:
            result["entropy_analysis"]["value"] = float(entropy_matches[0])
        
        # Extract extracted files
        extracted_matches = re.findall(r'Extracted:\s+([^\n]+)', output_text)
        result["extracted_files"] = [match.strip() for match in extracted_matches]
        
        # Extract errors
        error_matches = re.findall(r'Error: ([^\n]+)', output_text)
        result["errors"] = error_matches
        
        # Extract warnings
        warning_matches = re.findall(r'Warning: ([^\n]+)', output_text)
        result["warnings"] = warning_matches
        
    except Exception as e:
        result["errors"].append(f"Parsing error: {str(e)}")
    
    return result


class BinwalkTool(BaseTool):
    """
    Binwalk Tool for firmware analysis and reverse engineering.
    
    This tool provides an interface to Binwalk for analyzing firmware images,
    extracting embedded files, performing entropy analysis, and identifying
    file signatures and formats.
    """
    
    args_model = BinwalkArgs

    def build_command(self, args: BinwalkArgs) -> List[str]:
        cmd = ["binwalk"]

        if args.mode == BinwalkMode.SCAN:
            cmd.append(args.target)
        elif args.mode == BinwalkMode.EXTRACT:
            cmd.extend(["-e", args.target])
            if args.output_directory:
                cmd.extend(["-C", args.output_directory])
        elif args.mode == BinwalkMode.ANALYZE:
            cmd.extend(["-A", args.target])
        elif args.mode == BinwalkMode.SIGNATURE:
            cmd.extend(["-B", args.target])
        elif args.mode == BinwalkMode.ENTROPY:
            cmd.extend(["-E", args.target])
        elif args.mode == BinwalkMode.DIFF:
            cmd.extend(["-W", args.target])
        elif args.mode == BinwalkMode.FILTER:
            cmd.extend(["-f", args.target])
        elif args.mode == BinwalkMode.DECODE:
            cmd.extend(["-D", args.target])
        elif args.mode == BinwalkMode.DISASSEMBLE:
            cmd.extend(["-d", args.target])

        if args.signature_type:
            cmd.extend(["-y", args.signature_type.value])
        if args.signature_file:
            cmd.extend(["-S", args.signature_file])
        if args.offset is not None:
            cmd.extend(["-o", str(args.offset)])
        if args.length is not None:
            cmd.extend(["-l", str(args.length)])
        if args.block_size is not None:
            cmd.extend(["-b", str(args.block_size)])
        if args.entropy_threshold is not None:
            cmd.extend(["-t", str(args.entropy_threshold)])
        if args.json_output:
            cmd.append("--json")
        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BinwalkArgs,
    ) -> Dict[str, Any]:
        _ = args
        metadata = parse_binwalk_output(stdout)
        if exit_code != 0:
            metadata["status"] = "error"
            if stderr:
                metadata.setdefault("errors", []).append(stderr.strip())
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: BinwalkArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []

        if args.output_directory and args.mode == BinwalkMode.EXTRACT:
            artifacts.append(args.output_directory)

        if stdout and len(stdout) >= MIN_ARTIFACT_BYTES:
            ts = timestamp if timestamp is not None else int(time.time())
            extension = "json" if args.json_output else "txt"
            artifact_path = os.path.join(ARTIFACTS_DIR, f"binwalk_{ts}.{extension}")
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        return artifacts

    def run(self, args: BinwalkArgs) -> ToolResult:
        cmd = self.build_command(args)
        start = time.time()

        try:
            process = subprocess.run(
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
                metadata={"status": "timeout"},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                artifacts=[],
                metadata={"status": "error"},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(process.stdout, args, timestamp=int(start))

        return ToolResult(
            success=self.is_success_exit_code(process.returncode, args),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


from agent.tools.enhanced_metadata_registry import (  # noqa: E402
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

BINWALK_EXECUTION_PRIORITY = 7
BINWALK_STEALTH_LEVEL = 4
BINWALK_ESTIMATED_RUNTIME_MINUTES = 5

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="reverse_engineering.disassemblers.binwalk",
        display_name="Binwalk",
        category=ToolCategory.REVERSE_ENGINEERING,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="firmware_analysis",
                description="Identify embedded files and signatures in firmware or binary images; returns offsets, types, and entropy; firmware-focused — not for live debugging or single-binary disassembly.",
                output_indicators=["signatures", "extracted_files"],
            ),
            ToolCapability(
                name="entropy_analysis",
                description="Assess entropy to identify compressed or encrypted regions",
                output_indicators=["entropy_analysis"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=BINWALK_EXECUTION_PRIORITY,
        parallel_compatible=True,
        stealth_level=BINWALK_STEALTH_LEVEL,
        estimated_runtime_minutes=BINWALK_ESTIMATED_RUNTIME_MINUTES,
    )
)
