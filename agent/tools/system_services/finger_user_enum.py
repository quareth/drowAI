"""Finger user enumeration tool."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120


class FingerUserEnumArgs(BaseToolArgs):
    """Arguments for finger user enumeration."""

    username: Optional[str] = Field(
        default=None,
        description="Specific username to query.",
    )
    long_format: bool = Field(
        default=False,
        description="Use long format output (-l).",
    )


def parse_finger_output(output_text: str) -> Dict[str, Any]:
    """Parse finger output into structured metadata."""
    metadata: Dict[str, Any] = {
        "users": [],
        "details": [],
        "errors": [],
    }

    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue

        if "error" in line.lower() or "unknown user" in line.lower():
            metadata["errors"].append(line)
            continue

        login_match = re.search(r"Login:\s*(\S+)", line)
        if login_match:
            metadata["users"].append(login_match.group(1))
            metadata["details"].append(line)
            continue

        if line.startswith("Login"):
            continue

        if line and len(line.split()) >= 2 and line.split()[0].isalpha():
            metadata["details"].append(line)

    metadata["total_users"] = len(metadata["users"])
    return metadata


class FingerUserEnumTool(BaseTool):
    """Finger user enumeration tool."""

    args_model = FingerUserEnumArgs

    def build_command(self, args: FingerUserEnumArgs) -> List[str]:
        cmd = ["finger"]
        if args.long_format:
            cmd.append("-l")
        target = f"{args.username}@{args.target}" if args.username else f"@{args.target}"
        cmd.append(target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FingerUserEnumArgs,
    ) -> Dict[str, Any]:
        metadata = parse_finger_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: FingerUserEnumArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        path = f"artifacts/finger_user_enum_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [path]

    def run(self, args: FingerUserEnumArgs) -> ToolResult:
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
                stderr="finger command not found. Ensure finger is installed.",
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
        artifacts = self.create_artifacts(
            proc.stdout, args=args, timestamp=int(start), stderr=proc.stderr
        )

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
from agent.tools.enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="system_services.finger_user_enum",
        display_name="finger",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="finger_user_enumeration",
                description="Enumerates users via the Finger protocol.",
                output_indicators=["Login", "Name"],
            ),
        ],
        required_services=["finger"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=2,
    )
)
