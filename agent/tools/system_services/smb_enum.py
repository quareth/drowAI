"""SMB enumeration tool using smbclient."""

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
DEFAULT_PORT = 445


class SMBEnumArgs(BaseToolArgs):
    """Arguments for SMB enumeration using smbclient -L."""

    username: Optional[str] = Field(
        default=None,
        description="Username for authentication (-U).",
    )
    password: Optional[str] = Field(
        default=None,
        description="Password for authentication.",
    )
    domain: Optional[str] = Field(
        default=None,
        description="Domain or workgroup (-W).",
    )
    port: int = Field(
        default=DEFAULT_PORT,
        description="SMB port (-p).",
        ge=1,
        le=65535,
    )
    no_pass: bool = Field(
        default=False,
        description="Use null session authentication (-N).",
    )
    verbose: bool = Field(
        default=False,
        description="Enable verbose output (-d 1).",
    )


def parse_smb_enum_output(output_text: str) -> Dict[str, Any]:
    """Parse smbclient -L output into structured metadata."""
    metadata: Dict[str, Any] = {
        "shares": [],
        "warnings": [],
        "errors": [],
    }

    in_share_table = False
    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            if in_share_table:
                in_share_table = False
            continue

        if line.lower().startswith("sharename"):
            in_share_table = True
            continue

        if "nt_status" in line.lower() or "error" in line.lower():
            metadata["errors"].append(line)
            continue

        if in_share_table:
            match = re.match(r"^(?P<name>\S+)\s+(?P<type>\S+)\s*(?P<comment>.*)$", line)
            if match:
                metadata["shares"].append(
                    {
                        "name": match.group("name"),
                        "type": match.group("type"),
                        "comment": match.group("comment").strip(),
                    }
                )
            continue

        if "warning" in line.lower():
            metadata["warnings"].append(line)

    metadata["total_shares"] = len(metadata["shares"])
    return metadata


class SMBEnumTool(BaseTool):
    """SMB Enumeration Tool for share listing using smbclient."""

    args_model = SMBEnumArgs

    def build_command(self, args: SMBEnumArgs) -> List[str]:
        cmd = ["smbclient", "-L", f"//{args.target}"]
        if args.verbose:
            cmd.extend(["-d", "1"])
        if args.no_pass:
            cmd.append("-N")
        if args.username:
            if args.password:
                cmd.extend(["-U", f"{args.username}%{args.password}"])
            else:
                cmd.extend(["-U", args.username])
        if args.domain:
            cmd.extend(["-W", args.domain])
        if args.port != DEFAULT_PORT:
            cmd.extend(["-p", str(args.port)])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SMBEnumArgs,
    ) -> Dict[str, Any]:
        metadata = parse_smb_enum_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SMBEnumArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        path = f"artifacts/smb_enum_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [path]

    def run(self, args: SMBEnumArgs) -> ToolResult:
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
                stderr="smbclient command not found. Ensure smbclient is installed.",
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
        tool_id="system_services.smb_enum",
        display_name="smbclient",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="smb_share_enumeration",
                description="Enumerates SMB shares via smbclient.",
                output_indicators=["sharename", "disk", "print"],
            ),
        ],
        required_services=["smb"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=3,
    )
)
