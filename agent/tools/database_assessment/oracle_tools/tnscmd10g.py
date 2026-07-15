"""TNSCmd10g tool for Oracle TNS command execution."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 100
DEFAULT_ORACLE_PORT = 1521
DEFAULT_TIMEOUT_SECONDS = 30


class TNSCommand(str, Enum):
    """Supported tnscmd10g commands."""

    PING = "ping"
    VERSION = "version"
    STATUS = "status"
    SERVICES = "services"


class TNSCmd10gArgs(BaseToolArgs):
    """Arguments for the TNSCmd10g tool."""

    command: TNSCommand = Field(
        TNSCommand.PING,
        description="TNS command to execute (ping, version, status, services).",
    )
    host: str = Field(
        ...,
        description="Target Oracle TNS listener host.",
    )
    port: int = Field(
        DEFAULT_ORACLE_PORT,
        description="Oracle TNS listener port.",
        ge=1,
        le=65535,
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Connection timeout in seconds.",
        ge=1,
        le=300,
    )


def _parse_tnscmd10g_output(
    output_text: str,
    command: str,
) -> Dict[str, Any]:
    """Parse tnscmd10g output into structured metadata."""
    metadata: Dict[str, Any] = {
        "command": command,
        "ping_successful": False,
        "version_obtained": None,
        "status_obtained": None,
        "services_found": [],
        "errors": [],
    }

    for line in output_text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue

        lower = trimmed.lower()
        if "version" in lower:
            metadata["version_obtained"] = trimmed.split(":", 1)[-1].strip()
        elif "status" in lower:
            metadata["status_obtained"] = trimmed.split(":", 1)[-1].strip()
        elif "service" in lower and ":" in trimmed:
            service_name = trimmed.split(":", 1)[-1].strip()
            metadata["services_found"].append(service_name)
        elif "ping" in lower and "ok" in lower:
            metadata["ping_successful"] = True
        elif "error" in lower or "failed" in lower:
            metadata["errors"].append(trimmed)

    return metadata


class TNSCmd10gTool(BaseTool):
    """TNSCmd10g tool for Oracle TNS command execution."""

    args_model = TNSCmd10gArgs

    def build_command(self, args: TNSCmd10gArgs) -> List[str]:
        cmd = ["tnscmd10g", args.command.value, "-h", args.host]
        cmd.extend(["-p", str(args.port)])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TNSCmd10gArgs,
    ) -> Dict[str, Any]:
        combined = "\n".join([stdout or "", stderr or ""]).strip()
        metadata = _parse_tnscmd10g_output(combined, args.command.value)
        metadata["exit_code"] = exit_code
        metadata["host"] = args.host
        metadata["port"] = args.port
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: TNSCmd10gArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/tnscmd10g_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: TNSCmd10gArgs) -> ToolResult:
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
                stderr="tnscmd10g command not found. Ensure it is installed.",
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
        tool_id="database_assessment.oracle_tools.tnscmd10g",
        display_name="TNSCmd10g",
        category=ToolCategory.DATABASE_ASSESSMENT,
        applicable_phases=[
            PentestPhase.ENUMERATION,
            PentestPhase.VULNERABILITY_ASSESSMENT,
        ],
        capabilities=[
            ToolCapability(
                name="tns_enumeration",
                description="Enumerate Oracle TNS listener information.",
                output_indicators=["version", "status", "service"],
            ),
        ],
        required_services=["oracle"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
