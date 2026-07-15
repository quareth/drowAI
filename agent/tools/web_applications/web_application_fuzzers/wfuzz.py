"""WFuzz web application fuzzing tool (fuzzer mode) - focuses on parameter fuzzing, response filtering, and scan mode. For directory enumeration, see web_crawlers.wfuzz."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import ConfigDict, Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from .._path_safety import resolve_wordlist_path_for_execution
from ..parsing_utils import (
    clean_output,
    parse_csv_output,
    parse_json_output,
    parse_xml_output,
)
from ...enhanced_metadata_registry import (
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
    register_enhanced_tool_metadata,
)


class WfuzzMethod(str, Enum):
    """WFuzz HTTP methods."""
    
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"


class OutputFormat(str, Enum):
    """WFuzz output format options."""
    
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    TEXT = "text"


class WfuzzArgs(BaseToolArgs):
    """Arguments for the WFuzz tool."""

    model_config = ConfigDict(extra="forbid")

    method: WfuzzMethod = Field(
        WfuzzMethod.GET,
        description="HTTP method to use for requests",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Wordlist file to use for fuzzing",
    )
    payload: Optional[str] = Field(
        None,
        description="Payload specification (e.g., 'FUZZ', 'FUZ2Z')",
    )
    filter: Optional[str] = Field(
        None,
        description="Wfuzz filter expression (e.g., 'c!=404', 'w>100')",
    )
    threads: int = Field(
        10,
        ge=1,
        le=50,
        description="Number of concurrent threads",
    )
    delay: Optional[float] = Field(
        None,
        ge=0.0,
        le=10.0,
        description="Delay between requests in seconds",
    )
    timeout: int = Field(
        30,
        ge=1,
        le=300,
        description="Request timeout in seconds",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects",
    )
    cookies: Optional[str] = Field(
        None,
        description="HTTP cookies",
    )
    headers: Optional[str] = Field(
        None,
        description="HTTP headers (comma-separated)",
    )
    data: Optional[str] = Field(
        None,
        description="POST data",
    )
    auth: Optional[str] = Field(
        None,
        description="HTTP authentication (user:pass)",
    )
    auth_type: Literal["basic", "digest", "ntlm"] = Field(
        "basic",
        description="Authentication mechanism used with auth credentials",
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy URL",
    )
    user_agent: Optional[str] = Field(
        None,
        description="HTTP User-Agent header",
    )
    verbose: bool = Field(
        False,
        description="Verbose output",
    )
    scan_mode: bool = Field(
        False,
        description="Scan mode (directory/file discovery)",
    )
    recursive: bool = Field(
        False,
        description="Enable recursion",
    )
    depth: int = Field(
        3,
        ge=1,
        le=10,
        description="Recursion depth",
    )
    match: Optional[str] = Field(
        None,
        description="Show responses containing this regex (Wfuzz --ss)",
    )
    not_match: Optional[str] = Field(
        None,
        description="Hide responses containing this regex (Wfuzz --hs)",
    )
    hc: Optional[str] = Field(
        None,
        description="Hide responses with specified codes",
    )
    hw: Optional[str] = Field(
        None,
        description="Hide responses with specified words",
    )
    hl: Optional[str] = Field(
        None,
        description="Hide responses with specified lines",
    )


def parse_wfuzz_json(json_text: str) -> Dict[str, Any]:
    """Parse WFuzz JSON output into structured metadata using shared helpers."""

    parsed = parse_json_output(json_text or "")
    metadata: Dict[str, Any] = {
        "requests": [],
        "statistics": {},
        "filters": {},
        "raw_output": parsed.get("raw_output"),
    }

    for entry in parsed.get("data", []):
        if isinstance(entry, dict):
            if isinstance(entry.get("results"), list):
                metadata["requests"].extend(entry["results"])
            else:
                metadata["requests"].append(entry)
            if isinstance(entry.get("statistics"), dict):
                metadata["statistics"].update(entry["statistics"])
            if isinstance(entry.get("filters"), dict):
                metadata["filters"].update(entry["filters"])

    if not metadata["requests"] and parsed.get("data"):
        metadata["requests"] = parsed.get("data", [])

    if parsed.get("summary"):
        metadata["summary"] = parsed["summary"]
    if parsed.get("error"):
        metadata["error"] = parsed["error"]

    return metadata


def parse_wfuzz_text(text_output: str) -> Dict[str, Any]:
    """Parse WFuzz text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "requests": [],
        "statistics": {},
        "raw_output": text_output,
    }
    
    lines = text_output.split('\n')
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines and decorative headers
        if not line or line.startswith('='):
            continue
        
        # Parse response lines (format: Code Lines Word Chars Request)
        parts = line.split()
        if len(parts) >= 5:
            try:
                metadata["requests"].append({
                    "status_code": int(parts[0]),
                    "lines": int(parts[1]),
                    "words": int(parts[2]),
                    "chars": int(parts[3]),
                    "url": ' '.join(parts[4:]),
                })
            except (ValueError, IndexError):
                continue
        
        # Look for statistics
        if 'Total requests:' in line:
            metadata["statistics"]["total_requests"] = line.split(':')[1].strip()
        elif 'Filtered requests:' in line:
            metadata["statistics"]["filtered_requests"] = line.split(':')[1].strip()
        elif 'Requests/sec:' in line:
            metadata["statistics"]["requests_per_sec"] = line.split(':')[1].strip()
    
    return metadata


