"""WPScan WordPress security scanner tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import (
    parse_json_output,
    extract_vulnerabilities,
    normalize_severity,
    clean_output,
)


class ScanMode(str, Enum):
    """Supported WPScan scan modes."""

    PASSIVE = "passive"
    AGGRESSIVE = "aggressive"
    STEALTH = "stealth"
    FULL = "full"


class OutputFormat(str, Enum):
    """WPScan output formats."""

    JSON = "json"
    XML = "xml"
    CSV = "csv"
    CLI = "cli"


class WPScanArgs(BaseToolArgs):
    """Arguments for the WPScan tool."""

    scan_mode: ScanMode = Field(
        ScanMode.PASSIVE,
        description="Scan mode to perform (passive, aggressive, stealth, full)",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for results (json, xml, csv, cli)",
    )
    enumerate: Optional[List[str]] = Field(
        None,
        description="Enumerate specific items (e.g., ['u', 'p', 't', 'tt', 'cb', 'dbe'])",
    )
    plugins: bool = Field(
        False,
        description="Enumerate plugins",
    )
    themes: bool = Field(
        False,
        description="Enumerate themes",
    )
    users: bool = Field(
        False,
        description="Enumerate users",
    )
    passwords: Optional[List[str]] = Field(
        None,
        description="List of passwords to test",
    )
    usernames: Optional[List[str]] = Field(
        None,
        description="List of usernames to test",
    )
    api_token: Optional[str] = Field(
        None,
        description="WPScan API token for vulnerability database access",
    )
    headers: Optional[str] = Field(
        None,
        description="Custom HTTP headers to include in requests (comma-separated)",
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy to route traffic through (e.g., http://127.0.0.1:8080)",
    )
    disable_tls_checks: bool = Field(
        False,
        description="Disable SSL/TLS certificate validation",
    )
    enumerate_all: bool = Field(
        False,
        description="Enumerate all supported components, plugins, themes, and users",
    )
    random_user_agent: bool = Field(
        False,
        description="Use random user agent",
    )
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string",
    )
    cookies: Optional[str] = Field(
        None,
        description="Cookie string for authentication",
    )
    output_file: Optional[str] = Field(
        None,
        description="Path to save the scan results",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for debugging",
    )
    timeout: int = Field(
        300,
        description="Timeout for WPScan execution in seconds",
        ge=30,
        le=1800,
    )


def parse_wpscan_output(output_text: str) -> Dict[str, Any]:
    """Parse WPScan output into structured metadata."""

    metadata: Dict[str, Any] = {
        "vulnerabilities": [],
        "plugins": [],
        "themes": [],
        "users": [],
        "wordpress_version": None,
        "scan_status": "unknown"
    }
    
    try:
        # Try to parse as JSON first
        if output_text.strip().startswith("{"):
            data = json.loads(output_text)
            metadata.update({
                "vulnerabilities": data.get("vulnerabilities", []),
                "plugins": data.get("plugins", []),
                "themes": data.get("themes", []),
                "users": data.get("users", []),
                "wordpress_version": data.get("version", {}).get("number"),
                "scan_status": data.get("status", "unknown"),
                "scan_duration": data.get("scan_duration", 0),
                "total_issues": len(data.get("vulnerabilities", [])),
                "high_vulnerabilities": len([v for v in data.get("vulnerabilities", []) if v.get("severity") == "High"]),
                "medium_vulnerabilities": len([v for v in data.get("vulnerabilities", []) if v.get("severity") == "Medium"]),
                "low_vulnerabilities": len([v for v in data.get("vulnerabilities", []) if v.get("severity") == "Low"])
            })
        else:
            # Parse text output
            lines = output_text.split("\n")
            for line in lines:
                if "vulnerability" in line.lower():
                    metadata["vulnerabilities"].append(line.strip())
                elif "plugin" in line.lower() and ":" in line:
                    plugin_info = line.split(":", 1)[1].strip()
                    metadata["plugins"].append(plugin_info)
                elif "theme" in line.lower() and ":" in line:
                    theme_info = line.split(":", 1)[1].strip()
                    metadata["themes"].append(theme_info)
                elif "user" in line.lower() and ":" in line:
                    user_info = line.split(":", 1)[1].strip()
                    metadata["users"].append(user_info)
                elif "wordpress version" in line.lower():
                    metadata["wordpress_version"] = line.split(":", 1)[1].strip()
                elif "status:" in line.lower():
                    metadata["scan_status"] = line.split(":", 1)[1].strip()
                    
    except (json.JSONDecodeError, Exception):
        # If parsing fails, extract basic info from text
        metadata["raw_output_length"] = len(output_text)
        metadata["lines_processed"] = len(output_text.split("\n"))
    
    return metadata


class WPScanTool(BaseTool):
    """Run WPScan WordPress security scans and parse the results."""

    args_model = WPScanArgs

    def build_command(self, args: WPScanArgs) -> List[str]:
        """Construct the WPScan command following the execution model."""
        command: List[str] = ["wpscan", "--url", args.target]

        if args.scan_mode != ScanMode.PASSIVE:
            command.extend(["--mode", args.scan_mode.value])

        command.extend(["--format", args.output_format.value])

        enumeration_flags: List[str] = []
        if args.enumerate:
            enumeration_flags.extend(args.enumerate)
        if args.plugins:
            enumeration_flags.append("p")
        if args.themes:
            enumeration_flags.append("t")
        if args.users:
            enumeration_flags.append("u")
        if args.enumerate_all:
            enumeration_flags.extend(["ap", "at", "tt", "cb", "dbe", "p", "t", "u"])
        if enumeration_flags:
            command.extend(["--enumerate", ",".join(sorted(set(enumeration_flags)))])

        if args.passwords:
            command.extend(["--passwords", ",".join(args.passwords)])
        if args.usernames:
            command.extend(["--usernames", ",".join(args.usernames)])
        if args.api_token:
            command.extend(["--api-token", args.api_token])
        if args.headers:
            command.extend(["--headers", args.headers])
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.disable_tls_checks:
            command.append("--disable-tls-checks")
        if args.random_user_agent:
            command.append("--random-user-agent")
        if args.user_agent:
            command.extend(["--user-agent", args.user_agent])
        if args.cookies:
            command.extend(["--cookies", args.cookies])
        if args.output_file:
            command.extend(["--output", args.output_file])
        if args.verbose:
            command.append("--verbose")

        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: WPScanArgs
    ) -> Dict[str, Any]:
        """Parse WPScan output into structured metadata."""
        metadata: Dict[str, Any] = {
            "wordpress_version": None,
            "plugins": [],
            "themes": [],
            "users": [],
            "vulnerabilities": [],
            "exit_code": exit_code,
        }

        if args.output_format == OutputFormat.JSON:
            parsed = parse_json_output(stdout or "", extract_nested=True)
            data = parsed.get("data", [])
            summary = parsed.get("summary", {}) or {}
            primary = data[0] if data else {}

            metadata["wordpress_version"] = (
                summary.get("version")
                or (primary.get("version", {}) if isinstance(primary, dict) else {})
            )
            if isinstance(metadata["wordpress_version"], dict):
                metadata["wordpress_version"] = metadata["wordpress_version"].get(
                    "number"
                )

            for key in ["plugins", "themes", "users"]:
                if isinstance(summary.get(key), list):
                    metadata[key] = summary.get(key, [])
                elif isinstance(primary, dict) and isinstance(primary.get(key), list):
                    metadata[key] = primary.get(key, [])

            vulnerabilities = extract_vulnerabilities(data)
            metadata["vulnerabilities"] = [
                {
                    **vuln,
                    "severity": normalize_severity(vuln.get("severity", "Unknown")),
                }
                for vuln in vulnerabilities
            ]
            if parsed.get("error"):
                metadata["parse_error"] = parsed["error"]
        else:
            metadata.update(parse_wpscan_output(stdout or ""))

        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: WPScanArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist WPScan output when meaningful."""
        if not stdout or len(stdout) <= 100:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/wpscan_{args.scan_mode.value}_{artifact_timestamp}."
            f"{args.output_format.value}"
        )
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: WPScanArgs) -> ToolResult:
        command = self.build_command(args)
        start_time = time.time()
        try:
            process = subprocess.run(
                command,
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
                execution_time=time.time() - start_time,
            )

        metadata = self.parse_output(
            process.stdout, process.stderr, process.returncode, args
        )
        artifacts = self.create_artifacts(process.stdout, args, start_time)

        return ToolResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start_time,
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
        tool_id="web_applications.cms_identification.wpscan",
        display_name="WPScan",
        category=ToolCategory.CMS_IDENTIFICATION,
        applicable_phases=[PentestPhase.ENUMERATION, PentestPhase.VULNERABILITY_ASSESSMENT],
        capabilities=[
            ToolCapability(
                name="wordpress_detection",
                description="Enumerate WordPress core version, themes, plugins, and users on a WordPress target; returns version evidence and known-vulnerability hits",
                output_indicators=["WordPress", "version"],
            ),
            ToolCapability(
                name="wordpress_enumeration",
                description="Enumerate WordPress plugins, themes, and users",
                output_indicators=["plugins", "themes", "users"],
            ),
            ToolCapability(
                name="wordpress_vulnerability_scan",
                description="Scan for known WordPress vulnerabilities using WPVulnDB API",
                output_indicators=["vulnerabilities", "CVE"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)
