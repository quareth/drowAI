"""Cymothoa tool for process injection and backdoor creation."""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_TIMEOUT = 30


class CymothoaArgs(BaseToolArgs):
    """Arguments for the Cymothoa tool."""

    process_id: int = Field(
        ...,
        description="Target process ID for injection (-p).",
        gt=0,
    )
    shellcode_num: int = Field(
        ...,
        description="Shellcode number to use (-s).",
        ge=0,
    )
    port: Optional[int] = Field(
        None,
        description="Port for reverse shell payloads (-y).",
        ge=1,
        le=65535,
    )
    list_shellcodes: bool = Field(
        False,
        description="List available shellcodes (-S).",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT,
        description="Maximum execution time in seconds before the tool is terminated.",
        ge=1,
    )


def parse_cymothoa_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse Cymothoa output into structured metadata."""
    metadata: Dict[str, Any] = {
        "injection_successful": False,
        "process_id": None,
        "shellcode_num": None,
        "memory_address": None,
        "errors": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    for line in combined.splitlines():
        line = line.strip()
        if not line:
            continue

        if "injected" in line.lower() and "success" in line.lower():
            metadata["injection_successful"] = True
        if "pid" in line.lower():
            pid_match = re.search(r"\bpid[:\s]+(\d+)", line, re.IGNORECASE)
            if pid_match:
                metadata["process_id"] = int(pid_match.group(1))
        if "shellcode" in line.lower():
            sc_match = re.search(r"\bshellcode[:\s]+(\d+)", line, re.IGNORECASE)
            if sc_match:
                metadata["shellcode_num"] = int(sc_match.group(1))
        if "0x" in line:
            addr_match = re.search(r"0x[0-9a-fA-F]+", line)
            if addr_match:
                metadata["memory_address"] = addr_match.group(0)
        if "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)

    return metadata


class CymothoaTool(BaseTool):
    """Cymothoa tool for process injection and backdoor creation."""

    args_model = CymothoaArgs

    def build_command(self, args: CymothoaArgs) -> List[str]:
        cmd: List[str] = ["cymothoa"]

        if args.list_shellcodes:
            cmd.append("-S")
            return cmd

        cmd.extend(["-p", str(args.process_id)])
        cmd.extend(["-s", str(args.shellcode_num)])

        if args.port is not None:
            cmd.extend(["-y", str(args.port)])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: CymothoaArgs,
    ) -> Dict[str, Any]:
        metadata = parse_cymothoa_output(stdout or "", stderr or "")
        metadata["exit_code"] = exit_code
        metadata["process_id"] = args.process_id
        metadata["shellcode_num"] = args.shellcode_num
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: CymothoaArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_MIN_CHARS:
            return []

        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        artifact_path = f"artifacts/cymothoa_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: CymothoaArgs) -> ToolResult:
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
                stderr="cymothoa command not found. Ensure Cymothoa is installed.",
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
        tool_id="maintaining_access.os_backdoors.cymothoa",
        display_name="Cymothoa",
        category=ToolCategory.MAINTAINING_ACCESS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="process_injection",
                description="Inject shellcode into a running Linux process for persistence; requires target PID and shellcode ID; returns injection success and memory address; active — modifies process memory.",
                output_indicators=["injected", "shellcode", "pid"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=5,
    )
)
