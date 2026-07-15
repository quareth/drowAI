"""First-class ffuf fuzzer for parameters, headers, cookies, and request bodies."""

from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from runtime_shared.workspace_files import RuntimeWorkspaceFile

from ...base_tool import BaseTool, ToolPostprocessResult
from ...canonical_capture import CanonicalCaptureFormat, CaptureFamily, ToolCaptureContract
from ...schemas import ToolResult
from ...enhanced_metadata_registry import (
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
    register_enhanced_tool_metadata,
)
from .._ffuf_common import (
    basic_auth_header,
    build_matcher_filter_args,
    extract_keyword_from_wordlist,
    inline_wordlist_relative_path,
    inline_wordlist_workspace_file,
    parse_ffuf_json_text,
    parse_ffuf_text,
    resolve_wordlist_reference_for_execution,
    resolve_workspace_file_path_for_execution,
    validate_delay,
    validate_fuzz_keyword_present,
    validate_http_target,
    validate_input_cmd,
)
from .._ffuf_planner import (
    FFUF_FUZZER_PLANNER_GUIDANCE,
    FfufFuzzerPlannerArgs,
    compile_fuzzer_planner_args,
)
from .._ffuf_semantics import (
    build_ffuf_semantic_evidence,
    build_ffuf_semantic_observations,
)
from ..parsing_utils import clean_output


class BasicAuthArgs(BaseModel):
    """Structured basic-auth input rendered as an Authorization header."""

    user: str = Field(..., min_length=1, description="Username for HTTP basic authentication.")
    password: str = Field(..., min_length=1, description="Password for HTTP basic authentication.")


class WordlistSpec(BaseModel):
    """A named ffuf wordlist entry for multi-keyword fuzzing."""

    path: str = Field(
        ...,
        description=(
            "Workspace-relative wordlist path or in-container SecLists path. Use `keyword` when you need "
            "something other than ffuf's default FUZZ placeholder."
        ),
    )
    keyword: Optional[str] = Field(
        None,
        description="Optional explicit ffuf keyword for this wordlist, for example 'PARAM' or 'VALUE'.",
    )

    @field_validator("keyword")
    @classmethod
    def _validate_keyword(cls, value: Optional[str]) -> Optional[str]:
        if value and not value.replace("_", "").isalnum():
            raise ValueError("keyword must be alphanumeric or underscore")
        return value


