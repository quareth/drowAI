"""Binwalk - Firmware analysis tool for reverse engineering and extracting firmware images."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 300
ARTIFACT_OUTPUT_MIN_CHARS = 200


class BinwalkMode(str, Enum):
    """Supported Binwalk modes."""

    SCAN = "scan"
    EXTRACT = "extract"
    ENTROPY = "entropy"


class BinwalkArgs(BaseToolArgs):
    """Arguments for the Binwalk tool."""

    mode: BinwalkMode = Field(
        BinwalkMode.SCAN,
        description="Binwalk mode to execute",
    )
    extract_directory: Optional[str] = Field(
        None,
        description="Directory to extract files to (binwalk -C)",
    )
    recursive: bool = Field(
        False,
        description="Recursively scan extracted files (binwalk -M)",
    )
    entropy: bool = Field(
        False,
        description="Enable entropy analysis (binwalk -E)",
    )
    verbose: bool = Field(
        False,
        description="Verbose output",
    )
    quiet: bool = Field(
        False,
        description="Quiet mode",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds",
        ge=60,
        le=3600,
    )


def parse_binwalk_output(output_text: str, mode: BinwalkMode) -> Dict[str, Any]:
    """Parse Binwalk output into structured metadata."""
    metadata: Dict[str, Any] = {
        "mode_executed": mode.value,
        "output_lines": len(output_text.splitlines()) if output_text else 0,
        "has_output": bool(output_text.strip()),
    }

    if not output_text.strip():
        return metadata

    if mode in (BinwalkMode.SCAN, BinwalkMode.EXTRACT):
        signatures_found = []
        for line in output_text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit():
                signatures_found.append(
                    {
                        "offset": parts[0],
                        "hex_offset": parts[1],
                        "description": " ".join(parts[2:]),
                    }
                )
        metadata["signatures_found"] = len(signatures_found)
        metadata["signature_list"] = signatures_found

    if mode == BinwalkMode.ENTROPY:
        entropy_points = 0
        for line in output_text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                try:
                    float(parts[1])
                    entropy_points += 1
                except ValueError:
                    continue
        metadata["entropy_points"] = entropy_points

    return metadata


class BinwalkTool(BaseTool):
    """Binwalk - Firmware analysis tool for reverse engineering and extracting firmware images."""

    args_model = BinwalkArgs

    def build_command(self, args: BinwalkArgs) -> List[str]:
        cmd: List[str] = ["binwalk"]

        if args.mode == BinwalkMode.EXTRACT:
            cmd.append("-e")
            if args.extract_directory:
                cmd.extend(["-C", args.extract_directory])
            if args.recursive:
                cmd.append("-M")

        if args.mode == BinwalkMode.ENTROPY or args.entropy:
            cmd.append("-E")

        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: BinwalkArgs,
    ) -> Dict[str, Any]:
        metadata = parse_binwalk_output(stdout or "", args.mode)
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: BinwalkArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/binwalk_{args.mode.value}_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: BinwalkArgs) -> ToolResult:
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
        tool_id="forensics.forensics_analysis_tools.binwalk",
        display_name="Binwalk",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="firmware_analysis",
                description="Scan firmware or binary blobs for embedded file signatures and entropy regions; returns offsets and extraction candidates; firmware-focused — not for full filesystem carving.",
                output_indicators=["signatures_found", "entropy_points"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=8,
    )
)
