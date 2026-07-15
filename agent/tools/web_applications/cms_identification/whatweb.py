"""WhatWeb v0.6.3 web application fingerprinting tool using native CLI options."""

from __future__ import annotations

import os
import subprocess
import time
import re
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import parse_json_output, clean_output


class AggressionLevel(int, Enum):
    """WhatWeb aggression levels (native v0.6.3 values)."""

    STEALTH = 1
    AGGRESSIVE = 3
    HEAVY = 4


class RedirectMode(str, Enum):
    """WhatWeb redirect handling modes."""

    NEVER = "never"
    HTTP_ONLY = "http-only"
    META_ONLY = "meta-only"
    SAME_SITE = "same-site"
    ALWAYS = "always"


class LogFormat(str, Enum):
    """WhatWeb logging formats (native v0.6.3 flags)."""

    BRIEF = "brief"
    VERBOSE = "verbose"
    ERRORS = "errors"
    XML = "xml"
    JSON = "json"
    SQL = "sql"
    SQL_CREATE = "sql_create"
    JSON_VERBOSE = "json_verbose"
    MAGICTREE = "magictree"
    OBJECT = "object"


LOG_FLAG_BY_FORMAT: Dict[LogFormat, str] = {
    LogFormat.BRIEF: "--log-brief",
    LogFormat.VERBOSE: "--log-verbose",
    LogFormat.ERRORS: "--log-errors",
    LogFormat.XML: "--log-xml",
    LogFormat.JSON: "--log-json",
    LogFormat.SQL: "--log-sql",
    LogFormat.SQL_CREATE: "--log-sql-create",
    LogFormat.JSON_VERBOSE: "--log-json-verbose",
    LogFormat.MAGICTREE: "--log-magictree",
    LogFormat.OBJECT: "--log-object",
}


ARTIFACT_EXT_BY_FORMAT: Dict[LogFormat, str] = {
    LogFormat.BRIEF: "txt",
    LogFormat.VERBOSE: "txt",
    LogFormat.ERRORS: "txt",
    LogFormat.XML: "xml",
    LogFormat.JSON: "json",
    LogFormat.SQL: "sql",
    LogFormat.SQL_CREATE: "sql",
    LogFormat.JSON_VERBOSE: "json",
    LogFormat.MAGICTREE: "xml",
    LogFormat.OBJECT: "txt",
}


