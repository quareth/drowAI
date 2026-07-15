"""Serpico tool for penetration testing report generation."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class SerpicoMode(str, Enum):
    """Supported Serpico modes."""

    CREATE = "create"
    GENERATE = "generate"
    EXPORT = "export"
    IMPORT = "import"


class SerpicoArgs(BaseToolArgs):
    """Arguments for the Serpico tool."""

    mode: SerpicoMode = Field(
        SerpicoMode.CREATE,
        description="Serpico mode to use",
    )
    report_name: Optional[str] = Field(
        None,
        description="Name of the report to create or work with",
    )
    template: Optional[str] = Field(
        None,
        description="Report template to use",
    )
    output_format: Literal["pdf", "docx", "html", "json"] = Field(
        "pdf",
        description="Output format for the report",
    )
    findings_file: Optional[str] = Field(
        None,
        description="Path to findings file to import",
    )
    custom_fields: Dict[str, str] = Field(
        default_factory=dict,
        description="Custom fields to include in the report",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file for the generated report",
    )
    timeout: int = Field(
        60,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_serpico_output(output_text: str) -> Dict[str, Any]:
    """Parse Serpico output into structured metadata."""
    metadata: Dict[str, Any] = {
        "report_created": False,
        "report_generated": False,
        "findings_count": 0,
        "template_used": None,
        "output_file": None,
        "errors": [],
    }
    
    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Parse report creation
        if "created" in line.lower() and "report" in line.lower():
            metadata["report_created"] = True
        # Parse report generation
        elif "generated" in line.lower() and "report" in line.lower():
            metadata["report_generated"] = True
        # Parse findings count
        elif "findings" in line.lower() and "count" in line.lower():
            match = re.search(r"(\d+)", line)
            if match:
                try:
                    metadata["findings_count"] = int(match.group(1))
                except ValueError:
                    metadata["errors"].append(f"Failed to parse findings count from line: {line}")
        # Parse template information
        elif "template" in line.lower() and ":" in line:
            metadata["template_used"] = line.split(":")[-1].strip()
        # Parse output file
        elif "output" in line.lower() and "file" in line.lower():
            metadata["output_file"] = line.split(":")[-1].strip()
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)
    
    return metadata


class SerpicoTool(BaseTool):
    """Serpico tool for penetration testing report generation."""
    
    args_model = SerpicoArgs

    def build_command(self, args: SerpicoArgs) -> List[str]:
        cmd: List[str] = ["serpico", "--mode", args.mode.value, "--format", args.output_format]

        if args.report_name:
            cmd.extend(["--report", args.report_name])

        if args.template:
            cmd.extend(["--template", args.template])

        if args.findings_file:
            cmd.extend(["--findings", args.findings_file])

        for key, value in args.custom_fields.items():
            cmd.extend(["--field", f"{key}={value}"])

        if args.verbose:
            cmd.append("--verbose")

        if args.output_file:
            cmd.extend(["--output", args.output_file])

        # BaseToolArgs requires a target; Serpico uses it variably depending on install.
        # Keep it as last arg to preserve existing behavior.
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SerpicoArgs,
    ) -> Dict[str, Any]:
        metadata = parse_serpico_output(stdout or "")
        if stderr:
            # Keep errors/warnings discoverable without relying on log scraping
            metadata.setdefault("stderr", stderr[:2000])
        metadata["mode"] = args.mode.value
        metadata["output_format"] = args.output_format
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SerpicoArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if not stdout or len(stdout) < 100:
            return artifacts

        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/serpico_{args.mode.value}_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write(stdout)
            artifacts.append(artifact_path)
        except OSError:
            # Artifact creation is optional; keep tool usable even if FS is read-only.
            pass
        return artifacts
    
    def run(self, args: SerpicoArgs) -> ToolResult:
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
    ToolCatalogRole,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="reporting_tools.report_generation.serpico",
        display_name="Serpico",
        category=ToolCategory.REPORTING_TOOLS,
        catalog_role=ToolCatalogRole.UTILITY,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="report_generation",
                description="Generate a pentest report from findings and templates with Serpico; returns a report file in PDF, DOCX, HTML, or JSON; supports custom fields.",
                output_indicators=["report", "generated", "template", "findings"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=5,
        estimated_runtime_minutes=2,
    )
)
