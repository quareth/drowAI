"""Showmount tool for NFS mount enumeration and discovery."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ..base_tool import BaseTool
from ..schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120


class ShowmountOption(str, Enum):
    """Supported showmount options."""

    EXPORTS = "-e"
    ALL = "-a"
    DIRECTORY = "-d"


class ShowmountArgs(BaseToolArgs):
    """Arguments for the showmount tool."""

    option: ShowmountOption = Field(
        ShowmountOption.EXPORTS,
        description="Showmount option to use (-e for exports, -a for all, -d for directories)",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )


def parse_showmount_output(output_text: str) -> Dict[str, Any]:
    """Parse showmount output into structured metadata."""
    metadata: Dict[str, Any] = {
        "exports": [],
        "clients": [],
        "directories": [],
        "total_exports": 0,
        "total_clients": 0,
    }
    
    lines = output_text.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Parse exports (format: /path/to/export host1 host2)
        if line.startswith("/"):
            parts = line.split()
            if len(parts) >= 1:
                export_path = parts[0]
                metadata["exports"].append({
                    "path": export_path,
                    "clients": parts[1:] if len(parts) > 1 else []
                })
                metadata["total_exports"] += 1
                metadata["total_clients"] += len(parts[1:]) if len(parts) > 1 else 0
        # Parse client information
        elif ":" in line and not line.startswith("/"):
            metadata["clients"].append(line)
        # Parse directory information
        elif line.startswith("/") and len(line.split()) == 1:
            metadata["directories"].append(line)
    
    return metadata


class ShowmountTool(BaseTool):
    """Showmount tool for NFS mount enumeration and discovery."""
    
    args_model = ShowmountArgs
    
    def build_command(self, args: ShowmountArgs) -> List[str]:
        cmd = ["showmount", args.option.value]
        if args.verbose:
            cmd.append("-v")
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ShowmountArgs,
    ) -> Dict[str, Any]:
        metadata = parse_showmount_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ShowmountArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        path = f"artifacts/showmount_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [path]

    def run(self, args: ShowmountArgs) -> ToolResult:
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
                stderr="showmount command not found. Ensure nfs-common is installed.",
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
from ..enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="system_services.showmount",
        display_name="showmount",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="nfs_export_enumeration",
                description="Enumerates NFS exports and clients.",
                output_indicators=["exports", "clients"],
            ),
        ],
        required_services=["nfs"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=2,
    )
)
