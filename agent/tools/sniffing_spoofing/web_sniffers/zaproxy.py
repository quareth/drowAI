"""OWASP ZAP (Zed Attack Proxy) web application security testing tool."""

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
DEFAULT_TIMEOUT = 900


class ZapReportFormat(str, Enum):
    """Output formats for ZAP baseline reports."""

    HTML = "html"
    JSON = "json"
    XML = "xml"
    MARKDOWN = "markdown"


class ZapProxyArgs(BaseToolArgs):
    """Arguments for the OWASP ZAP baseline tool."""

    target_url: str = Field(
        ...,
        description="Target URL to scan.",
    )
    report_format: ZapReportFormat = Field(
        ZapReportFormat.HTML,
        description="Report output format.",
    )
    report_file: Optional[str] = Field(
        None,
        description="Path to save the report file.",
    )
    config_file: Optional[str] = Field(
        None,
        description="ZAP config file to generate (-g).",
    )
    rules_file: Optional[str] = Field(
        None,
        description="Rules configuration file for alert thresholds (-c).",
    )
    ajax_spider: bool = Field(
        False,
        description="Enable AJAX spider in baseline scan (-j).",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output.",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments.",
    )


def parse_zaproxy_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """Parse ZAP baseline output into structured metadata."""
    metadata: Dict[str, Any] = {
        "alerts_found": 0,
        "high_alerts": 0,
        "medium_alerts": 0,
        "low_alerts": 0,
        "info_alerts": 0,
        "scan_status": "unknown",
        "urls_scanned": 0,
        "errors": [],
        "warnings": [],
    }

    combined = "\n".join([stdout or "", stderr or ""]).strip()
    if not combined:
        return metadata

    lines = combined.splitlines()
    for line in lines:
        lowered = line.lower()
        if "alert" in lowered:
            metadata["alerts_found"] += 1
            if "high" in lowered:
                metadata["high_alerts"] += 1
            elif "medium" in lowered:
                metadata["medium_alerts"] += 1
            elif "low" in lowered:
                metadata["low_alerts"] += 1
            elif "info" in lowered:
                metadata["info_alerts"] += 1

        url_match = re.search(r"(\d+)\s+urls?", line, re.IGNORECASE)
        if url_match:
            metadata["urls_scanned"] = int(url_match.group(1))

        if "completed" in lowered:
            metadata["scan_status"] = "completed"
        if "error" in lowered:
            metadata["errors"].append(line.strip())
        elif "warn" in lowered:
            metadata["warnings"].append(line.strip())

    return metadata


class ZapProxyTool(BaseTool):
    """Run OWASP ZAP baseline scan for web application security testing."""

    args_model = ZapProxyArgs

    def build_command(self, args: ZapProxyArgs) -> List[str]:
        cmd: List[str] = ["zap-baseline.py", "-t", args.target_url]

        if args.report_file:
            if args.report_format == ZapReportFormat.HTML:
                cmd.extend(["-r", args.report_file])
            elif args.report_format == ZapReportFormat.JSON:
                cmd.extend(["-J", args.report_file])
            elif args.report_format == ZapReportFormat.XML:
                cmd.extend(["-x", args.report_file])
            elif args.report_format == ZapReportFormat.MARKDOWN:
                cmd.extend(["-w", args.report_file])

        if args.config_file:
            cmd.extend(["-g", args.config_file])
        if args.rules_file:
            cmd.extend(["-c", args.rules_file])
        if args.ajax_spider:
            cmd.append("-j")
        if args.verbose:
            cmd.append("-v")
        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ZapProxyArgs,
    ) -> Dict[str, Any]:
        metadata = parse_zaproxy_output(stdout, stderr)
        metadata["exit_code"] = exit_code
        metadata["report_format"] = args.report_format.value
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ZapProxyArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if args.report_file and os.path.exists(args.report_file):
            artifacts.append(args.report_file)

        if stdout and len(stdout) >= ARTIFACT_MIN_CHARS:
            os.makedirs("artifacts", exist_ok=True)
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/zaproxy_{ts}.txt"
            try:
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                return artifacts

        return artifacts

    def run(self, args: ZapProxyArgs) -> ToolResult:
        start = time.time()
        timeout = args.timeout or DEFAULT_TIMEOUT

        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={"timeout": timeout},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="zap-baseline.py command not found. Ensure OWASP ZAP is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
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
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="sniffing_spoofing.web_sniffers.zaproxy",
        display_name="OWASP ZAP Baseline",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.VULNERABILITY_ASSESSMENT],
        capabilities=[
            ToolCapability(
                name="web_baseline_scan",
                description="Run an OWASP ZAP baseline crawl and passive scan against a web URL; returns alert counts by severity and scanned URLs; not for packet sniffing — web vulnerability discovery only.",
                output_indicators=["zap", "alert", "baseline"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=15,
    )
)