class FfufArgs(BaseModel):
    """Accurate ffuf schema for request fuzzing outside of path recursion."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description=(
            "Absolute target URL for ffuf `-u`. Use FUZZ or explicit keywords in the URL, headers, "
            "cookies, or body to tell ffuf where payloads belong."
        ),
    )
    method: Literal["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"] = Field(
        "GET",
        description="HTTP method for ffuf `-X`.",
    )
    headers: List[str] = Field(
        default_factory=list,
        description=(
            "Repeated HTTP headers. Use this for vhost fuzzing via `Host: FUZZ` or other header payload slots."
        ),
    )
    cookies: Optional[str] = Field(
        None,
        description="Cookie string for ffuf `-b`. You can place FUZZ or a keyword inside the cookie value.",
    )
    data: Optional[str] = Field(
        None,
        description="Request body for ffuf `-d`. Supports FUZZ or declared wordlist keywords.",
    )
    basic_auth: Optional[BasicAuthArgs] = Field(
        None,
        description="Optional basic authentication rendered as an Authorization header.",
    )
    raw_request_file: Optional[str] = Field(
        None,
        description="Workspace-relative raw HTTP request file for ffuf `-request`.",
    )
    request_proto: Literal["http", "https"] = Field(
        "https",
        description="Protocol paired with `raw_request_file` for ffuf `-request-proto`.",
    )
    wordlist: Optional[str] = Field(
        None,
        description=(
            "Single ffuf `-w` wordlist path. You may append `:KEYWORD` when you want a custom placeholder."
        ),
    )
    wordlists: List[WordlistSpec] = Field(
        default_factory=list,
        description="Multi-wordlist ffuf input. Each item becomes its own `-w path:KEYWORD` flag.",
    )
    inline_wordlist: Optional[List[str]] = Field(
        None,
        description=(
            "Safe dynamic wordlist entries materialized into the workspace. Prefer this over shell when you "
            "just need concrete payloads like numeric IDs."
        ),
    )
    input_cmd: Optional[str] = Field(
        None,
        description=(
            "Advanced ffuf `-input-cmd` source for runtime-generated payloads. Use only when inline values are "
            "not practical."
        ),
    )
    input_shell: Optional[str] = Field(
        None,
        description="Shell used for `-input-cmd`, for example '/bin/sh'.",
    )
    input_num: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Required when `input_cmd` is set: number of generated payloads ffuf should consume.",
    )
    extensions: Optional[str] = Field(
        None,
        description="Optional ffuf `-e` extension list when fuzzing filenames or suffixes.",
    )
    combo_mode: Literal["clusterbomb", "pitchfork", "sniper"] = Field(
        "clusterbomb",
        description="ffuf `-mode` for multi-wordlist payload combination.",
    )
    ignore_wordlist_comments: bool = Field(
        False,
        description="Ignore comment lines in wordlists with ffuf `-ic`.",
    )
    follow_redirects: bool = Field(False, description="Follow redirects with ffuf `-r`.")
    http2: bool = Field(False, description="Enable HTTP/2 with ffuf `-http2`.")
    ignore_body: bool = Field(False, description="Use ffuf `-ignore-body` to skip response bodies.")
    proxy: Optional[str] = Field(None, description="Optional ffuf `-x` proxy URL.")
    replay_proxy: Optional[str] = Field(None, description="Optional ffuf `-replay-proxy` URL.")
    ignore_tls: bool = Field(False, description="Ignore TLS verification errors with ffuf `-k`.")
    threads: int = Field(40, ge=1, le=100, description="Concurrent ffuf threads (`-t`).")
    delay: Optional[str] = Field(
        None,
        description="ffuf `-p` delay as a float or float range, for example '0.1' or '0.1-2.0'.",
    )
    rate: Optional[int] = Field(None, ge=1, le=10_000, description="Optional ffuf `-rate` cap.")
    request_timeout: int = Field(10, ge=1, le=300, description="ffuf `-timeout` in seconds per request.")
    job_max_time: Optional[int] = Field(None, ge=1, le=86_400, description="ffuf `-maxtime` overall limit.")
    job_max_time_per_recursion: Optional[int] = Field(
        None,
        ge=1,
        le=86_400,
        description="ffuf `-maxtime-job` limit. Available for parity even though recursion is disabled here.",
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
    matcher_mode: Literal["and", "or"] = Field("or", description="ffuf `-mmode` set operator.")
    filter_mode: Literal["and", "or"] = Field("or", description="ffuf `-fmode` set operator.")
    auto_calibrate: bool = Field(False, description="Enable ffuf `-ac` auto-calibration.")
    auto_calibrate_per_host: bool = Field(False, description="Enable ffuf `-ach` per-host auto-calibration.")
    auto_calibrate_strings: List[str] = Field(
        default_factory=list,
        description="Repeated ffuf `-acc` calibration strings.",
    )
    auto_calibrate_strategies: List[str] = Field(
        default_factory=list,
        description="Repeated ffuf `-acs` strategies such as 'basic' or 'advanced'.",
    )
    auto_calibrate_keyword: Optional[str] = Field(
        None,
        description="Optional ffuf `-ack` keyword override for calibration.",
    )
    stop_on_403: bool = Field(False, description="Enable ffuf `-sf` stop-on-403.")
    stop_on_errors: bool = Field(False, description="Enable ffuf `-se` stop-on-errors.")
    stop_on_any: bool = Field(False, description="Enable ffuf `-sa` stop-on-any-error.")
    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return validate_http_target(value)

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
            bool(self.wordlists),
            bool(self.inline_wordlist),
            bool(self.input_cmd),
        ]
        if sum(input_sources) != 1:
            raise ValueError(
                "Provide exactly one payload input source: wordlist, wordlists, inline_wordlist, or input_cmd."
            )
        if self.input_cmd and not self.input_num:
            raise ValueError("input_cmd requires input_num so ffuf knows how many generated payloads to consume.")
        if self.input_shell and not self.input_cmd:
            raise ValueError("input_shell is only valid together with input_cmd.")
        if self.job_max_time_per_recursion:
            raise ValueError("ffuf fuzzer tool disables recursion; use the crawler tool for recursive path fuzzing.")

        declared_keywords: List[str] = []
        if self.wordlist:
            declared_keywords.append(extract_keyword_from_wordlist(self.wordlist))
        if self.wordlists:
            declared_keywords.extend(item.keyword or "FUZZ" for item in self.wordlists)
        if self.inline_wordlist or self.input_cmd:
            declared_keywords.append("FUZZ")
        validate_fuzz_keyword_present(
            self.target,
            self.headers,
            self.data,
            self.cookies,
            declared_keywords,
            raw_request_file=self.raw_request_file,
        )
        return self


class FfufTool(BaseTool):
    """Run ffuf in request-fuzzing mode with accurate upstream semantics."""

    args_model = FfufArgs
    planner_args_model = FfufFuzzerPlannerArgs
    planner_guidance = FFUF_FUZZER_PLANNER_GUIDANCE
    parameter_validation_policy = {"autofill_target": False}
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.STRUCTURED_NATIVE,
        canonical_format=CanonicalCaptureFormat.TEXT,
    )
    _tool_name = "fuzzer"

    @classmethod
    def compile_planner_parameters(
        cls,
        planner_args: FfufFuzzerPlannerArgs | Dict[str, Any],
        *,
        action_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        if isinstance(planner_args, FfufFuzzerPlannerArgs):
            return compile_fuzzer_planner_args(planner_args, action_target=action_target)
        parsed = FfufFuzzerPlannerArgs(**dict(planner_args or {}))
        return compile_fuzzer_planner_args(parsed, action_target=action_target)

    def _build_input_args(self, args: FfufArgs) -> List[str]:
        if args.wordlist:
            return ["-w", resolve_wordlist_reference_for_execution(args.wordlist)]
        if args.wordlists:
            command: List[str] = []
            for item in args.wordlists:
                resolved = resolve_wordlist_reference_for_execution(item.path)
                if item.keyword:
                    resolved_path = resolved.split(":", 1)[0] if ":" in resolved else resolved
                    resolved = f"{resolved_path}:{item.keyword}"
                command.extend(["-w", resolved])
            if len(args.wordlists) > 1:
                command.extend(["-mode", args.combo_mode])
            return command
        if args.inline_wordlist:
            return ["-w", f"/workspace/{self._inline_wordlist_path()}"]

        command = ["-input-cmd", args.input_cmd or "", "-input-num", str(args.input_num or 0)]
        if args.input_shell:
            command.extend(["-input-shell", args.input_shell])
        return command

    def _inline_wordlist_path(self) -> str:
        path = getattr(self, "_inline_wordlist_relative_path", None)
        if not path:
            path = inline_wordlist_relative_path(prefix="ffuf_fuzzer")
            self._inline_wordlist_relative_path = path
        return str(path)

    def prepare_workspace_files(self, args: FfufArgs) -> List[RuntimeWorkspaceFile]:
        if not args.inline_wordlist:
            return []
        return [
            inline_wordlist_workspace_file(
                args.inline_wordlist,
                relative_path=self._inline_wordlist_path(),
                description="ffuf fuzzer inline wordlist",
            )
        ]

    def _build_command(self, args: FfufArgs, *, timestamp: Optional[float] = None) -> List[str]:
        _ = timestamp
        command: List[str] = ["ffuf", "-noninteractive", "-u", args.target, "-X", args.method]
        command.extend(self._build_input_args(args))
        command.extend(["-t", str(args.threads), "-timeout", str(args.request_timeout)])

        if args.data:
            command.extend(["-d", args.data])
        if args.raw_request_file:
            command.extend(
                [
                    "-request",
                    resolve_workspace_file_path_for_execution(args.raw_request_file),
                    "-request-proto",
                    args.request_proto,
                ]
            )
        if args.extensions:
            command.extend(["-e", args.extensions])
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
        if args.delay:
            command.extend(["-p", args.delay])
        if args.rate:
            command.extend(["-rate", str(args.rate)])
        if args.job_max_time:
            command.extend(["-maxtime", str(args.job_max_time)])

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
        metadata.setdefault("ffuf_variant", "fuzzer")
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
        metadata.setdefault("ffuf_variant", "fuzzer")
        return build_ffuf_semantic_evidence(metadata, args)

    def _subprocess_timeout(self, args: FfufArgs) -> int:
        return args.job_max_time or (args.threads * args.request_timeout + 60)

    def run(self, args: FfufArgs) -> ToolResult:
        start_time = time.time()
        timestamp = start_time
        command = self._build_command(args, timestamp=timestamp)

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
                artifacts=[],
                metadata=metadata,
                execution_time=time.time() - start_time,
            )

        metadata = self.parse_output(process.stdout, process.stderr, process.returncode, args)
        artifacts = self.create_artifacts(process.stdout, args, timestamp)

        metadata["exit_code"] = process.returncode
        if process.stderr:
            metadata["stderr"] = clean_output(process.stderr)

        return ToolResult(
            success=self.is_success_exit_code(process.returncode, args),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start_time,
        )


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="web_applications.web_application_fuzzers.ffuf",
        display_name="FFUF (Fuzzer)",
        category=ToolCategory.WEB_FUZZING,
        applicable_phases=[
            PentestPhase.VULNERABILITY_ASSESSMENT,
            PentestPhase.EXPLOITATION,
        ],
        capabilities=[
            ToolCapability(
                name="parameter_fuzzing",
                description="Fuzz HTTP parameters, headers, methods, or bodies from a request template; use for input-point testing, not simple URL fetches or path discovery",
                output_indicators=["results", "status", "url"],
            ),
            ToolCapability(
                name="vhost_fuzzing",
                description="Fuzz Host headers or other request headers by placing FUZZ in repeated `headers` entries.",
                output_indicators=["Host", "results", "headers"],
            ),
            ToolCapability(
                name="body_fuzzing",
                description="Fuzz JSON, form, or raw request bodies with ffuf `-d` or `-request` without inventing unsupported flags.",
                output_indicators=["data", "results", "status"],
            ),
            ToolCapability(
                name="multi_wordlist_combination",
                description="Use ffuf `clusterbomb`, `pitchfork`, or `sniper` modes with named wordlists and honest keyword validation.",
                output_indicators=["mode", "wordlists", "results"],
            ),
            ToolCapability(
                name="response_calibration",
                description="Apply ffuf auto-calibration and matcher/filter controls to isolate meaningful findings on noisy targets.",
                output_indicators=["ac", "acc", "fc", "mc"],
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
