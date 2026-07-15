"""First-class ffuf crawler focused on path and directory enumeration."""

from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from runtime_shared.workspace_files import RuntimeWorkspaceFile

from ...base_tool import BaseTool, ToolPostprocessResult
from ...canonical_capture import CanonicalCaptureFormat, CaptureFamily, ToolCaptureContract
from ...schemas import ToolResult
from .._ffuf_common import (
    basic_auth_header,
    build_matcher_filter_args,
    inline_wordlist_relative_path,
    inline_wordlist_workspace_file,
    parse_ffuf_json_text,
    parse_ffuf_text,
    resolve_wordlist_reference_for_execution,
    validate_delay,
    validate_http_target,
    validate_input_cmd,
)
from .._ffuf_planner import (
    FFUF_CRAWLER_PLANNER_GUIDANCE,
    FfufCrawlerPlannerArgs,
    compile_crawler_planner_args,
)
from .._ffuf_semantics import (
    build_ffuf_semantic_evidence,
    build_ffuf_semantic_observations,
)
from ..parsing_utils import clean_output
from ...enhanced_metadata_registry import (
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
    register_enhanced_tool_metadata,
)


class BasicAuthArgs(BaseModel):
    """Structured basic-auth input rendered as an Authorization header."""

    user: str = Field(..., min_length=1, description="Username for HTTP basic authentication.")
    password: str = Field(..., min_length=1, description="Password for HTTP basic authentication.")


