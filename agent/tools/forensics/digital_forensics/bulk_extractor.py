"""Bulk Extractor - Digital forensics tool for extracting and analyzing data from disk images."""

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


class BulkExtractorArgs(BaseToolArgs):
    """Arguments for the Bulk Extractor tool."""

    output_directory: str = Field(
        ...,
        description="Output directory for extracted features (-o)",
    )
    enable_scanners: List[str] = Field(
        default_factory=list,
        description="Scanners to enable (-e)",
    )
    disable_scanners: List[str] = Field(
        default_factory=list,
        description="Scanners to disable (-x)",
    )
    threads: int = Field(
        4,
        ge=1,
        le=32,
        description="Number of threads/jobs (-j)",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output (-v)",
    )
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors (-q)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=300,
        le=7200,
        description="Timeout in seconds for the entire operation",
    )


def parse_bulk_extractor_output(output_text: str) -> Dict[str, Any]:
    """Parse Bulk Extractor output into structured metadata."""
    metadata: Dict[str, Any] = {"summary": {}, "features": {}}

    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "processed" in line.lower():
            processed_match = re.search(r"(\d+)\s+processed", line.lower())
            if processed_match:
                metadata["summary"]["files_processed"] = int(processed_match.group(1))
        if ":" in line and re.search(r"\b\d+\b", line):
            parts = line.split(":", 1)
            if len(parts) == 2:
                name = parts[0].strip()
                count_match = re.search(r"(\d+)", parts[1])
                if name and count_match:
                    metadata["features"][name] = int(count_match.group(1))

    metadata["summary"]["total_features"] = sum(metadata["features"].values())
    return metadata


class BulkExtractorTool(BaseTool):
    """Bulk Extractor - Digital forensics tool for extracting and analyzing data from disk images."""

    args_model = BulkExtractorArgs

    def build_command(self, args: BulkExtractorArgs) -> List[str]:
        cmd: List[str] = ["bulk_extractor", "-o", args.output_directory]

        for scanner in args.enable_scanners:
            cmd.extend(["-e", scanner])
        for scanner in args.disable_scanners:
            cmd.extend(["-x", scanner])

        if args.threads:
            cmd.extend(["-j", str(args.threads)])

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
        args: BulkExtractorArgs,
    ) -> Dict[str, Any]:
        metadata = parse_bulk_extractor_output(stdout or "")
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: BulkExtractorArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/bulk_extractor_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: BulkExtractorArgs) -> ToolResult:
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
        tool_id="forensics.digital_forensics.bulk_extractor",
        display_name="Bulk Extractor",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="artifact_extraction",
                description="Extract features (emails, URLs, phone numbers, credit cards) from disk images with parallel scanners; returns feature counts and classified artifacts; read-only.",
                output_indicators=["features", "files_processed"],
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
