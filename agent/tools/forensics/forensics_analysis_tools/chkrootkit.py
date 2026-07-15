"""Chkrootkit - Rootkit detection and system integrity checking tool."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 600
ARTIFACT_OUTPUT_MIN_CHARS = 200


class ChkrootkitArgs(BaseToolArgs):
    """Arguments for the Chkrootkit tool."""

    root_directory: Optional[str] = Field(
        None,
        description="Alternate root directory (-r)",
    )
    search_path: Optional[str] = Field(
        None,
        description="Search path override (-p)",
    )
    expert_mode: bool = Field(
        False,
        description="Enable expert mode (-x)",
    )
    quiet: bool = Field(
        False,
        description="Suppress output except for warnings (-q)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=60,
        le=3600,
        description="Timeout in seconds for the operation",
    )


def parse_chkrootkit_output(output_text: str) -> Dict[str, Any]:
    """Parse Chkrootkit output into structured metadata."""
    metadata: Dict[str, Any] = {"alerts": [], "summary": {}}
    for line in output_text.splitlines():
        if "INFECTED" in line or "WARNING" in line or "SUSPICIOUS" in line:
            metadata["alerts"].append(line.strip())
    metadata["summary"]["alerts_found"] = len(metadata["alerts"])
    return metadata


class ChkrootkitTool(BaseTool):
    """Chkrootkit - Rootkit detection and system integrity checking tool."""

    args_model = ChkrootkitArgs

    def build_command(self, args: ChkrootkitArgs) -> List[str]:
        cmd: List[str] = ["chkrootkit"]
        if args.root_directory:
            cmd.extend(["-r", args.root_directory])
        if args.search_path:
            cmd.extend(["-p", args.search_path])
        if args.expert_mode:
            cmd.append("-x")
        if args.quiet:
            cmd.append("-q")
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ChkrootkitArgs,
    ) -> Dict[str, Any]:
        metadata = parse_chkrootkit_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ChkrootkitArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/chkrootkit_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: ChkrootkitArgs) -> ToolResult:
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
        tool_id="forensics.forensics_analysis_tools.chkrootkit",
        display_name="Chkrootkit",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="rootkit_detection",
                description="Detect rootkits and suspicious binaries on a live or mounted filesystem; returns alerts with INFECTED, WARNING, or SUSPICIOUS classifications; read-only.",
                output_indicators=["alerts_found"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=4,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=10,
    )
)
