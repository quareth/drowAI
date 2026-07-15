"""Weevely tool for creating and managing PHP web shells."""

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

ARTIFACT_MIN_CHARS = 120
DEFAULT_TIMEOUT = 30


class WeevelyMode(str, Enum):
    """Supported Weevely modes."""

    GENERATE = "generate"
    CONNECT = "connect"


class WeevelyArgs(BaseToolArgs):
    """Arguments for the Weevely tool."""

    mode: WeevelyMode = Field(
        ...,
        description="Weevely mode to use.",
    )
    password: str = Field(
        ...,
        description="Password for web shell authentication.",
        min_length=1,
    )
    output_path: str = Field(
        ...,
        description="Output path for generate mode.",
        min_length=1,
    )
    url: Optional[str] = Field(
        None,
        description="Backdoor URL for connect mode.",
    )
    command: Optional[str] = Field(
        None,
        description="Command to execute on connect mode.",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_weevely_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse Weevely output into structured metadata."""
    metadata: Dict[str, Any] = {
        "web_shell_created": False,
        "file_size": 0,
        "obfuscated": False,
        "connection_status": "unknown",
        "errors": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    for line in combined.splitlines():
        line = line.strip()
        if not line:
            continue

        if "created" in line.lower() or "generated" in line.lower():
            metadata["web_shell_created"] = True
        elif "size" in line.lower() and "bytes" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                metadata["file_size"] = int(match.group(1))
        elif "obfuscated" in line.lower():
            metadata["obfuscated"] = True
        elif "connected" in line.lower():
            metadata["connection_status"] = "connected"
        elif "failed" in line.lower() or "error" in line.lower():
            metadata["connection_status"] = "failed"
            metadata["errors"].append(line)
    
    return metadata


class WeevelyTool(BaseTool):
    """Weevely tool for creating PHP web shells."""
    
    args_model = WeevelyArgs

    def build_command(self, args: WeevelyArgs) -> List[str]:
        cmd: List[str] = ["weevely"]

        if args.mode == WeevelyMode.GENERATE:
            cmd.extend(["generate", args.password, args.output_path])
            return cmd

        if not args.url:
            raise ValueError("url is required for connect mode.")
        cmd.extend([args.url, args.password])
        if args.command:
            cmd.append(args.command)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: WeevelyArgs,
    ) -> Dict[str, Any]:
        metadata = parse_weevely_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["mode"] = args.mode.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: WeevelyArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []

        if args.mode == WeevelyMode.GENERATE and args.output_path:
            if os.path.exists(args.output_path):
                artifacts.append(args.output_path)

        if stdout and len(stdout) >= ARTIFACT_MIN_CHARS:
            os.makedirs("artifacts", exist_ok=True)
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/weevely_{ts}.txt"
            try:
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                return artifacts

        return artifacts

    def run(self, args: WeevelyArgs) -> ToolResult:
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
                stderr="weevely command not found. Ensure Weevely is installed.",
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
        tool_id="maintaining_access.web_backdoors.weevely",
        display_name="Weevely",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="web_shell_management",
                description="Generate or connect to a password-protected obfuscated PHP web shell; returns shell file or remote command output; requires confirmed PHP upload or execution capability",
                output_indicators=["weevely", "connected", "generated"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["http", "https"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=4,
    )
)
