"""Gobuster directory/DNS/vhost brute-force tool.

Gobuster is text-native: the CLI produces human-readable text that is
parsed internally into structured metadata. There is no reliable CLI
JSON flag across all gobuster builds, so text is the canonical capture
substrate.

Command construction is split per documented mode (``dir``, ``dns``,
``vhost``) because each mode exposes a different flag set in the
upstream ``gobuster v3.8.2`` source. Flags are taken from:

- Debian man page: https://manpages.debian.org/testing/gobuster/gobuster.1.en.html
- ``dir`` flags: https://raw.githubusercontent.com/OJ/gobuster/v3.8.2/cli/dir/dir.go
- ``dns`` flags: https://raw.githubusercontent.com/OJ/gobuster/v3.8.2/cli/dns/dns.go
- ``vhost`` flags: https://raw.githubusercontent.com/OJ/gobuster/v3.8.2/cli/vhost/vhost.go
"""

import os
import re
import subprocess
import time
from typing import List, Optional, Dict, Any, Literal
from pydantic import ConfigDict, Field, field_validator, model_validator

from ...base_tool import BaseTool
from ...canonical_capture import CaptureFamily, CanonicalCaptureFormat, ToolCaptureContract
from ...schemas import BaseToolArgs, ToolResult
from ..parsing_utils import clean_output, parse_crawler_line


class GobusterArgs(BaseToolArgs):
    """Arguments for the Gobuster directory bruteforcer."""

    model_config = ConfigDict(extra="forbid")

    wordlist: str = Field(..., description="Path to wordlist to use")
    mode: Literal["dir", "dns", "vhost"] = Field(
        "dir",
        description="Gobuster mode to run (dir, dns, or vhost)",
    )
    threads: int = Field(
        10,
        description="Number of concurrent threads",
        ge=1,
        le=100,
    )
    extensions: Optional[str] = Field(
        None,
        description="Comma-separated file extensions to append",
    )
    output_format: Literal["text", "json"] = Field(
        "text",
        description=(
            "Desired output format for the agent/platform. "
            "Note: gobuster CLI output is parsed from text; this does NOT imply "
            "a CLI JSON flag will be used."
        ),
    )
    username: Optional[str] = Field(
        None,
        description="Username for basic authentication",
    )
    password: Optional[str] = Field(
        None,
        description="Password for basic authentication",
    )
    headers: Optional[str] = Field(
        None,
        description='Custom headers, e.g. "Authorization: Bearer <token>"',
    )
    cookies: Optional[str] = Field(
        None,
        description='Cookies to send with requests, e.g. "SESSION=abc123"',
    )
    user_agent: Optional[str] = Field(
        None,
        description="Override the User-Agent header",
    )
    method: str = Field(
        "GET",
        description="HTTP method for dir/vhost requests",
    )
    status_codes: Optional[str] = Field(
        None,
        description="Positive status code filter, supports comma-separated values/ranges (e.g. 200,300-399)",
    )
    status_codes_blacklist: Optional[str] = Field(
        None,
        description="Negative status code filter, supports comma-separated values/ranges; overrides status_codes",
    )
    exclude_length: Optional[str] = Field(
        None,
        description="Response lengths to exclude, supports comma-separated values/ranges",
    )
    follow_redirects: bool = Field(
        False,
        description="Follow redirects",
    )
    no_tls_validation: bool = Field(
        False,
        description="Skip TLS certificate validation",
    )
    proxy: Optional[str] = Field(
        None,
        description="Proxy URL for HTTP requests",
    )
    delay: Optional[str] = Field(
        None,
        description="Delay each thread waits between requests (e.g. 1500ms, 2s)",
    )
    hide_length: bool = Field(
        False,
        description=(
            "Hide response length column in dir/vhost output. Maps to gobuster's "
            "documented --hide-length / --hl flag. Note: gobuster has no '-l' "
            "flag; older code that emitted '-l' produced an immediate parse error."
        ),
    )
    no_progress: bool = Field(
        True,
        description="Disable progress output for deterministic capture",
    )
    no_color: bool = Field(
        True,
        description="Disable color output for deterministic parsing",
    )
    quiet: bool = Field(
        False,
        description="Suppress banner and other noise",
    )
    force: bool = Field(
        False,
        description="Continue in dir mode even when precheck errors occur",
    )
    append_domain: bool = Field(
        False,
        description="Append base domain in vhost mode",
    )
    timeout: int = Field(
        300,
        description="Execution timeout in seconds",
        ge=5,
        le=900,
    )

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        """Normalize and validate common HTTP methods accepted by gobuster."""
        method = str(value or "").strip().upper()
        allowed = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        if method not in allowed:
            raise ValueError(f"method must be one of: {', '.join(sorted(allowed))}")
        return method

    @field_validator("status_codes", "status_codes_blacklist", "exclude_length")
    @classmethod
    def _validate_ranges(cls, value: Optional[str]) -> Optional[str]:
        """Validate comma-separated integer/range lists used by gobuster filters."""
        if not value:
            return value
        for token in str(value).split(","):
            part = token.strip()
            if not part:
                continue
            if re.fullmatch(r"\d+", part):
                continue
            if re.fullmatch(r"\d+\s*-\s*\d+", part):
                start, end = [int(piece.strip()) for piece in part.split("-", 1)]
                if start > end:
                    raise ValueError(f"invalid range '{part}': start must be <= end")
                continue
            raise ValueError(f"invalid numeric/range token: {part}")
        return value

    @model_validator(mode="after")
    def _validate_mode_options(self) -> "GobusterArgs":
        """Reject options the selected gobuster mode does not document.

        Source citations:
        - dir flags:    gobuster/v3.8.2/cli/dir/dir.go
        - dns flags:    gobuster/v3.8.2/cli/dns/dns.go
        - vhost flags:  gobuster/v3.8.2/cli/vhost/vhost.go
        """
        if self.mode == "dns":
            http_options = {
                "username": self.username,
                "password": self.password,
                "headers": self.headers,
                "cookies": self.cookies,
                "user_agent": self.user_agent,
                "proxy": self.proxy,
                "status_codes": self.status_codes,
                "status_codes_blacklist": self.status_codes_blacklist,
                "exclude_length": self.exclude_length,
                "method": self.method if self.method != "GET" else None,
                "follow_redirects": self.follow_redirects,
                "no_tls_validation": self.no_tls_validation,
            }
            supplied = [name for name, value in http_options.items() if value]
            if supplied:
                raise ValueError(
                    "dns mode does not support HTTP options: " + ", ".join(sorted(supplied))
                )
        # Extensions are documented only for `dir` mode in v3.8.2.
        if self.mode != "dir" and self.extensions:
            raise ValueError(
                "extensions are only supported in dir mode (gobuster v3.8.2 dir CLI)"
            )
        # vhost mode does not document a positive status-code filter; it only
        # exposes --exclude-status (negative).
        if self.mode == "vhost" and self.status_codes:
            raise ValueError(
                "status_codes (positive filter) is not supported in vhost mode; "
                "use status_codes_blacklist for --exclude-status"
            )
        # gobuster v3.8.2 dir CLI rejects supplying positive and negative
        # status-code filters simultaneously (cli/dir/dir.go enforces a
        # mutual-exclusivity check between --status-codes and
        # --status-codes-blacklist). Refuse early so we never build a
        # command the CLI will reject.
        if (
            self.mode == "dir"
            and self.status_codes
            and self.status_codes_blacklist
        ):
            raise ValueError(
                "status_codes and status_codes_blacklist are mutually exclusive "
                "in dir mode (gobuster v3.8.2 cli/dir/dir.go); supply only one"
            )
        # The hide_length / --hl / --hide-length flag is only documented for
        # dir and vhost modes.
        if self.mode == "dns" and self.hide_length:
            raise ValueError("hide_length is not supported in dns mode")
        if self.mode != "dir" and self.force:
            raise ValueError("force is only supported in dir mode")
        if self.mode != "vhost" and self.append_domain:
            raise ValueError("append_domain is only supported in vhost mode")
        return self


