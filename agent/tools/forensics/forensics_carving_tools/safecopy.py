"""SafeCopy - Data recovery tool for damaged media."""

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


class SafeCopyStage(str, Enum):
    """SafeCopy recovery stages."""

    STAGE1 = "stage1"
    STAGE2 = "stage2"
    STAGE3 = "stage3"
    AUTO = "auto"


class SafeCopyArgs(BaseToolArgs):
    """Arguments for the SafeCopy tool."""

    destination: str = Field(..., description="Destination file for recovered data")
    stage: SafeCopyStage = Field(
        SafeCopyStage.AUTO,
        description="Recovery stage to execute",
    )
    log_file: Optional[str] = Field(
        None,
        description="Optional log file path",
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


def parse_safecopy_output(output_text: str) -> Dict[str, Any]:
    """Parse SafeCopy output into structured metadata."""
    metadata: Dict[str, Any] = {
        "bytes_recovered": 0,
        "blocks_processed": 0,
        "errors": [],
    }

    for line in output_text.splitlines():
        bytes_match = re.search(r"(\d+)\s+bytes?", line, re.IGNORECASE)
        if bytes_match:
            metadata["bytes_recovered"] = int(bytes_match.group(1))
        blocks_match = re.search(r"(\d+)\s+blocks?", line, re.IGNORECASE)
        if blocks_match:
            metadata["blocks_processed"] = int(blocks_match.group(1))
        if "error" in line.lower():
            metadata["errors"].append(line.strip())

    return metadata


class SafeCopyTool(BaseTool):
    """Run SafeCopy data recovery and parse the output."""

    args_model = SafeCopyArgs

    def build_command(self, args: SafeCopyArgs) -> List[str]:
        cmd: List[str] = ["safecopy"]

        if args.stage == SafeCopyStage.STAGE1:
            cmd.append("--stage1")
        elif args.stage == SafeCopyStage.STAGE2:
            cmd.append("--stage2")
        elif args.stage == SafeCopyStage.STAGE3:
            cmd.append("--stage3")

        if args.log_file:
            cmd.extend(["--log", args.log_file])

        if args.verbose:
            cmd.append("-v")

        cmd.extend([args.target, args.destination])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SafeCopyArgs,
    ) -> Dict[str, Any]:
        metadata = parse_safecopy_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SafeCopyArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/safecopy_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: SafeCopyArgs) -> ToolResult:
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
        tool_id="forensics.forensics_carving_tools.safecopy",
        display_name="SafeCopy",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="media_recovery",
                description="Recover data from damaged media using a three-stage SafeCopy approach; returns recovery metrics per stage; sector-level recovery, not signature carving.",
                output_indicators=["bytes_recovered", "blocks_processed"],
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
