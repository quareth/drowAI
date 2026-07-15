"""Droopescan - Drupal Security Scanner."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import (
    parse_json_output,
    extract_vulnerabilities,
    normalize_severity,
    clean_output,
)


class OutputFormat(str, Enum):
    """Output format options for Droopescan."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class ScanType(str, Enum):
    """Droopescan scan types."""
    ENUMERATE = "enumerate"
    VERSION = "version"
    PLUGINS = "plugins"
    THEMES = "themes"
    USERS = "users"
    INTERESTING_URLS = "interesting_urls"


class DroopescanArgs(BaseToolArgs):
    """Arguments for the Droopescan tool."""

    cms_type: Literal["drupal", "moodle", "silverstripe"] = Field(
        default="drupal",
        description="Target CMS type supported by Droopescan",
    )
    scan_type: ScanType = Field(
        default=ScanType.ENUMERATE,
        description="Type of scan to perform (enumerate, version, plugins, themes, users, interesting_urls)",
    )

    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for the tool"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    timeout: int = Field(
        default=300,
        description="Timeout in seconds for the scan",
        ge=30,
        le=3600
    )
    
    threads: int = Field(
        default=10,
        description="Number of threads to use",
        ge=1,
        le=50
    )
    
    exploit: bool = Field(
        default=False,
        description="Attempt exploitation after enumeration"
    )
    
    custom_payload: Optional[str] = Field(
        None,
        description="Custom payload to use for exploitation"
    )
    
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string"
    )
    authentication: Optional[str] = Field(
        None,
        description='HTTP authentication credentials in "user:pass" format',
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy to route traffic through (e.g., http://127.0.0.1:8080)",
    )
    enumerate_plugins: bool = Field(
        default=False,
        description="Enumerate CMS plugins/modules",
    )
    enumerate_themes: bool = Field(
        default=False,
        description="Enumerate CMS themes",
    )


def parse_droopescan_output(output_text: str) -> Dict[str, Any]:
    """Parse Droopescan output into structured metadata."""
    metadata: Dict[str, Any] = {
        "drupal_detected": False,
        "version_found": "unknown",
        "vulnerabilities_found": 0,
        "modules_found": 0,
        "themes_found": 0,
        "users_found": 0,
        "execution_status": "unknown"
    }
    
    try:
        # Parse common Droopescan output patterns
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Look for Drupal detection
            if "drupal" in line.lower():
                metadata["drupal_detected"] = True
                
            # Look for version information
            if "version" in line.lower():
                metadata["version_found"] = line.split(":")[-1].strip()
                
            # Look for vulnerability indicators
            if any(indicator in line.lower() for indicator in [
                "vulnerable", "exploit", "cve", "vulnerability"
            ]):
                metadata["vulnerabilities_found"] += 1
                
            # Look for module information
            if "module" in line.lower():
                metadata["modules_found"] += 1
                
            # Look for theme information
            if "theme" in line.lower():
                metadata["themes_found"] += 1
                
            # Look for user information
            if "user" in line.lower():
                metadata["users_found"] += 1
                
            # Look for execution status
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
                
    except Exception:
        # If parsing fails, return basic metadata
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class DroopescanTool(BaseTool):
    """Run Droopescan Drupal security scanning and exploitation."""
    
    args_model = DroopescanArgs
    
    def build_command(self, args: DroopescanArgs) -> List[str]:
        """Construct the Droopescan command following the execution model."""
        command: List[str] = ["droopescan"]

        command.extend(["-s", args.scan_type.value])
        command.extend(["-c", args.cms_type])

        if args.output_format != OutputFormat.TEXT:
            command.extend(["-o", args.output_format.value])
        if args.verbose:
            command.append("-v")
        if args.threads != 10:
            command.extend(["-t", str(args.threads)])
        if args.exploit:
            command.append("-e")
        if args.custom_payload:
            command.extend(["-p", args.custom_payload])
        if args.user_agent:
            command.extend(["-u", args.user_agent])
        if args.authentication:
            command.extend(["-a", args.authentication])
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.enumerate_plugins:
            command.append("--plugins")
        if args.enumerate_themes:
            command.append("--themes")

        command.append(args.target)
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: DroopescanArgs
    ) -> Dict[str, Any]:
        """Parse Droopescan output into structured metadata."""
        metadata: Dict[str, Any] = {
            "cms_type": args.cms_type,
            "version_found": None,
            "modules": [],
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

            metadata["version_found"] = summary.get("version") or (
                primary.get("version") if isinstance(primary, dict) else None
            )
            metadata["modules"] = summary.get("modules", []) or (
                primary.get("modules", []) if isinstance(primary, dict) else []
            )
            metadata["themes"] = summary.get("themes", []) or (
                primary.get("themes", []) if isinstance(primary, dict) else []
            )
            metadata["users"] = summary.get("users", []) or (
                primary.get("users", []) if isinstance(primary, dict) else []
            )

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
            metadata.update(parse_droopescan_output(stdout or ""))

        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: DroopescanArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist Droopescan output when meaningful."""
        if not stdout or len(stdout) <= 100:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/droopescan_{args.scan_type.value}_{artifact_timestamp}."
            f"{args.output_format.value}"
        )
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: DroopescanArgs) -> ToolResult:
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
        tool_id="web_applications.cms_identification.droopescan",
        display_name="Droopescan",
        category=ToolCategory.CMS_IDENTIFICATION,
        applicable_phases=[PentestPhase.ENUMERATION, PentestPhase.VULNERABILITY_ASSESSMENT],
        capabilities=[
            ToolCapability(
                name="drupal_detection",
                description="Enumerate Drupal, Moodle, or SilverStripe core version, modules, themes, and users on the matching CMS target; returns version and module evidence",
                output_indicators=["Drupal", "Moodle", "version"],
            ),
            ToolCapability(
                name="cms_enumeration",
                description="Enumerate CMS modules, themes, and users",
                output_indicators=["modules", "themes", "users"],
            ),
            ToolCapability(
                name="cms_vulnerability_scan",
                description="Scan for known CMS vulnerabilities",
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
