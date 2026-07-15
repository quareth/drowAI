"""Scalpel - File carving tool for recovering deleted files from disk images."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 3600
ARTIFACT_OUTPUT_MIN_CHARS = 200


class ScalpelArgs(BaseToolArgs):
    """Arguments for the Scalpel tool."""

    output_directory: str = Field(
        ...,
        description="Output directory for carved files (-o)",
    )
    config_file: Optional[str] = Field(
        None,
        description="Configuration file path (-c)",
    )
    block_size: Optional[int] = Field(
        None,
        description="Block size for processing (-b)",
        ge=512,
        le=1048576,
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output (-v)",
    )
    quiet: bool = Field(
        False,
        description="Suppress output messages (-q)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds for the operation",
        ge=300,
        le=7200,
    )


def parse_scalpel_output(output_text: str) -> Dict[str, Any]:
    """Parse Scalpel output into structured metadata."""
    metadata: Dict[str, Any] = {
        "files_carved": 0,
        "bytes_processed": 0,
        "errors": [],
    }

    for line in output_text.splitlines():
        files_match = re.search(r"(\d+)\s+files?.*carved", line, re.IGNORECASE)
        if files_match:
            metadata["files_carved"] = int(files_match.group(1))
        bytes_match = re.search(r"(\d+)\s+bytes?.*processed", line, re.IGNORECASE)
        if bytes_match:
            metadata["bytes_processed"] = int(bytes_match.group(1))
        if "error" in line.lower():
            metadata["errors"].append(line.strip())

    return metadata


class ScalpelTool(BaseTool):
    """Run Scalpel file carving and parse the output."""

    args_model = ScalpelArgs

    def build_command(self, args: ScalpelArgs) -> List[str]:
        cmd: List[str] = ["scalpel"]
        if args.config_file:
            cmd.extend(["-c", args.config_file])
        if args.block_size:
            cmd.extend(["-b", str(args.block_size)])
        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")
        cmd.extend(["-o", args.output_directory, args.target])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ScalpelArgs,
    ) -> Dict[str, Any]:
        metadata = parse_scalpel_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ScalpelArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/scalpel_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ScalpelArgs) -> ToolResult:
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
        tool_id="forensics.forensics_carving_tools.scalpel",
        display_name="Scalpel",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="file_carving",
                description="Carve files from disk images using Scalpel with a configuration-driven signature set; returns file count and byte totals; use when foremost defaults are too narrow.",
                output_indicators=["files_carved", "bytes_processed"],
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