class FfufArgs(BaseModel):
    """Enumeration-focused ffuf arguments for `/FUZZ` path discovery."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description=(
            "Absolute target URL for directory enumeration. This crawler only supports URLs ending "
            "with '/FUZZ' or '/FUZZ/' so ffuf can enumerate paths professionally and recurse safely."
        ),
    )
    wordlist: Optional[str] = Field(
        None,
        description=(
            "Primary wordlist path for ffuf `-w`. Use a workspace-relative file path or an in-container "
            "SecLists path like '/usr/share/seclists/...'."
        ),
    )
    inline_wordlist: Optional[List[str]] = Field(
        None,
        description=(
            "Safe dynamic wordlist. Provide concrete entries and the tool will materialize them into a "
            "workspace file before invoking ffuf."
        ),
    )
    input_cmd: Optional[str] = Field(
        None,
        description=(
            "Advanced ffuf `-input-cmd` source. Use only when you truly need runtime-generated input. "
            "Prefer `inline_wordlist` for simple sequences like numeric IDs."
        ),
    )
    input_shell: Optional[str] = Field(
        None,
        description="Shell used for `-input-cmd`, for example '/bin/sh' or '/bin/bash'.",
    )
    input_num: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Required with `input_cmd`: how many generated payloads ffuf should consume.",
    )
    headers: List[str] = Field(
        default_factory=list,
        description="Optional repeated HTTP headers. Example: ['Authorization: Bearer <token>'].",
    )
    cookies: Optional[str] = Field(
        None,
        description="Optional cookie header content for ffuf `-b`, for example 'SESSION=abc123'.",
    )
    basic_auth: Optional[BasicAuthArgs] = Field(
        None,
        description="Optional HTTP basic authentication rendered as an Authorization header.",
    )
    extensions: Optional[str] = Field(
        None,
        description="Optional comma-separated extension list for ffuf `-e`, for example '.php,.txt'.",
    )
    dirsearch_compat: bool = Field(
        False,
        description="Enable ffuf `-D` DirSearch-compatible wordlist behavior.",
    )
    ignore_wordlist_comments: bool = Field(
        False,
        description="Ignore comment lines in wordlists with ffuf `-ic`.",
    )
    follow_redirects: bool = Field(
        False,
        description="Follow HTTP redirects with ffuf `-r`.",
    )
    http2: bool = Field(
        False,
        description="Enable HTTP/2 with ffuf `-http2` when the target supports it.",
    )
    ignore_body: bool = Field(
        False,
        description="Use ffuf `-ignore-body` to save bandwidth when body content is not needed.",
    )
    proxy: Optional[str] = Field(
        None,
        description="Optional upstream proxy URL for ffuf `-x`.",
    )
    replay_proxy: Optional[str] = Field(
        None,
        description="Optional replay proxy URL for ffuf `-replay-proxy`.",
    )
    ignore_tls: bool = Field(
        False,
        description="Ignore TLS verification errors with ffuf `-k`.",
    )
    recursion: bool = Field(
        False,
        description="Enable ffuf `-recursion`. Only valid when the URL ends with '/FUZZ' or '/FUZZ/'.",
    )
    recursion_depth: int = Field(
        0,
        ge=0,
        le=20,
        description="Maximum ffuf recursion depth for path enumeration.",
    )
    recursion_strategy: Literal["default", "greedy"] = Field(
        "default",
        description="ffuf recursion strategy: 'default' or 'greedy'.",
    )
    threads: int = Field(
        40,
        ge=1,
        le=100,
        description="Concurrent ffuf worker threads (`-t`).",
    )
    delay: Optional[str] = Field(
        None,
        description="ffuf `-p` delay as a float or float range, for example '0.1' or '0.1-2.0'.",
    )
    rate: Optional[int] = Field(
        None,
        ge=1,
        le=10_000,
        description="Optional ffuf `-rate` requests-per-second cap.",
    )
    request_timeout: int = Field(
        10,
        ge=1,
        le=300,
        description="Per-request timeout in seconds for ffuf `-timeout`.",
    )
    job_max_time: Optional[int] = Field(
        None,
        ge=1,
        le=86_400,
        description="Optional ffuf `-maxtime` limit in seconds for the overall job.",
    )
    job_max_time_per_recursion: Optional[int] = Field(
        None,
        ge=1,
        le=86_400,
        description="Optional ffuf `-maxtime-job` limit in seconds per recursion job.",
    )
    match_status: Optional[str] = Field(None, description="ffuf `-mc` HTTP status matcher.")
    match_lines: Optional[str] = Field(None, description="ffuf `-ml` line-count matcher.")
    match_words: Optional[str] = Field(None, description="ffuf `-mw` word-count matcher.")
    match_size: Optional[str] = Field(None, description="ffuf `-ms` response-size matcher.")
    match_time: Optional[str] = Field(None, description="ffuf `-mt` time-to-first-byte matcher.")
    match_regex: Optional[str] = Field(None, description="ffuf `-mr` regex matcher.")
    filter_status: Optional[str] = Field(None, description="ffuf `-fc` HTTP status filter.")
    filter_lines: Optional[str] = Field(None, description="ffuf `-fl` line-count filter.")
    filter_words: Optional[str] = Field(None, description="ffuf `-fw` word-count filter.")
    filter_size: Optional[str] = Field(None, description="ffuf `-fs` response-size filter.")
    filter_time: Optional[str] = Field(None, description="ffuf `-ft` time-to-first-byte filter.")
    filter_regex: Optional[str] = Field(None, description="ffuf `-fr` regex filter.")
    matcher_mode: Literal["and", "or"] = Field(
        "or",
        description="ffuf `-mmode` set operator for matchers.",
    )
    filter_mode: Literal["and", "or"] = Field(
        "or",
        description="ffuf `-fmode` set operator for filters.",
    )
    auto_calibrate: bool = Field(
        False,
        description="Enable ffuf `-ac` auto-calibration for unfamiliar targets.",
    )
    auto_calibrate_per_host: bool = Field(
        False,
        description="Enable ffuf `-ach` per-host auto-calibration.",
    )
    auto_calibrate_strings: List[str] = Field(
        default_factory=list,
        description="Optional repeated ffuf `-acc` strings for custom calibration probes.",
    )
    auto_calibrate_strategies: List[str] = Field(
        default_factory=list,
        description="Optional repeated ffuf `-acs` strategies, for example 'basic' or 'advanced'.",
    )
    auto_calibrate_keyword: Optional[str] = Field(
        None,
        description="Optional ffuf `-ack` calibration keyword. Defaults to ffuf's FUZZ keyword.",
    )
    stop_on_403: bool = Field(False, description="Enable ffuf `-sf` stop-on-403 guard.")
    stop_on_errors: bool = Field(False, description="Enable ffuf `-se` stop-on-errors guard.")
    stop_on_any: bool = Field(False, description="Enable ffuf `-sa` stop-on-any-error guard.")
    silent: bool = Field(
        False,
        description="When true, use ffuf `-s` to emit only matched payload values on stdout.",
    )

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        normalized = validate_http_target(value)
        if not (normalized.endswith("/FUZZ") or normalized.endswith("/FUZZ/")):
            raise ValueError(
                "ffuf crawler requires a URL ending with '/FUZZ' or '/FUZZ/'. Example: 'https://host/FUZZ'"
            )
        return normalized

    @field_validator("delay")
    @classmethod
    def _validate_delay(cls, value: Optional[str]) -> Optional[str]:
        return validate_delay(value)

    @field_validator("input_cmd")
    @classmethod
    def _validate_input_cmd(cls, value: Optional[str]) -> Optional[str]:
        return validate_input_cmd(value)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: List[str]) -> List[str]:
        for header in value:
            if ":" not in header:
                raise ValueError(f"headers must use 'Name: Value' format. Invalid header: {header!r}")
        return value

    @model_validator(mode="after")
    def _validate_model(self) -> "FfufArgs":
        input_sources = [
            bool(self.wordlist),
            bool(self.inline_wordlist),
            bool(self.input_cmd),
        ]
        if sum(input_sources) != 1:
            raise ValueError(
                "Provide exactly one input source: wordlist, inline_wordlist, or input_cmd."
            )
        if self.input_cmd and not self.input_num:
            raise ValueError("input_cmd requires input_num so ffuf knows how many generated inputs to consume.")
        if self.input_shell and not self.input_cmd:
            raise ValueError("input_shell is only valid together with input_cmd.")
        if self.recursion_depth and not self.recursion:
            raise ValueError("recursion_depth only applies when recursion=true.")
        return self


class FfufTool(BaseTool):
    """Run ffuf in crawler mode with honest, enumeration-focused semantics."""

    args_model = FfufArgs
    planner_args_model = FfufCrawlerPlannerArgs
    planner_guidance = FFUF_CRAWLER_PLANNER_GUIDANCE
    parameter_validation_policy = {"autofill_target": False}
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.TEXT_NATIVE,
        canonical_format=CanonicalCaptureFormat.TEXT,
    )
    _tool_name = "crawler"

    @classmethod
    def compile_planner_parameters(
        cls,
        planner_args: FfufCrawlerPlannerArgs | Dict[str, Any],
        *,
        action_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        if isinstance(planner_args, FfufCrawlerPlannerArgs):
            return compile_crawler_planner_args(planner_args, action_target=action_target)
        parsed = FfufCrawlerPlannerArgs(**dict(planner_args or {}))
        return compile_crawler_planner_args(parsed, action_target=action_target)

    def _build_input_args(self, args: FfufArgs) -> List[str]:
        if args.wordlist:
            return ["-w", resolve_wordlist_reference_for_execution(args.wordlist)]
        if args.inline_wordlist:
            relative_path = self._inline_wordlist_path()
            return ["-w", f"/workspace/{relative_path}"]
        command = ["-input-cmd", args.input_cmd or "", "-input-num", str(args.input_num or 0)]
        if args.input_shell:
            command.extend(["-input-shell", args.input_shell])
        return command

    def _inline_wordlist_path(self) -> str:
        path = getattr(self, "_inline_wordlist_relative_path", None)
        if not path:
            path = inline_wordlist_relative_path(prefix="ffuf_crawler")
            self._inline_wordlist_relative_path = path
        return str(path)

    def prepare_workspace_files(self, args: FfufArgs) -> List[RuntimeWorkspaceFile]:
        if not args.inline_wordlist:
            return []
        return [
            inline_wordlist_workspace_file(
                args.inline_wordlist,
                relative_path=self._inline_wordlist_path(),
                description="ffuf crawler inline wordlist",
            )
        ]

    def _build_command(self, args: FfufArgs, *, timestamp: Optional[float] = None) -> List[str]:
        """Construct a ffuf command for `/FUZZ` path enumeration."""

        _ = timestamp
        command: List[str] = ["ffuf", "-noninteractive"]
        if args.silent:
            command.append("-s")
        command.extend(self._build_input_args(args))
        command.extend(["-u", args.target, "-t", str(args.threads), "-timeout", str(args.request_timeout)])

        if args.extensions:
            command.extend(["-e", args.extensions])
        if args.dirsearch_compat:
            command.append("-D")
        if args.ignore_wordlist_comments:
            command.append("-ic")
        if args.follow_redirects:
            command.append("-r")
        if args.http2:
            command.append("-http2")
        if args.ignore_body:
            command.append("-ignore-body")
        if args.proxy:
            command.extend(["-x", args.proxy])
        if args.replay_proxy:
            command.extend(["-replay-proxy", args.replay_proxy])
        if args.ignore_tls:
            command.append("-k")
        if args.recursion:
            command.append("-recursion")
            if args.recursion_depth:
                command.extend(["-recursion-depth", str(args.recursion_depth)])
            command.extend(["-recursion-strategy", args.recursion_strategy])
        if args.delay:
            command.extend(["-p", args.delay])
        if args.rate:
            command.extend(["-rate", str(args.rate)])
        if args.job_max_time:
            command.extend(["-maxtime", str(args.job_max_time)])
        if args.job_max_time_per_recursion:
            command.extend(["-maxtime-job", str(args.job_max_time_per_recursion)])

        for header in args.headers:
            command.extend(["-H", header])
        if args.basic_auth:
            command.extend(["-H", basic_auth_header(args.basic_auth.user, args.basic_auth.password)])
        if args.cookies:
            command.extend(["-b", args.cookies])

        command.extend(build_matcher_filter_args(args))

        if args.auto_calibrate:
            command.append("-ac")
        if args.auto_calibrate_per_host:
            command.append("-ach")
        if args.auto_calibrate_keyword:
            command.extend(["-ack", args.auto_calibrate_keyword])
        for value in args.auto_calibrate_strings:
            command.extend(["-acc", value])
        for value in args.auto_calibrate_strategies:
            command.extend(["-acs", value])
        if args.stop_on_403:
            command.append("-sf")
        if args.stop_on_errors:
            command.append("-se")
        if args.stop_on_any:
            command.append("-sa")

        return command

    def build_command(self, args: FfufArgs) -> List[str]:
        return self._build_command(args)

    def parse_output(
        self, stdout: str, stderr: str, exit_code: int, args: FfufArgs
    ) -> Dict[str, Any]:
        """Parse ffuf JSON or text output into a consistent metadata shape."""

        stripped = (stdout or "").strip()
        metadata = (
            parse_ffuf_json_text(stripped)
            if stripped.startswith("{") or stripped.startswith("[")
            else parse_ffuf_text(stdout or "", target_template=args.target)
        )
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = clean_output(stderr)
        return metadata

    def create_artifacts(
        self, stdout: str, args: FfufArgs, timestamp: Optional[float] = None
    ) -> List[str]:
        """Crawler output is captured on stdout; generic runtime artifacting persists it."""

        _ = stdout, args, timestamp
        return []

    def postprocess_execution(
        self,
        *,
        args: FfufArgs,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        _ = args, runtime_context
        return ToolPostprocessResult(
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            metadata=dict(metadata or {}),
            artifacts=list(artifacts or []),
        )

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FfufArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code
        metadata.setdefault("ffuf_variant", "crawler")
        return build_ffuf_semantic_observations(metadata, args)

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FfufArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        _ = stdout, stderr, exit_code
        metadata.setdefault("ffuf_variant", "crawler")
        return build_ffuf_semantic_evidence(metadata, args)

    def _subprocess_timeout(self, args: FfufArgs) -> int:
        return args.job_max_time or (args.threads * args.request_timeout + 60)

    def run(self, args: FfufArgs) -> ToolResult:
        start_time = time.time()
        timestamp = start_time
        command = self._build_command(args, timestamp=timestamp)
        artifacts: List[str] = []

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._subprocess_timeout(args),
            )
        except subprocess.TimeoutExpired:
            metadata = {
                "results": [],
                "timeout": {
                    "message": "ffuf timed out; reduce the wordlist size, lower rate, or raise job_max_time.",
                },
            }
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=artifacts,
                metadata=metadata,
                execution_time=time.time() - start_time,
            )

        metadata = self.parse_output(process.stdout, process.stderr, process.returncode, args)
        stdout_value = process.stdout
        artifacts = self.create_artifacts(process.stdout, args, timestamp)

        metadata["exit_code"] = process.returncode
        if process.stderr:
            metadata["stderr"] = clean_output(process.stderr)

        return ToolResult(
            success=process.returncode == 0,
            exit_code=process.returncode,
            stdout=stdout_value,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start_time,
        )


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_crawlers.ffuf",
        display_name="FFUF (Crawler)",
        category=ToolCategory.WEB_CRAWLING,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="directory_enumeration",
                description="Discover web paths by fuzzing a URL template containing FUZZ with a wordlist; use for directory/content enumeration, not input fuzzing",
                output_indicators=["results", "status", "url"],
            ),
            ToolCapability(
                name="recursive_enumeration",
                description="Use ffuf recursion safely when the target path ends with /FUZZ and recursion depth is explicit.",
                output_indicators=["recursion", "recursion-depth", "results"],
            ),
            ToolCapability(
                name="authenticated_enumeration",
                description="Enumerate authenticated content with headers, cookies, or basic auth without inventing fake ffuf flags.",
                output_indicators=["Authorization", "Set-Cookie", "results"],
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