class GobusterTool(BaseTool):
    """Run Gobuster for directory or DNS enumeration."""

    args_model = GobusterArgs
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.TEXT_NATIVE,
        canonical_format=CanonicalCaptureFormat.TEXT,
    )

    def build_command(self, args: GobusterArgs) -> List[str]:
        """Dispatch to the documented per-mode command builder."""
        if args.mode == "dir":
            return self._build_dir_command(args)
        if args.mode == "dns":
            return self._build_dns_command(args)
        return self._build_vhost_command(args)

    # ------------------------------------------------------------------
    # Mode-specific builders
    #
    # Notes on flag choices:
    # * No JSON-output flag is emitted: gobuster is text-native here.
    # * Long-form flags are preferred where both forms exist (per the
    #   project's version-drift policy).
    # * We never emit ``-l``: gobuster has no such flag. Length display
    #   control is exposed via the documented ``--hide-length`` flag.
    # ------------------------------------------------------------------

    def _build_dir_command(self, args: GobusterArgs) -> List[str]:
        """Build the ``gobuster dir`` command (cli/dir/dir.go in v3.8.2)."""
        command: List[str] = [
            "gobuster",
            "dir",
            "--url",
            args.target,
            "--wordlist",
            args.wordlist,
            "--threads",
            str(args.threads),
            "--no-error",
            "--method",
            args.method,
        ]
        if args.extensions:
            command.extend(["--extensions", args.extensions])
        if args.username:
            command.extend(["--username", args.username])
        if args.password:
            command.extend(["--password", args.password])
        if args.headers:
            command.extend(["--headers", args.headers])
        if args.cookies:
            command.extend(["--cookies", args.cookies])
        if args.user_agent:
            command.extend(["--useragent", args.user_agent])
        # gobuster's dir CLI ships with a default --status-codes-blacklist
        # value (cli/dir/dir.go). To use a positive --status-codes filter we
        # must explicitly clear that default with an empty
        # --status-codes-blacklist; otherwise the CLI rejects the
        # combination at startup. The two filters are mutually exclusive
        # when both are user-supplied (validated above).
        if args.status_codes:
            command.extend(["--status-codes", args.status_codes])
            command.extend(["--status-codes-blacklist", ""])
        elif args.status_codes_blacklist:
            command.extend(["--status-codes-blacklist", args.status_codes_blacklist])
        if args.exclude_length:
            command.extend(["--exclude-length", args.exclude_length])
        if args.follow_redirects:
            command.append("--follow-redirect")
        if args.no_tls_validation:
            command.append("--no-tls-validation")
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.delay:
            command.extend(["--delay", args.delay])
        if args.hide_length:
            command.append("--hide-length")
        if args.no_progress:
            command.append("--no-progress")
        if args.no_color:
            command.append("--no-color")
        if args.quiet:
            command.append("--quiet")
        if args.force:
            command.append("--force")
        return command

    def _build_dns_command(self, args: GobusterArgs) -> List[str]:
        """Build the ``gobuster dns`` command (cli/dns/dns.go in v3.8.2).

        DNS mode uses ``--domain`` for the target host. HTTP options,
        extensions, status codes, and ``hide-length`` are not supported
        and are rejected by the args validator.
        """
        command: List[str] = [
            "gobuster",
            "dns",
            "--domain",
            args.target,
            "--wordlist",
            args.wordlist,
            "--threads",
            str(args.threads),
            "--no-error",
        ]
        if args.delay:
            command.extend(["--delay", args.delay])
        if args.no_progress:
            command.append("--no-progress")
        if args.no_color:
            command.append("--no-color")
        if args.quiet:
            command.append("--quiet")
        return command

    def _build_vhost_command(self, args: GobusterArgs) -> List[str]:
        """Build the ``gobuster vhost`` command (cli/vhost/vhost.go in v3.8.2).

        Vhost mode exposes ``--url``, ``--wordlist``, ``--append-domain``,
        ``--exclude-length``, and ``--exclude-status`` for negative status
        filtering. It does not document a positive ``--status-codes``
        filter (rejected by the args validator) nor a ``-b`` flag.
        """
        command: List[str] = [
            "gobuster",
            "vhost",
            "--url",
            args.target,
            "--wordlist",
            args.wordlist,
            "--threads",
            str(args.threads),
            "--no-error",
            "--method",
            args.method,
        ]
        if args.username:
            command.extend(["--username", args.username])
        if args.password:
            command.extend(["--password", args.password])
        if args.headers:
            command.extend(["--headers", args.headers])
        if args.cookies:
            command.extend(["--cookies", args.cookies])
        if args.user_agent:
            command.extend(["--useragent", args.user_agent])
        # vhost only supports negative status filters via --exclude-status.
        if args.status_codes_blacklist:
            command.extend(["--exclude-status", args.status_codes_blacklist])
        if args.exclude_length:
            command.extend(["--exclude-length", args.exclude_length])
        if args.follow_redirects:
            command.append("--follow-redirect")
        if args.no_tls_validation:
            command.append("--no-tls-validation")
        if args.proxy:
            command.extend(["--proxy", args.proxy])
        if args.delay:
            command.extend(["--delay", args.delay])
        if args.hide_length:
            command.append("--hide-length")
        if args.no_progress:
            command.append("--no-progress")
        if args.no_color:
            command.append("--no-color")
        if args.quiet:
            command.append("--quiet")
        if args.append_domain:
            command.append("--append-domain")
        return command

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: GobusterArgs,
    ) -> Dict[str, Any]:
        """Parse gobuster output into structured metadata."""
        cleaned_output = clean_output(stdout, strip_ansi=True)
        findings: List[Dict[str, Any]] = []

        for line in cleaned_output.splitlines():
            if not line:
                continue
            parsed_line = parse_crawler_line(line)
            if parsed_line:
                findings.append(parsed_line)

        metadata: Dict[str, Any] = {
            "findings": findings,
            "found_paths": [item["path"] for item in findings],
            "exit_code": exit_code,
            "requested_output_format": args.output_format,
            "effective_output_format": "text_parsed",
        }
        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: GobusterArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Persist output as artifact when long enough."""
        if not stdout or len(stdout) <= 200:
            return []
        artifact_timestamp = int(timestamp or time.time())
        artifact_path = (
            f"artifacts/gobuster_{args.mode}_{artifact_timestamp}.txt"
        )
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
            return [artifact_path]
        except Exception:
            return []

    def run(self, args: GobusterArgs) -> ToolResult:
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
        tool_id="web_applications.web_crawlers.gobuster",
        display_name="Gobuster",
        category=ToolCategory.WEB_CRAWLING,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="directory_enumeration",
                description="Discover web directories, files, DNS names, or vhosts with wordlists; use for broad content or host enumeration, not input fuzzing",
                output_indicators=["Status: 200", "Status: 301"],
            ),
            ToolCapability(
                name="subdomain_enumeration",
                description="Find subdomains",
                output_indicators=["Found:"],
            ),
            ToolCapability(
                name="authenticated_enumeration",
                description="Supports authenticated scans via basic auth/headers/cookies",
                output_indicators=["Status:", "Size:"],
            ),
        ],
        required_services=["http", "https"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=15,
    )
)
