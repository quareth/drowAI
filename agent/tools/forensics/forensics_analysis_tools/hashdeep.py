"""Hashdeep - File hashing and integrity verification tool for digital forensics."""

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


DEFAULT_TIMEOUT_SECONDS = 1800
ARTIFACT_OUTPUT_MIN_CHARS = 200


class HashAlgorithm(str, Enum):
    """Supported hash algorithms."""

    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA512 = "sha512"


class HashdeepArgs(BaseToolArgs):
    """Arguments for the Hashdeep tool."""

    hash_algorithm: HashAlgorithm = Field(
        HashAlgorithm.SHA256,
        description="Hash algorithm to use",
    )
    recursive: bool = Field(
        True,
        description="Process directories recursively (-r)",
    )
    additional_paths: List[str] = Field(
        default_factory=list,
        description="Additional files or directories to hash",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output (-v)",
    )
    quiet: bool = Field(
        False,
        description="Suppress output except for errors (-q)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=300,
        le=7200,
        description="Timeout in seconds for the entire operation",
    )


def _algorithm_binary(algorithm: HashAlgorithm) -> str:
    if algorithm == HashAlgorithm.MD5:
        return "md5deep"
    if algorithm == HashAlgorithm.SHA1:
        return "sha1deep"
    if algorithm == HashAlgorithm.SHA512:
        return "sha512deep"
    return "sha256deep"


def parse_hashdeep_output(output_text: str) -> Dict[str, Any]:
    """Parse Hashdeep output into structured metadata."""
    metadata: Dict[str, Any] = {"hashes": [], "summary": {}}

    for line in output_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+", line, maxsplit=2)
        if len(parts) >= 3 and re.fullmatch(r"[0-9a-fA-F]+", parts[0]):
            metadata["hashes"].append(
                {
                    "hash": parts[0],
                    "size": parts[1],
                    "file_path": parts[2],
                }
            )

    metadata["summary"]["total_hashes"] = len(metadata["hashes"])
    return metadata


class HashdeepTool(BaseTool):
    """Hashdeep - File hashing and integrity verification tool for digital forensics."""

    args_model = HashdeepArgs

    def build_command(self, args: HashdeepArgs) -> List[str]:
        cmd: List[str] = [_algorithm_binary(args.hash_algorithm)]

        if args.recursive:
            cmd.append("-r")
        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")

        cmd.append(args.target)
        cmd.extend(args.additional_paths)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: HashdeepArgs,
    ) -> Dict[str, Any]:
        metadata = parse_hashdeep_output(stdout or "")
        metadata["algorithm"] = args.hash_algorithm.value
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: HashdeepArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/hashdeep_{args.hash_algorithm.value}_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: HashdeepArgs) -> ToolResult:
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
        tool_id="forensics.forensics_analysis_tools.hashdeep",
        display_name="Hashdeep",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="file_hashing",
                description="Compute recursive file hashes (MD5, SHA1, SHA256, SHA512) for integrity verification; returns hash table and per-file statistics; read-only.",
                output_indicators=["hashes", "total_hashes"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=4,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=5,
    )
)