class WhatWebArgs(BaseToolArgs):
    """Arguments for the WhatWeb v0.6.3 tool."""

    aggression_level: AggressionLevel = Field(
        AggressionLevel.STEALTH,
        description="Aggression level (native WhatWeb levels: 1=stealth, 3=aggressive, 4=heavy)",
    )
    plugins: Optional[List[str]] = Field(
        None,
        description="WhatWeb plugin list for --plugins/-p (supports native + and - modifiers)",
    )
    user_agent: Optional[str] = Field(
        None,
        description="Custom user agent string",
    )
    headers: Optional[List[str]] = Field(
        None,
        description='Custom headers for repeated --header usage, e.g. ["X-Test: 1", "User-Agent: Custom"]',
    )
    user: Optional[str] = Field(
        None,
        description='HTTP authentication in "user:pass" format',
    )
    cookie: Optional[str] = Field(
        None,
        description="Cookie string, e.g. 'name=value; name2=value2'",
    )
    cookie_jar: Optional[str] = Field(
        None,
        description="Path to cookie jar file for --cookie-jar",
    )
    no_cookies: bool = Field(
        False,
        description="Disable automatic cookie handling",
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy host[:port]",
    )
    proxy_user: Optional[str] = Field(
        None,
        description='Proxy authentication in "user:pass" format',
    )
    follow_redirect: RedirectMode = Field(
        RedirectMode.ALWAYS,
        description="Redirect mode for --follow-redirect",
    )
    max_redirects: int = Field(
        10,
        description="Maximum number of redirects to follow",
        ge=0,
        le=100,
    )
    log_format: LogFormat = Field(
        LogFormat.JSON,
        description="WhatWeb logging format (mapped to native --log-* flags)",
    )
    log_destination: str = Field(
        "-",
        description='WhatWeb log destination. Use "-" for stdout or provide a file path.',
    )
    max_threads: int = Field(
        25,
        description="Maximum threads (--max-threads)",
        ge=1,
        le=2000,
    )
    open_timeout: int = Field(
        15,
        description="Connection open timeout (--open-timeout)",
        ge=1,
        le=600,
    )
    read_timeout: int = Field(
        30,
        description="Read timeout (--read-timeout)",
        ge=1,
        le=600,
    )
    wait: Optional[float] = Field(
        None,
        description="Seconds to wait between connections (--wait)",
        ge=0,
        le=300,
    )
    no_errors: bool = Field(
        False,
        description="Suppress error messages",
    )
    quiet: bool = Field(
        False,
        description="Suppress brief stdout logging",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    timeout: int = Field(
        60,
        description="Subprocess timeout in seconds for WhatWeb execution",
        ge=10,
        le=600,
    )


def parse_whatweb_output(output_text: str) -> Dict[str, Any]:
    """Parse WhatWeb output (prefers JSON log schema, falls back to text parsing)."""

    metadata: Dict[str, Any] = {
        "technologies": [],
        "cms_detected": [],
        "web_servers": [],
        "frameworks": [],
        "languages": [],
        "scan_status": "unknown",
    }

    cleaned = clean_output(output_text or "")
    if not cleaned:
        return metadata

    def _categorize(technology_entry: Dict[str, Any]) -> None:
        name = str(technology_entry.get("name", "")).lower()
        if any(cms in name for cms in ("wordpress", "joomla", "drupal", "magento", "silverstripe")):
            metadata["cms_detected"].append(technology_entry)
        if any(server in name for server in ("apache", "nginx", "iis", "lighttpd", "tomcat", "caddy")):
            metadata["web_servers"].append(technology_entry)
        if any(framework in name for framework in ("rails", "django", "laravel", "spring", "express", "asp.net")):
            metadata["frameworks"].append(technology_entry)
        if any(language in name for language in ("php", "python", "ruby", "java", "node", "perl")):
            metadata["languages"].append(technology_entry)

    parsed_json = parse_json_output(cleaned, extract_nested=False)
    json_data = parsed_json.get("data", [])
    if json_data and not parsed_json.get("error"):
        metadata["scan_status"] = "parsed_json"
        metadata["target_count"] = len(json_data)
        for target_entry in json_data:
            if not isinstance(target_entry, dict):
                continue
            plugins = target_entry.get("plugins", {})
            if not isinstance(plugins, dict):
                continue
            for plugin_name, plugin_data in plugins.items():
                technology: Dict[str, Any] = {
                    "name": str(plugin_name),
                    "target": target_entry.get("target"),
                    "http_status": target_entry.get("http_status"),
                }
                if isinstance(plugin_data, dict):
                    if "version" in plugin_data:
                        technology["version"] = plugin_data.get("version")
                    if plugin_data:
                        technology["details"] = plugin_data
                elif plugin_data is not None:
                    technology["details"] = plugin_data

                metadata["technologies"].append(technology)
                _categorize(technology)
        return metadata

    metadata["scan_status"] = "parsed_text"
    seen_names = set()
    for line in cleaned.splitlines():
        for raw_name in re.findall(r"([A-Za-z0-9_.:-]+)\[", line):
            lowered = raw_name.lower()
            if lowered in seen_names:
                continue
            seen_names.add(lowered)
            technology = {"name": raw_name}
            metadata["technologies"].append(technology)
            _categorize(technology)

    if not metadata["technologies"]:
        metadata["raw_output_length"] = len(cleaned)
        metadata["lines_processed"] = len(cleaned.splitlines())

    return metadata


class WhatWebTool(BaseTool):
    """Run WhatWeb web application fingerprinting and parse the results."""

    args_model = WhatWebArgs

    def build_command(self, args: WhatWebArgs) -> List[str]:
        """Construct the WhatWeb command using native v0.6.3 CLI options."""
        command: List[str] = ["whatweb", "-a", str(int(args.aggression_level.value))]

        if args.plugins:
            command.extend(["--plugins", ",".join(args.plugins)])
        if args.headers:
            for header_value in args.headers:
                command.extend(["--header", header_value])
        if args.user_agent:
            command.extend(["--user-agent", args.user_agent])
        if args.user:
            command.extend(["--user", args.user])
        if args.cookie:
            command.extend(["--cookie", args.cookie])
        if args.cookie_jar:
            command.extend(["--cookie-jar", args.cookie_jar])
        if args.no_cookies:
            command.append("--no-cookies")
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.proxy_user:
            command.extend(["--proxy-user", args.proxy_user])

        command.append(f"--follow-redirect={args.follow_redirect.value}")
        command.extend(["--max-redirects", str(args.max_redirects)])
        command.extend(["--max-threads", str(args.max_threads)])
        command.extend(["--open-timeout", str(args.open_timeout)])
        command.extend(["--read-timeout", str(args.read_timeout)])
        if args.wait is not None:
            command.append(f"--wait={args.wait}")

        log_flag = LOG_FLAG_BY_FORMAT[args.log_format]
        command.append(f"{log_flag}={args.log_destination}")

        if args.no_errors:
            command.append("--no-errors")
        if args.quiet or args.log_destination == "-":
            command.append("--quiet")
        if args.verbose:
            command.append("--verbose")

        command.append(args.target)
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: WhatWebArgs
    ) -> Dict[str, Any]:
        """Parse WhatWeb output into structured metadata."""
        metadata = parse_whatweb_output(stdout or "")
        metadata["exit_code"] = exit_code

        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: WhatWebArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist WhatWeb output and include native log files when requested."""
        artifacts: List[str] = []

        if args.log_destination and args.log_destination != "-" and os.path.exists(args.log_destination):
            artifacts.append(args.log_destination)

        if stdout and len(stdout) > 100:
            artifact_timestamp = int(timestamp or time.time())
            extension = ARTIFACT_EXT_BY_FORMAT[args.log_format]
            artifact_path = (
                f"artifacts/whatweb_a{int(args.aggression_level.value)}_{artifact_timestamp}."
                f"{extension}"
            )
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass

        return artifacts

    def run(self, args: WhatWebArgs) -> ToolResult:
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
        tool_id="web_applications.cms_identification.whatweb",
        display_name="WhatWeb",
        category=ToolCategory.CMS_IDENTIFICATION,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="technology_fingerprinting",
                description="Fingerprint web technologies, frameworks, servers, plugins, and versions from HTTP responses; not for path discovery or vulnerability scanning",
                output_indicators=["Detected"],
            ),
            ToolCapability(
                name="cms_detection",
                description="Detect CMS platforms and versions",
                output_indicators=["wordpress", "joomla", "drupal", "magento"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        batch_audited=True,
        stealth_level=3,
        estimated_runtime_minutes=3,
    )
)