class WfuzzTool(BaseTool):
    """Run WFuzz and parse the results."""

    args_model = WfuzzArgs

    def build_command(self, args: WfuzzArgs) -> List[str]:
        """Construct wfuzz command arguments for execution-model support."""

        command: List[str] = ["wfuzz"]

        command.extend(["-X", args.method.value])

        if args.wordlist:
            command.extend(["-w", resolve_wordlist_path_for_execution(args.wordlist)])

        if args.payload:
            command.extend(["-z", args.payload])

        if args.filter:
            command.extend(["--filter", args.filter])

        command.extend(["-t", str(args.threads)])

        if args.delay:
            command.extend(["-s", str(args.delay)])

        command.extend(["--req-delay", str(args.timeout)])

        if args.follow_redirects:
            command.append("--follow")

        if args.cookies:
            command.extend(["-b", args.cookies])

        if args.headers:
            command.extend(["-H", args.headers])

        if args.data:
            command.extend(["-d", args.data])

        if args.auth:
            command.extend([f"--{args.auth_type}", args.auth])

        if args.proxy:
            command.extend(["-p", args.proxy])

        if args.user_agent:
            command.extend(["-H", f"User-Agent: {args.user_agent}"])

        if args.verbose:
            command.append("-v")

        if args.recursive:
            command.extend(["-R", str(args.depth)])

        if args.scan_mode:
            command.append("-Z")

        if args.match:
            command.extend(["--ss", args.match])
        if args.not_match:
            command.extend(["--hs", args.not_match])

        if args.hc:
            command.extend(["--hc", args.hc])
        if args.hw:
            command.extend(["--hw", args.hw])
        if args.hl:
            command.extend(["--hl", args.hl])

        # Wfuzz uses ``-o printer`` to select the stdout printer (man page).
        # ``-f filename,printer`` is reserved for writing results to a file
        # path; we don't expose a real output file here, so route formatting
        # through ``-o``. ``raw`` is the documented text printer.
        if args.output_format == OutputFormat.JSON:
            command.extend(["-o", "json"])
        elif args.output_format == OutputFormat.CSV:
            command.extend(["-o", "csv"])
        elif args.output_format == OutputFormat.XML:
            command.extend(["-o", "xml"])
        else:
            command.extend(["-o", "raw"])

        command.append(args.target)
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: WfuzzArgs
    ) -> Dict[str, Any]:
        """Parse wfuzz output into structured metadata."""

        metadata: Dict[str, Any] = {
            "requests": [],
            "statistics": {},
            "filters": {},
            "output_format": args.output_format.value,
            "exit_code": exit_code,
        }

        if not stdout or not stdout.strip():
            if stderr:
                metadata["stderr"] = clean_output(stderr)
            return metadata

        if args.output_format == OutputFormat.JSON:
            metadata.update(parse_wfuzz_json(stdout))
        elif args.output_format == OutputFormat.CSV:
            parsed_csv = parse_csv_output(stdout)
            metadata["requests"] = parsed_csv.get("rows", [])
            metadata["statistics"]["row_count"] = parsed_csv.get("row_count", 0)
            if parsed_csv.get("headers"):
                metadata["filters"]["headers"] = parsed_csv["headers"]
            if parsed_csv.get("raw_output"):
                metadata["raw_output"] = parsed_csv["raw_output"]
            if parsed_csv.get("error"):
                metadata["error"] = parsed_csv["error"]
        elif args.output_format == OutputFormat.XML:
            parsed_xml = parse_xml_output(stdout)
            metadata["requests"] = parsed_xml.get("elements", [])
            if parsed_xml.get("attributes"):
                metadata["filters"]["attributes"] = parsed_xml["attributes"]
            if parsed_xml.get("raw_output"):
                metadata["raw_output"] = parsed_xml["raw_output"]
            if parsed_xml.get("error"):
                metadata["error"] = parsed_xml["error"]
        else:
            metadata.update(parse_wfuzz_text(stdout))

        if stderr:
            metadata["stderr"] = clean_output(stderr)

        return metadata

    def create_artifacts(
        self, stdout: str, args: WfuzzArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist wfuzz output when available."""

        if not stdout:
            return []

        artifact_timestamp = int(timestamp or time.time())
        artifact_path = f"artifacts/wfuzz_{artifact_timestamp}.{args.output_format.value}"
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: WfuzzArgs) -> ToolResult:
        command = self.build_command(args)
        start_time = time.time()

        try:
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=args.timeout
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
            success=self.is_success_exit_code(process.returncode, args),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start_time,
        )


# ---------------------------------------------------------------------------
# Tool Metadata Registration (fuzzer focus; enumeration variant lives in web_crawlers)
# ---------------------------------------------------------------------------
register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_application_fuzzers.wfuzz",
        display_name="WFuzz (Fuzzer)",
        category=ToolCategory.WEB_FUZZING,
        applicable_phases=[
            PentestPhase.VULNERABILITY_ASSESSMENT,
            PentestPhase.EXPLOITATION,
        ],
        capabilities=[
            ToolCapability(
                name="parameter_fuzzing",
                description="Fuzz web requests across parameters, headers, paths, or auth flows with payload markers; use for input variation testing, not path discovery",
                output_indicators=["payload", "status_code", "words"],
            ),
            ToolCapability(
                name="response_filtering",
                description="Hide or match responses via status, words, lines, chars, and timing filters for targeted fuzzing",
                output_indicators=["filter", "hide", "match"],
            ),
            ToolCapability(
                name="recursive_fuzzing",
                description="Support recursive fuzzing and scan mode to expand coverage across nested paths",
                output_indicators=["recursion", "scan"],
            ),
            ToolCapability(
                name="scan_mode",
                description="Scan mode for adaptive discovery combined with payload-driven fuzzing strategies",
                output_indicators=["scan", "payload"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=8,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=10,
    )
)
