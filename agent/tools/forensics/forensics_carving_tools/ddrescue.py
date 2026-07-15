"""DDRescue - Data recovery tool for copying data from damaged media."""

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


class DDRescueArgs(BaseToolArgs):
    """Arguments for the DDRescue tool."""

    output_file: str = Field(..., description="Output file to write to")
    log_file: Optional[str] = Field(None, description="Log file for recovery progress")
    retry_passes: Optional[int] = Field(
        None,
        description="Retry passes for bad sectors (-r)",
        ge=0,
        le=10,
    )
    no_scrape: bool = Field(
        False,
        description="Skip scraping phase (-n)",
    )
    reverse: bool = Field(
        False,
        description="Read in reverse direction (-R)",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output (-v)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds for the operation",
        ge=300,
        le=7200,
    )


def parse_ddrescue_output(output_text: str) -> Dict[str, Any]:
    """Parse DDRescue output into structured metadata."""
    metadata: Dict[str, Any] = {
        "bytes_copied": 0,
        "bytes_failed": 0,
        "recovery_rate": 0.0,
        "errors": [],
    }

    for line in output_text.splitlines():
        copied_match = re.search(r"(\d+)\s+bytes?.*copied", line, re.IGNORECASE)
        if copied_match:
            metadata["bytes_copied"] = int(copied_match.group(1))
        failed_match = re.search(r"(\d+)\s+bytes?.*failed", line, re.IGNORECASE)
        if failed_match:
            metadata["bytes_failed"] = int(failed_match.group(1))
        rate_match = re.search(r"(\d+\.?\d*)%", line)
        if rate_match:
            metadata["recovery_rate"] = float(rate_match.group(1))
        if "error" in line.lower():
            metadata["errors"].append(line.strip())

    return metadata


class DDRescueTool(BaseTool):
    """Run DDRescue data recovery and parse the output."""

    args_model = DDRescueArgs

    def build_command(self, args: DDRescueArgs) -> List[str]:
        cmd: List[str] = ["ddrescue"]
        if args.no_scrape:
            cmd.append("-n")
        if args.reverse:
            cmd.append("-R")
        if args.retry_passes is not None:
            cmd.extend(["-r", str(args.retry_passes)])
        if args.verbose:
            cmd.append("-v")
        cmd.extend([args.target, args.output_file])
        if args.log_file:
            cmd.append(args.log_file)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: DDRescueArgs,
    ) -> Dict[str, Any]:
        metadata = parse_ddrescue_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: DDRescueArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/ddrescue_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: DDRescueArgs) -> ToolResult:
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
        tool_id="forensics.forensics_carving_tools.ddrescue",
        display_name="DDRescue",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="media_recovery",
                description="Copy data from damaged media to an image with retry logic and rescue logs; returns recovery statistics (bytes copied, error rate); sector-level recovery, not signature carving.",
                output_indicators=["bytes_copied", "bytes_failed"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=1,
        estimated_runtime_minutes=30,
    )
)
