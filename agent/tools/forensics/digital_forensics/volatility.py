"""Volatility command tool for memory forensics analysis."""

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


DEFAULT_TIMEOUT_SECONDS = 600
ARTIFACT_OUTPUT_MIN_CHARS = 200


class VolatilityVersion(str, Enum):
    """Supported Volatility versions."""

    VOL2 = "vol2"
    VOL3 = "vol3"


class VolatilityBinary(str, Enum):
    """Common Volatility binary names."""

    VOLATILITY = "volatility"
    VOLATILITY3 = "volatility3"
    VOL = "vol"


class VolatilityArgs(BaseToolArgs):
    """Arguments for the Volatility tool."""

    version: VolatilityVersion = Field(
        VolatilityVersion.VOL3,
        description="Volatility major version to use",
    )
    binary: Optional[VolatilityBinary] = Field(
        None,
        description="Override the default Volatility binary name",
    )
    profile: Optional[str] = Field(
        None,
        description="Volatility 2 profile name (e.g., Win7SP1x64)",
    )
    plugin: str = Field(
        "pslist",
        description="Volatility plugin to run (e.g., pslist, netscan, filescan)",
    )
    plugin_options: List[str] = Field(
        default_factory=list,
        description="Additional plugin-specific arguments",
    )
    output_format: Optional[str] = Field(
        None,
        description="Output format/renderer (e.g., json, csv, text)",
    )
    output_file: Optional[str] = Field(
        None,
        description="Volatility 2 output file path",
    )
    output_directory: Optional[str] = Field(
        None,
        description="Volatility 3 output directory",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    quiet: bool = Field(
        False,
        description="Suppress non-essential output",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds for the operation",
        ge=60,
        le=7200,
    )


def _default_binary(version: VolatilityVersion) -> VolatilityBinary:
    return VolatilityBinary.VOLATILITY if version == VolatilityVersion.VOL2 else VolatilityBinary.VOLATILITY3


def parse_volatility_output(output_text: str) -> Dict[str, Any]:
    """Parse volatility output into structured metadata."""

    metadata: Dict[str, Any] = {
        "output_lines": 0,
        "processes_found": 0,
        "connections_found": 0,
        "files_found": 0,
        "modules_found": 0,
        "handles_found": 0,
        "registry_keys": 0,
        "plugin_status": "unknown",
    }

    for line in output_text.splitlines():
        line = line.strip()
        if not line:
            continue
        metadata["output_lines"] += 1
        if re.search(r"\bPID\b", line) and re.search(r"\bName\b", line):
            metadata["processes_found"] += 1
        if "Connection" in line or "Socket" in line:
            metadata["connections_found"] += 1
        if "File" in line and "path" in line.lower():
            metadata["files_found"] += 1
        if "Module" in line or "DLL" in line:
            metadata["modules_found"] += 1
        if "Handle" in line:
            metadata["handles_found"] += 1
        if "Registry" in line or "HKEY" in line:
            metadata["registry_keys"] += 1

    return metadata


class VolatilityTool(BaseTool):
    """Run volatility command for memory forensics analysis."""

    args_model = VolatilityArgs

    def build_command(self, args: VolatilityArgs) -> List[str]:
        binary = args.binary.value if args.binary else _default_binary(args.version).value
        cmd: List[str] = [binary, "-f", args.target]

        if args.version == VolatilityVersion.VOL2:
            if args.profile:
                cmd.extend(["--profile", args.profile])
            if args.output_format:
                cmd.extend(["--output", args.output_format])
            if args.output_file:
                cmd.extend(["--output-file", args.output_file])
        else:
            if args.output_format:
                cmd.extend(["-r", args.output_format])
            if args.output_directory:
                cmd.extend(["-o", args.output_directory])

        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")

        cmd.append(args.plugin)
        cmd.extend(args.plugin_options)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: VolatilityArgs,
    ) -> Dict[str, Any]:
        metadata = parse_volatility_output(stdout or "")
        metadata["plugin"] = args.plugin
        metadata["version"] = args.version.value
        if args.profile:
            metadata["profile"] = args.profile
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: VolatilityArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []
        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/volatility_{args.plugin}_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: VolatilityArgs) -> ToolResult:
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
        tool_id="forensics.digital_forensics.volatility",
        display_name="Volatility",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="memory_forensics",
                description="Analyze memory dumps with Volatility 2/3 plugins (pslist, netscan, filescan); returns process list, network connections, and file handles; read-only.",
                output_indicators=["processes_found", "connections_found", "files_found"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=10,
    )
)
