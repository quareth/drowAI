"""JoomScan Joomla security scanner tool using Pydantic models."""

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


class ScanType(str, Enum):
    """Supported JoomScan scan types."""

    BASIC = "basic"  # Quick scan for core info
    FULL = "full"  # Full enumeration of components, plugins, modules, templates
    VULNERABILITIES = "vulnerabilities"  # Vulnerability focused scan
    COMPONENTS = "components"  # Component enumeration only


class OutputFormat(str, Enum):
    """JoomScan output formats."""

    JSON = "json"
    XML = "xml"
    CSV = "csv"
    TEXT = "text"


class JoomScanArgs(BaseToolArgs):
    """Arguments for the JoomScan tool."""

    scan_type: ScanType = Field(
        ScanType.BASIC,
        description="Scan type to perform (basic, full, vulnerabilities, components)",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for results (json, xml, csv, text)",
    )
    enumerate_components: bool = Field(
        False,
        description="Enumerate Joomla components",
    )
    enumerate_plugins: bool = Field(
        False,
        description="Enumerate Joomla plugins",
    )
    enumerate_modules: bool = Field(
        False,
        description="Enumerate Joomla modules",
    )
    enumerate_templates: bool = Field(
        False,
        description="Enumerate Joomla templates",
    )
    random_user_agent: bool = Field(
        False,
        description="Use a random user agent for requests",
    )
    authentication: Optional[str] = Field(
        None,
        description='HTTP authentication credentials in "user:pass" format',
    )
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string",
    )
    cookies: Optional[str] = Field(
        None,
        description="Cookie string for authentication",
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy server to use (host:port)",
    )
    headers: Optional[str] = Field(
        None,
        description="Custom HTTP headers to include (comma-separated)",
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
        description="Timeout for JoomScan execution in seconds",
        ge=30,
        le=1800,
    )


def parse_joomscan_output(output_text: str) -> Dict[str, Any]:
    """Parse JoomScan output into structured metadata."""

    metadata: Dict[str, Any] = {
        "vulnerabilities": [],
        "components": [],
        "plugins": [],
        "modules": [],
        "templates": [],
        "joomla_version": None,
        "scan_status": "unknown"
    }
    
    try:
        # Try to parse as JSON first
        if output_text.strip().startswith("{"):
            data = json.loads(output_text)
            metadata.update({
                "vulnerabilities": data.get("vulnerabilities", []),
                "components": data.get("components", []),
                "plugins": data.get("plugins", []),
                "modules": data.get("modules", []),
                "templates": data.get("templates", []),
                "joomla_version": data.get("version", {}).get("number"),
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
                elif "component" in line.lower() and ":" in line:
                    component_info = line.split(":", 1)[1].strip()
                    metadata["components"].append(component_info)
                elif "plugin" in line.lower() and ":" in line:
                    plugin_info = line.split(":", 1)[1].strip()
                    metadata["plugins"].append(plugin_info)
                elif "module" in line.lower() and ":" in line:
                    module_info = line.split(":", 1)[1].strip()
                    metadata["modules"].append(module_info)
                elif "template" in line.lower() and ":" in line:
                    template_info = line.split(":", 1)[1].strip()
                    metadata["templates"].append(template_info)
                elif "joomla version" in line.lower():
                    metadata["joomla_version"] = line.split(":", 1)[1].strip()
                elif "status:" in line.lower():
                    metadata["scan_status"] = line.split(":", 1)[1].strip()
                    
    except (json.JSONDecodeError, Exception):
        # If parsing fails, extract basic info from text
        metadata["raw_output_length"] = len(output_text)
        metadata["lines_processed"] = len(output_text.split("\n"))
    
    return metadata


class JoomScanTool(BaseTool):
    """Run JoomScan Joomla security scans and parse the results."""

    args_model = JoomScanArgs

    def build_command(self, args: JoomScanArgs) -> List[str]:
        """Construct the JoomScan command following the execution model."""
        command: List[str] = ["joomscan", "--type", args.scan_type.value]

        command.extend(["--format", args.output_format.value])

        if args.enumerate_components:
            command.append("--enumerate-components")
        if args.enumerate_plugins:
            command.append("--enumerate-plugins")
        if args.enumerate_modules:
            command.append("--enumerate-modules")
        if args.enumerate_templates:
            command.append("--enumerate-templates")
        if args.random_user_agent:
            command.append("--random-agent")
        if args.authentication:
            command.extend(["--auth", args.authentication])
        if args.user_agent:
            command.extend(["--user-agent", args.user_agent])
        if args.cookies:
            command.extend(["--cookies", args.cookies])
        if args.headers:
            command.extend(["--headers", args.headers])
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.output_file:
            command.extend(["--output", args.output_file])
        if args.verbose:
            command.append("--verbose")

        command.append(args.target)
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: JoomScanArgs
    ) -> Dict[str, Any]:
        """Parse JoomScan output into structured metadata."""
        metadata: Dict[str, Any] = {
            "joomla_version": None,
            "components": [],
            "plugins": [],
            "modules": [],
            "templates": [],
            "vulnerabilities": [],
            "exit_code": exit_code,
        }

        if args.output_format == OutputFormat.JSON:
            parsed = parse_json_output(stdout or "", extract_nested=True)
            data = parsed.get("data", [])
            summary = parsed.get("summary", {}) or {}
            primary = data[0] if data else {}

            metadata["joomla_version"] = summary.get("version") or (
                primary.get("version", {}).get("number")
                if isinstance(primary, dict) and isinstance(primary.get("version"), dict)
                else None
            )
            for key in ["components", "plugins", "modules", "templates"]:
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
            metadata.update(parse_joomscan_output(stdout or ""))

        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: JoomScanArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist JoomScan output when meaningful."""
        if not stdout or len(stdout) <= 100:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/joomscan_{args.scan_type.value}_{artifact_timestamp}."
            f"{args.output_format.value}"
        )
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: JoomScanArgs) -> ToolResult:
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
        tool_id="web_applications.cms_identification.joomscan",
        display_name="JoomScan",
        category=ToolCategory.CMS_IDENTIFICATION,
        applicable_phases=[PentestPhase.ENUMERATION, PentestPhase.VULNERABILITY_ASSESSMENT],
        capabilities=[
            ToolCapability(
                name="joomla_detection",
                description="Enumerate Joomla core version, components, plugins, modules, and templates on a Joomla target; returns version and component evidence",
                output_indicators=["Joomla", "version"],
            ),
            ToolCapability(
                name="joomla_enumeration",
                description="Enumerate Joomla components, plugins, modules, and templates",
                output_indicators=["components", "plugins", "modules", "templates"],
            ),
            ToolCapability(
                name="joomla_vulnerability_scan",
                description="Scan for known Joomla vulnerabilities",
                output_indicators=["vulnerabilities", "CVE"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=8,
    )
)
