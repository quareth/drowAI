"""mitmproxy web application security testing tool using Pydantic models."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import (
    clean_output,
    detect_format,
    parse_json_output,
    parse_xml_output,
    safe_extract,
)


class ProxyMode(str, Enum):
    """Supported mitmproxy modes."""

    REGULAR = "regular"
    TRANSPARENT = "transparent"
    SOCKS = "socks"
    REVERSE = "reverse"


class CaptureMode(str, Enum):
    """mitmproxy capture modes."""

    ALL = "all"
    FILTERED = "filtered"
    NONE = "none"


class MitmProxyArgs(BaseToolArgs):
    """Arguments for the mitmproxy tool."""

    proxy_mode: ProxyMode = Field(
        ProxyMode.REGULAR,
        description="Proxy mode to use (regular, transparent, socks, reverse)",
    )
    capture_mode: CaptureMode = Field(
        CaptureMode.ALL,
        description="Capture mode for traffic (all, filtered, none)",
    )
    port: Optional[int] = Field(
        8080,
        description="Port for mitmproxy to listen on",
    )
    host: Optional[str] = Field(
        "127.0.0.1",
        description="Host address for mitmproxy to bind to",
    )
    upstream_proxy: Optional[str] = Field(
        None,
        description="Optional upstream proxy to chain requests through",
    )
    upstream_auth: Optional[str] = Field(
        None,
        description="Authentication credentials for upstream proxy (user:pass)",
    )
    ignore_hosts: Optional[str] = Field(
        None,
        description="Comma-separated hosts to exclude from capture",
    )
    allow_hosts: Optional[str] = Field(
        None,
        description="Comma-separated hosts to explicitly allow for capture",
    )
    script_file: Optional[str] = Field(
        None,
        description="Path to Python script for custom traffic manipulation",
    )
    addon_paths: Optional[str] = Field(
        None,
        description="Additional addon directories for mitmproxy scripts",
    )
    filter_expression: Optional[str] = Field(
        None,
        description="Filter expression to capture specific traffic",
    )
    headers: Optional[str] = Field(
        None,
        description="Custom headers to inject (comma-separated Header: Value pairs)",
    )
    auth_user: Optional[str] = Field(
        None,
        description="Username for proxy authentication",
    )
    auth_pass: Optional[str] = Field(
        None,
        description="Password for proxy authentication",
    )
    flow_detail: Optional[int] = Field(
        None,
        description="Flow detail level (0-4) for console output and captures",
        ge=0,
        le=4,
    )
    output_format: Literal["json", "xml", "har"] = Field(
        "json",
        description="Output format for the captured traffic",
    )
    output_file: Optional[str] = Field(
        None,
        description="Path to save the captured traffic output",
    )
    ssl_insecure: bool = Field(
        False,
        description="Disable SSL certificate verification",
    )
    ssl_version: Optional[str] = Field(
        None,
        description="Explicit SSL/TLS version (e.g., TLS1.2, TLS1.3)",
    )
    certs: Optional[str] = Field(
        None,
        description="Path to custom certificate file(s) for interception",
    )
    client_certs: Optional[str] = Field(
        None,
        description="Path to client certificates for upstream authentication",
    )
    confdir: Optional[str] = Field(
        None,
        description="Path to mitmproxy configuration directory",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for debugging",
    )
    def _build_auth_value(self) -> Optional[str]:
        if self.auth_user and self.auth_pass:
            return f"{self.auth_user}:{self.auth_pass}"
        if self.auth_user:
            return f"{self.auth_user}:"
        return None

    @field_validator("addon_paths")
    @classmethod
    def _normalize_addon_paths(cls, value: Optional[str]) -> Optional[str]:
        if value and isinstance(value, str):
            return value.strip()
        return value


class MitmProxyTool(BaseTool):
    """Run mitmproxy web application traffic capture and analysis."""

    args_model = MitmProxyArgs

    def supports_pty(self) -> bool:
        """mitmproxy supports interactive PTY execution."""

        return True

    def build_command(self, args: MitmProxyArgs) -> List[str]:
        """Construct the mitmproxy CLI invocation."""

        command: List[str] = ["mitmproxy"]

        if args.proxy_mode != ProxyMode.REGULAR:
            command.extend(["--mode", args.proxy_mode.value])

        if args.upstream_proxy:
            command.extend(["--upstream", args.upstream_proxy])
        if args.upstream_auth:
            command.extend(["--upstream-auth", args.upstream_auth])

        if args.capture_mode != CaptureMode.ALL:
            command.extend(["--set", f"capture_mode={args.capture_mode.value}"])

        if args.host:
            command.extend(["--listen-host", args.host])
        if args.port:
            command.extend(["--listen-port", str(args.port)])

        if args.ignore_hosts:
            command.extend(["--ignore-hosts", args.ignore_hosts])
        if args.allow_hosts:
            command.extend(["--allow-hosts", args.allow_hosts])

        if args.script_file:
            command.extend(["--scripts", args.script_file])
        if args.addon_paths:
            command.extend(["--set", f"addon_paths={args.addon_paths}"])

        if args.filter_expression:
            command.extend(["--set", f"flow_filter={args.filter_expression}"])

        if args.headers:
            command.extend(["--set", f"inject_headers={args.headers}"])

        auth_value = args._build_auth_value()
        if auth_value:
            command.extend(["--set", f"proxyauth={auth_value}"])

        if args.flow_detail is not None:
            command.extend(["--set", f"flow_detail={args.flow_detail}"])

        command.extend(["--set", f"output_format={args.output_format}"])
        if args.output_file:
            command.extend(["--save-stream-file", args.output_file])

        if args.ssl_insecure:
            command.append("--ssl-insecure")
        if args.ssl_version:
            command.extend(["--set", f"ssl_version={args.ssl_version}"])
        if args.certs:
            command.extend(["--certs", args.certs])
        if args.client_certs:
            command.extend(["--set", f"client_certs={args.client_certs}"])

        if args.confdir:
            command.extend(["--confdir", args.confdir])
        if args.verbose:
            command.append("--verbose")

        return command

    def _parse_text_capture(self, output: str) -> Dict[str, Any]:
        """Extract capture statistics from plain text output."""

        metadata: Dict[str, Any] = {
            "requests_captured": 0,
            "responses_captured": 0,
            "ssl_connections": 0,
            "capture_status": "unknown",
            "unique_hosts": 0,
        }
        unique_hosts: set[str] = set()
        for line in output.splitlines():
            lowered = line.lower()
            if "request" in lowered and "captured" in lowered:
                match = re.search(r"(\d+)", line)
                if match:
                    metadata["requests_captured"] = int(match.group(1))
            if "response" in lowered and "captured" in lowered:
                match = re.search(r"(\d+)", line)
                if match:
                    metadata["responses_captured"] = int(match.group(1))
            if "ssl" in lowered and "connection" in lowered:
                match = re.search(r"(\d+)", line)
                if match:
                    metadata["ssl_connections"] = int(match.group(1))
            if "status:" in lowered:
                metadata["capture_status"] = line.split(":", 1)[1].strip()
            host_match = re.search(r"host[:=]\s*([^\s]+)", line, flags=re.IGNORECASE)
            if host_match:
                unique_hosts.add(host_match.group(1))
        metadata["unique_hosts"] = len(unique_hosts)
        return metadata

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: MitmProxyArgs,
    ) -> Dict[str, Any]:
        """Parse mitmproxy output into structured metadata."""

        cleaned_stdout = clean_output(stdout, strip_ansi=True, strip_whitespace=False)
        detected_format = detect_format(cleaned_stdout)
        metadata: Dict[str, Any] = {
            "requests_captured": 0,
            "responses_captured": 0,
            "ssl_connections": 0,
            "capture_status": "unknown",
            "capture_duration": 0,
            "total_traffic": 0,
            "unique_hosts": 0,
            "exit_code": exit_code,
            "output_format_detected": detected_format,
        }

        if stderr:
            metadata["stderr"] = clean_output(stderr)

        if not cleaned_stdout.strip():
            metadata["capture_status"] = "no_output"
            return metadata

        selected_format = detected_format if detected_format != "text" else args.output_format

        try:
            if selected_format == "json":
                json_result = parse_json_output(cleaned_stdout, line_by_line=False)
                summary = json_result.get("summary", {})
                first_entry = json_result.get("data", [{}])[0] if json_result.get("data") else {}
                metadata["requests_captured"] = safe_extract(summary, ["requests_captured"], 0) or safe_extract(first_entry, ["requests"], 0) or 0
                metadata["responses_captured"] = safe_extract(summary, ["responses_captured"], 0) or safe_extract(first_entry, ["responses"], 0) or 0
                metadata["ssl_connections"] = safe_extract(summary, ["ssl_connections"], 0) or safe_extract(first_entry, ["ssl_connections"], 0) or 0
                metadata["capture_status"] = safe_extract(summary, ["status"], "unknown") or safe_extract(first_entry, ["status"], "unknown")
                metadata["capture_duration"] = safe_extract(summary, ["duration"], 0) or safe_extract(first_entry, ["duration"], 0) or 0
                metadata["total_traffic"] = safe_extract(summary, ["total_traffic"], 0) or safe_extract(first_entry, ["total_traffic"], 0) or 0
                hosts = safe_extract(summary, ["hosts"], []) or safe_extract(first_entry, ["hosts"], [])
                if isinstance(hosts, list):
                    metadata["unique_hosts"] = len(set(hosts))
                if json_result.get("error"):
                    metadata["parse_error"] = json_result["error"]
                if json_result.get("error") and detected_format == "text":
                    text_metadata = self._parse_text_capture(cleaned_stdout)
                    metadata.update(text_metadata)
            elif selected_format == "xml":
                xml_result = parse_xml_output(cleaned_stdout)
                metadata["capture_status"] = (
                    xml_result.get("attributes", {}).get("status") or "unknown"
                )
                metadata["xml_elements"] = len(xml_result.get("elements", []))
                if xml_result.get("error"):
                    metadata["parse_error"] = xml_result["error"]
                if xml_result.get("error") and detected_format == "text":
                    text_metadata = self._parse_text_capture(cleaned_stdout)
                    metadata.update(text_metadata)
            else:
                text_metadata = self._parse_text_capture(cleaned_stdout)
                metadata.update(text_metadata)
            metadata["raw_output_length"] = len(cleaned_stdout)
            metadata["lines_processed"] = len(cleaned_stdout.splitlines())
        except Exception as exc:  # pragma: no cover - defensive parsing guard
            metadata["parse_error"] = str(exc)
            metadata["raw_output_length"] = len(cleaned_stdout)
            metadata["lines_processed"] = len(cleaned_stdout.splitlines())

        return metadata

    def create_artifacts(
        self, stdout: str, args: MitmProxyArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist captured traffic to an artifact file."""

        if not stdout:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_basename = (
            args.output_file
            if args.output_file
            else f"artifacts/mitmproxy_{args.proxy_mode.value}_{artifact_timestamp}.{args.output_format}"
        )
        artifact_path = artifact_basename

        try:
            artifact_dir = os.path.dirname(artifact_path) or "."
            os.makedirs(artifact_dir, exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: MitmProxyArgs) -> ToolResult:
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
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_application_proxies.mitmproxy",
        display_name="mitmproxy",
        category=ToolCategory.APPLICATION_PROXY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="traffic_interception",
                description="Intercept and inspect HTTP/HTTPS traffic",
                output_indicators=["requests_captured", "responses_captured"],
            ),
            ToolCapability(
                name="ssl_inspection",
                description="Perform SSL/TLS interception and analysis",
                output_indicators=["ssl_connections"],
            ),
            ToolCapability(
                name="traffic_modification",
                description="Modify requests and responses via scripts or addons",
                output_indicators=["scripts", "addons"],
            ),
            ToolCapability(
                name="traffic_replay",
                description="Capture and replay HTTP flows for analysis",
                output_indicators=["capture_duration", "total_traffic"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=4,
        estimated_runtime_minutes=30,
    )
)