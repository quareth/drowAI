"""CMSmap - CMS Vulnerability Scanner."""

from __future__ import annotations

import os
import subprocess
import time
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


class OutputFormat(str, Enum):
    """Output format options for CMSmap."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class CMSType(str, Enum):
    """Supported CMS types for CMSmap."""
    WORDPRESS = "wordpress"
    JOOMLA = "joomla"
    DRUPAL = "drupal"
    MAGENTO = "magento"
    OPENCART = "opencart"
    PRESTASHOP = "prestashop"
    TYPECHO = "typecho"
    DEDECMS = "dedecms"
    PHPCMS = "phpcms"
    DISCUZ = "discuz"


class CMSmapArgs(BaseToolArgs):
    """Arguments for the CMSmap tool."""
    
    cms_type: CMSType = Field(
        default=CMSType.WORDPRESS,
        description="Type of CMS to scan"
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
        description="Enumerate plugins/extensions",
    )
    enumerate_themes: bool = Field(
        default=False,
        description="Enumerate themes/templates",
    )
    enumerate_users: bool = Field(
        default=False,
        description="Enumerate CMS users",
    )
    brute_force: bool = Field(
        default=False,
        description="Attempt brute force attacks against the CMS",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Wordlist to use when brute_force is enabled",
    )


def parse_cmsmap_output(output_text: str) -> Dict[str, Any]:
    """Parse CMSmap output into structured metadata."""
    metadata: Dict[str, Any] = {
        "cms_detected": "unknown",
        "version_found": "unknown",
        "vulnerabilities_found": 0,
        "plugins_found": 0,
        "themes_found": 0,
        "execution_status": "unknown"
    }
    
    try:
        # Parse common CMSmap output patterns
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Look for CMS detection
            if any(cms in line.lower() for cms in [
                "wordpress", "joomla", "drupal", "magento"
            ]):
                metadata["cms_detected"] = line.split(":")[-1].strip()
                
            # Look for version information
            if "version" in line.lower():
                metadata["version_found"] = line.split(":")[-1].strip()
                
            # Look for vulnerability indicators
            if any(indicator in line.lower() for indicator in [
                "vulnerable", "exploit", "cve", "vulnerability"
            ]):
                metadata["vulnerabilities_found"] += 1
                
            # Look for plugin information
            if "plugin" in line.lower():
                metadata["plugins_found"] += 1
                
            # Look for theme information
            if "theme" in line.lower():
                metadata["themes_found"] += 1
                
            # Look for execution status
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
                
    except Exception:
        # If parsing fails, return basic metadata
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class CMSmapTool(BaseTool):
    """Run CMSmap CMS vulnerability scanning and exploitation."""
    
    args_model = CMSmapArgs
    
    def build_command(self, args: CMSmapArgs) -> List[str]:
        """Construct the CMSmap command following the execution model."""
        command: List[str] = ["cmsmap", "-t", args.cms_type.value]

        if args.output_format != OutputFormat.TEXT:
            command.extend(["-o", args.output_format.value])
        if args.verbose:
            command.append("-v")
        if args.threads != 10:
            command.extend(["-T", str(args.threads)])
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
        if args.enumerate_users:
            command.append("--users")
        if args.brute_force:
            command.append("--bruteforce")
            if args.wordlist:
                command.extend(["-w", args.wordlist])

        command.append(args.target)
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: CMSmapArgs
    ) -> Dict[str, Any]:
        """Parse CMSmap output into structured metadata."""
        metadata: Dict[str, Any] = {
            "cms_detected": args.cms_type.value,
            "version_found": None,
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

            metadata["cms_detected"] = summary.get("cms") or metadata["cms_detected"]
            metadata["version_found"] = summary.get("version") or (
                primary.get("version") if isinstance(primary, dict) else None
            )
            metadata["plugins"] = summary.get("plugins", []) or (
                primary.get("plugins", []) if isinstance(primary, dict) else []
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
            metadata.update(parse_cmsmap_output(stdout or ""))

        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: CMSmapArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist CMSmap output when meaningful."""
        if not stdout or len(stdout) <= 100:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/cmsmap_{args.cms_type.value}_{artifact_timestamp}."
            f"{args.output_format.value}"
        )
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: CMSmapArgs) -> ToolResult:
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
        tool_id="web_applications.cms_identification.cmsmap",
        display_name="CMSmap",
        category=ToolCategory.CMS_IDENTIFICATION,
        applicable_phases=[
            PentestPhase.ENUMERATION,
            PentestPhase.VULNERABILITY_ASSESSMENT,
            PentestPhase.EXPLOITATION,
        ],
        capabilities=[
            ToolCapability(
                name="multi_cms_detection",
                description="Detect WordPress, Joomla, Drupal, or Moodle, then enumerate plugins, themes, and users; use for multi-CMS triage, not WordPress-only depth",
                output_indicators=["WordPress", "Joomla", "Drupal", "Magento"],
            ),
            ToolCapability(
                name="cms_enumeration",
                description="Enumerate CMS plugins, themes, and users",
                output_indicators=["plugins", "themes", "users"],
            ),
            ToolCapability(
                name="cms_exploitation",
                description="Attempt exploitation of detected vulnerabilities",
                output_indicators=["exploit", "shell"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=15,
    )
)
