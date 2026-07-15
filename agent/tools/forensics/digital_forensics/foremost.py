"""Foremost - File carving and data recovery tool for digital forensics."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 3600
ARTIFACT_OUTPUT_MIN_CHARS = 200


class FileType(str, Enum):
    """Foremost supported file types."""

    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    BMP = "bmp"
    TIFF = "tiff"
    PDF = "pdf"
    DOC = "doc"
    XLS = "xls"
    PPT = "ppt"
    ZIP = "zip"
    RAR = "rar"
    EXE = "exe"
    DLL = "dll"
    ALL = "all"


class ForemostArgs(BaseToolArgs):
    """Arguments for the Foremost tool."""

    output_directory: str = Field(
        ...,
        description="Output directory for carved files (-o)",
    )
    file_types: List[FileType] = Field(
        default_factory=lambda: [FileType.ALL],
        description="File types to carve (-t)",
    )
    config_file: Optional[str] = Field(
        None,
        description="Custom configuration file (-c)",
    )
    quick_mode: bool = Field(
        False,
        description="Enable quick mode (-Q)",
    )
    use_timestamps: bool = Field(
        False,
        description="Prefix carved files with timestamps (-T)",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output (-v)",
    )
    quiet: bool = Field(
        False,
        description="Suppress non-essential output (-q)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds for the entire operation",
        ge=300,
        le=7200,
    )


def parse_foremost_output(output_text: str) -> Dict[str, Any]:
    """Parse Foremost output into structured metadata."""
    metadata: Dict[str, Any] = {"files_carved": {}, "summary": {}}

    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "files written" in line.lower():
            match = re.search(r"(\w+):\s*(\d+)\s+files\s+written", line)
            if match:
                file_type = match.group(1)
                count = int(match.group(2))
                metadata["files_carved"][file_type] = count
        if "bytes processed" in line.lower():
            bytes_match = re.search(r"(\d+)\s+bytes\s+processed", line.lower())
            if bytes_match:
                metadata["summary"]["bytes_processed"] = int(bytes_match.group(1))

    metadata["summary"]["total_files"] = sum(metadata["files_carved"].values())
    return metadata


class ForemostTool(BaseTool):
    """Foremost - File carving and data recovery tool for digital forensics."""

    args_model = ForemostArgs

    def build_command(self, args: ForemostArgs) -> List[str]:
        cmd: List[str] = ["foremost", "-i", args.target, "-o", args.output_directory]

        if args.file_types and FileType.ALL not in args.file_types:
            file_types = ",".join(file_type.value for file_type in args.file_types)
            cmd.extend(["-t", file_types])

        if args.config_file:
            cmd.extend(["-c", args.config_file])

        if args.quick_mode:
            cmd.append("-Q")

        if args.use_timestamps:
            cmd.append("-T")

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
        args: ForemostArgs,
    ) -> Dict[str, Any]:
        metadata = parse_foremost_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ForemostArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/foremost_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ForemostArgs) -> ToolResult:
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

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))
        success = self.is_success_exit_code(proc.returncode, args)

        return ToolResult(
            success=success,
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
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="forensics.digital_forensics.foremost",
        display_name="Foremost",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="file_carving",
                description="Carve files from disk images by header and footer signatures; returns recovered files grouped by type; not for live filesystem analysis.",
                output_indicators=["files_carved", "total_files"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=15,
    )
)
