"""OScanner tool for Oracle database scanning."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 100
DEFAULT_ORACLE_PORT = 1521
DEFAULT_TIMEOUT_SECONDS = 300


class OScannerArgs(BaseToolArgs):
    """Arguments for the OScanner tool."""

    server: str = Field(
        ...,
        description="Target Oracle server hostname or IP.",
    )
    port: int = Field(
        DEFAULT_ORACLE_PORT,
        description="Oracle listener port.",
        ge=1,
        le=65535,
    )
    report_file: Optional[str] = Field(
        None,
        description="Path to save the report file.",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Maximum execution time in seconds.",
        ge=1,
        le=3600,
    )


def _parse_oscanner_output(output_text: str) -> Dict[str, Any]:
    """Parse oscanner output into structured metadata."""
    metadata: Dict[str, Any] = {
        "sids_found": [],
        "services_found": [],
        "accounts_found": [],
        "errors": [],
    }

    for line in output_text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue

        lower = trimmed.lower()
        if "sid" in lower and ":" in trimmed:
            metadata["sids_found"].append(trimmed.split(":", 1)[-1].strip())
        elif "service" in lower and ":" in trimmed:
            metadata["services_found"].append(trimmed.split(":", 1)[-1].strip())
        elif "user" in lower and ":" in trimmed:
            metadata["accounts_found"].append(trimmed.split(":", 1)[-1].strip())
        elif "error" in lower or "failed" in lower:
            metadata["errors"].append(trimmed)

    return metadata


class OScannerTool(BaseTool):
    """OScanner tool for Oracle database scanning."""

    args_model = OScannerArgs

    def build_command(self, args: OScannerArgs) -> List[str]:
        cmd = ["oscanner", "-s", args.server, "-p", str(args.port)]
        if args.report_file:
            cmd.extend(["-r", args.report_file])
        if args.verbose:
            cmd.append("-v")
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: OScannerArgs,
    ) -> Dict[str, Any]:
        combined = "\n".join([stdout or "", stderr or ""]).strip()
        metadata = _parse_oscanner_output(combined)
        metadata["exit_code"] = exit_code
        metadata["server"] = args.server
        metadata["port"] = args.port
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: OScannerArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/oscanner_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: OScannerArgs) -> ToolResult:
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
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="oscanner command not found. Ensure it is installed.",
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
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start))

        return ToolResult(
            success=proc.returncode == 0,
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
        tool_id="database_assessment.oracle_tools.oscanner",
        display_name="OScanner",
        category=ToolCategory.DATABASE_ASSESSMENT,
        applicable_phases=[
            PentestPhase.ENUMERATION,
            PentestPhase.VULNERABILITY_ASSESSMENT,
        ],
        capabilities=[
            ToolCapability(
                name="oracle_enumeration",
                description="Enumerate Oracle listener metadata and accounts.",
                output_indicators=["sid", "service", "user"],
            ),
        ],
        required_services=["oracle"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)
