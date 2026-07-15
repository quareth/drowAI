import os
import re
import subprocess
import time
from typing import List, Dict, Any, Optional
from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import clean_output


class DirbArgs(BaseToolArgs):
    """Arguments for the Dirb web content scanner."""

    wordlist: str = Field(..., description="Path to wordlist to use")
    threads: int = Field(
        5,
        description="Number of concurrent threads (Dirb uses lightweight workers)",
        ge=1,
        le=100,
    )
    timeout: int = Field(
        300,
        description="Execution timeout in seconds",
        ge=5,
        le=900,
    )
    username: Optional[str] = Field(
        None, description="Username for basic authentication"
    )
    password: Optional[str] = Field(
        None, description="Password for basic authentication"
    )
    headers: Optional[str] = Field(
        None, description='Custom headers, e.g. "Authorization: Bearer <token>"'
    )
    cookies: Optional[str] = Field(
        None, description='Cookies string, e.g. "SESSION=abc123"'
    )
    user_agent: Optional[str] = Field(
        None, description="Override User-Agent header"
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy to route requests through, e.g. http://127.0.0.1:8080",
    )


class DirbTool(BaseTool):
    """Run Dirb for web content enumeration."""

    args_model = DirbArgs

    def build_command(self, args: DirbArgs) -> List[str]:
        """Construct the dirb command."""
        command = ["dirb", args.target, args.wordlist, "-noerror"]
        # Dirb does not have a formal threads flag; workers are lightweight.
        if args.username and args.password:
            command.extend(["-u", f"{args.username}:{args.password}"])
        if args.headers:
            command.extend(["-H", args.headers])
        if args.cookies:
            command.extend(["-c", args.cookies])
        if args.user_agent:
            command.extend(["-a", args.user_agent])
        if args.proxy:
            command.extend(["-p", args.proxy])
        return command

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: DirbArgs
    ) -> Dict[str, Any]:
        """Parse dirb output lines into structured metadata."""
        cleaned_output = clean_output(stdout, strip_ansi=True)
        findings: List[Dict[str, Any]] = []
        pattern = re.compile(
            r"^\+\s+(?P<url>\S+)\s+\(CODE:(?P<status>\d+)\|SIZE:(?P<size>\d+)\)",
            flags=re.IGNORECASE,
        )
        for line in cleaned_output.splitlines():
            match = pattern.match(line.strip())
            if match:
                findings.append(
                    {
                        "url": match.group("url"),
                        "status": int(match.group("status")),
                        "size": int(match.group("size")),
                    }
                )

        metadata: Dict[str, Any] = {
            "findings": findings,
            "found_urls": [item["url"] for item in findings],
            "exit_code": exit_code,
        }
        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: DirbArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist stdout to artifacts directory."""
        if not stdout:
            return []
        artifact_timestamp = int(timestamp or time.time())
        artifact_path = f"artifacts/dirb_{artifact_timestamp}.txt"
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: DirbArgs) -> ToolResult:
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
        tool_id="web_applications.web_crawlers.dirb",
        display_name="Dirb",
        category=ToolCategory.WEB_CRAWLING,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="directory_enumeration",
                description="Discover common web directories and files with built-in wordlists; use for simple content enumeration on web roots",
                output_indicators=["Status: 200", "Status: 301"],
            ),
            ToolCapability(
                name="authenticated_enumeration",
                description="Supports authenticated/proxy scans via headers and basic auth",
                output_indicators=["CODE:", "SIZE:"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)