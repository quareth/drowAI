"""WFUZZ web crawling tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
import re
from enum import Enum
from typing import List, Optional, Literal, Dict, Any

from pydantic import ConfigDict, Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from .._path_safety import resolve_wordlist_path_for_execution
from ..parsing_utils import parse_json_output, clean_output


class WfuzzMode(str, Enum):
    """WFUZZ operation modes exposed by this crawler wrapper."""
    
    DIRECTORY = "directory"


class OutputFormat(str, Enum):
    """WFUZZ output format options."""
    
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    TEXT = "text"


class WfuzzArgs(BaseToolArgs):
    """Arguments for the WFUZZ tool."""

    model_config = ConfigDict(extra="forbid")

    mode: WfuzzMode = Field(
        WfuzzMode.DIRECTORY,
        description="Operation mode for WFUZZ",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Wordlist file to use for fuzzing",
    )
    threads: int = Field(
        10,
        ge=1,
        le=50,
        description="Number of concurrent threads",
    )
    timeout: int = Field(
        10,
        ge=1,
        le=60,
        description="Timeout in seconds for requests",
    )
    delay: Optional[float] = Field(
        None,
        ge=0.1,
        le=10.0,
        description="Delay between requests in seconds",
    )
    match_status: Optional[str] = Field(
        None,
        description="Match HTTP status codes (comma-separated)",
    )
    filter_status: Optional[str] = Field(
        None,
        description="Filter HTTP status codes (comma-separated)",
    )
    match_size: Optional[str] = Field(
        None,
        description="Match response size (e.g., '123,456')",
    )
    filter_size: Optional[str] = Field(
        None,
        description="Filter response size (e.g., '123,456')",
    )
    match_words: Optional[str] = Field(
        None,
        description="Match response word count (e.g., '123,456')",
    )
    filter_words: Optional[str] = Field(
        None,
        description="Filter response word count (e.g., '123,456')",
    )
    match_lines: Optional[str] = Field(
        None,
        description="Match response line count (e.g., '123,456')",
    )
    filter_lines: Optional[str] = Field(
        None,
        description="Filter response line count (e.g., '123,456')",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects",
    )
    ssl_verify: bool = Field(
        True,
        description="Verify SSL certificates",
    )
    auth: Optional[str] = Field(
        None,
        description="Authentication credentials (user:pass)",
    )
    username: Optional[str] = Field(
        None, description="Username for authentication"
    )
    password: Optional[str] = Field(
        None, description="Password for authentication"
    )
    auth_type: Literal["basic", "digest", "ntlm"] = Field(
        "basic",
        description="Authentication mechanism to use with provided credentials",
    )
    proxy: Optional[str] = Field(
        None, description="Proxy to use for outbound requests"
    )


def parse_wfuzz_json(json_text: str) -> Dict[str, Any]:
    """Parse WFUZZ JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"results": [], "summary": {}}
    
    try:
        data = json.loads(json_text)
        
        # Handle different response types
        if isinstance(data, list):
            metadata["results"] = data
        elif isinstance(data, dict):
            if "results" in data:
                metadata["results"] = data["results"]
            elif "data" in data:
                metadata["results"] = data["data"]
            else:
                metadata["results"] = [data]
            
            # Extract summary information
            if "total" in data:
                metadata["summary"]["total"] = data["total"]
            if "mode" in data:
                metadata["summary"]["mode"] = data["mode"]
            if "status" in data:
                metadata["summary"]["status"] = data["status"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


def parse_wfuzz_text(text_output: str) -> Dict[str, Any]:
    """Parse WFUZZ text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "results": [],
        "summary": {},
        "config": {}
    }
    
    try:
        lines = text_output.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse result lines
            if re.match(r'^\d+\s+\d+\s+\d+\s+\d+', line):
                parts = line.split()
                if len(parts) >= 4:
                    result = {
                        "request_id": int(parts[0]),
                        "status": int(parts[1]),
                        "size": int(parts[2]),
                        "words": int(parts[3]),
                        "lines": int(parts[4]) if len(parts) > 4 else 0
                    }
                    metadata["results"].append(result)
            
            # Parse configuration
            elif ":: Config" in line:
                config_match = re.search(r':: Config\s*:\s*(.+)', line)
                if config_match:
                    metadata["config"]["config"] = config_match.group(1)
            
            # Parse summary
            elif "Total requests:" in line:
                total_match = re.search(r'Total requests:\s*(\d+)', line)
                if total_match:
                    metadata["summary"]["total_requests"] = int(total_match.group(1))
            elif "Successful requests:" in line:
                success_match = re.search(r'Successful requests:\s*(\d+)', line)
                if success_match:
                    metadata["summary"]["successful_requests"] = int(success_match.group(1))
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "successful_results": len([r for r in metadata["results"] if r.get("status", 0) < 400])
            }
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse WFUZZ output: {str(e)}"
    
    return metadata


class WfuzzTool(BaseTool):
    """Run WFUZZ and parse the results."""

    args_model = WfuzzArgs

    def build_command(self, args: WfuzzArgs) -> List[str]:
        """Construct wfuzz command for execution model.

        Flag references come from the Wfuzz man page (Debian trixie):
        ``-o printer`` for stdout printer, ``-s N`` for request delay,
        ``-L``/``--follow`` for HTTP redirect following, and the documented
        match/hide filters (``--sc``/``--hc`` etc.). ``-of``, ``-d``,
        ``-timeout`` and ``-r`` are not documented Wfuzz flags.
        """
        command: List[str] = ["wfuzz"]

        # DIRECTORY emits no mode flag; the FUZZ marker belongs in the target.

        if args.wordlist:
            command.extend(["-w", resolve_wordlist_path_for_execution(args.wordlist)])
        command.extend(["-t", str(args.threads)])
        if args.delay:
            command.extend(["-s", str(args.delay)])
        if args.match_status:
            command.extend(["--sc", args.match_status])
        if args.filter_status:
            command.extend(["--hc", args.filter_status])
        if args.match_size:
            command.extend(["--sh", args.match_size])
        if args.filter_size:
            command.extend(["--hh", args.filter_size])
        if args.match_words:
            command.extend(["--sw", args.match_words])
        if args.filter_words:
            command.extend(["--hw", args.filter_words])
        if args.match_lines:
            command.extend(["--sl", args.match_lines])
        if args.filter_lines:
            command.extend(["--hl", args.filter_lines])
        if args.verbose:
            command.append("-v")
        if args.follow_redirects:
            command.append("--follow")
        if not args.ssl_verify:
            command.append("-k")
        if args.username and args.password:
            flag = f"--{args.auth_type}"
            command.extend([flag, f"{args.username}:{args.password}"])
        elif args.auth:
            # Default to basic auth when only a raw user:pass string is given.
            command.extend(["--basic", args.auth])
        if args.proxy:
            command.extend(["-p", args.proxy])

        if args.output_format == OutputFormat.JSON:
            command.extend(["-o", "json"])
        elif args.output_format == OutputFormat.CSV:
            command.extend(["-o", "csv"])
        elif args.output_format == OutputFormat.XML:
            command.extend(["-o", "xml"])
        else:
            command.extend(["-o", "raw"])

        # ``-u url`` is the documented Wfuzz way to specify the request URL.
        command.extend(["-u", args.target])
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: WfuzzArgs
    ) -> Dict[str, Any]:
        """Parse wfuzz output into structured metadata."""
        if args.output_format == OutputFormat.JSON:
            metadata = parse_json_output(stdout or "")
        else:
            metadata = parse_wfuzz_text(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: WfuzzArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist wfuzz output when non-trivial."""
        if not stdout or len(stdout) <= 200:
            return []
        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/wfuzz_{args.mode.value}_{artifact_timestamp}."
            f"{args.output_format.value}"
        )
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
    ToolCapability,
    ToolCategory,
    PentestPhase,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_crawlers.wfuzz",
        display_name="WFUZZ",
        category=ToolCategory.WEB_CRAWLING,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="web_fuzzing",
                description="Discover web paths by fuzzing a URL with FUZZ marker and wordlist; use for content enumeration when ffuf is unavailable",
                output_indicators=["Total requests", "FUZZ"],
            ),
            ToolCapability(
                name="authenticated_enumeration",
                description="Supports authenticated fuzzing via basic/digest/ntlm and cookies",
                output_indicators=["Authorization", "Cookie"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=12,
    )
)
