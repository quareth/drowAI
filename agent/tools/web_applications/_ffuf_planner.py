"""Planner-facing ffuf schemas and compiler helpers.

This module defines guidance-heavy planner contracts for ffuf crawler and
fuzzer tools, plus the compiler logic that turns those planner-facing payloads
into the existing strict execution arguments consumed by the runtime ffuf
implementations.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Sequence, Union
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._ffuf_common import (
    resolve_workspace_file_path,
    resolve_wordlist_reference,
    validate_delay,
    validate_fuzz_keyword_present,
    validate_http_target,
)

PayloadFamily = Literal["paths", "parameter_names", "extensions", "vhosts", "common_values"]
PayloadProfile = Literal["small", "medium", "large"]
FuzzSurface = Literal["path", "query", "header", "cookie", "body", "raw_request", "multi_surface"]
FuzzerSurface = Literal["path", "query", "header", "cookie", "body", "raw_request", "multi_surface"]
CrawlerSurface = Literal["path"]

FFUF_PAYLOAD_CATALOG: Dict[str, Dict[str, str]] = {
    "paths": {
        "small": "/usr/share/seclists/Discovery/Web-Content/common.txt",
        "medium": "/usr/share/seclists/Discovery/Web-Content/raft-small-directories.txt",
        "large": "/usr/share/seclists/Discovery/Web-Content/directory-list-2.3-medium.txt",
    },
    "parameter_names": {
        "small": "/usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt",
        "medium": "/usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt",
        "large": "/usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt",
    },
    "extensions": {
        "small": "/usr/share/seclists/Discovery/Web-Content/web-extensions.txt",
        "medium": "/usr/share/seclists/Discovery/Web-Content/web-extensions.txt",
        "large": "/usr/share/seclists/Discovery/Web-Content/web-extensions-big.txt",
    },
    "vhosts": {
        "small": "/usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt",
        "medium": "/usr/share/seclists/Discovery/DNS/subdomains-top1million-20000.txt",
        "large": "/usr/share/seclists/Discovery/DNS/subdomains-top1million-110000.txt",
    },
    "common_values": {
        "small": "/usr/share/seclists/Fuzzing/interesting-values.txt",
        "medium": "/usr/share/seclists/Fuzzing/interesting-values.txt",
        "large": "/usr/share/seclists/Fuzzing/interesting-values.txt",
    },
}

SURFACE_FAMILY_ALLOWLIST: Dict[str, set[str]] = {
    "path": {"paths", "extensions", "common_values"},
    "query": {"parameter_names", "common_values"},
    "header": {"vhosts", "common_values"},
    "cookie": {"common_values"},
    "body": {"parameter_names", "common_values"},
    "raw_request": {"paths", "parameter_names", "extensions", "vhosts", "common_values"},
    "multi_surface": {"paths", "parameter_names", "extensions", "vhosts", "common_values"},
}

FFUF_CRAWLER_PLANNER_GUIDANCE = (
    "Decision order: choose the path target template, choose one payload_source, then configure "
    "response_strategy and runtime_controls. Supported payload sources: catalog, inline_values, "
    "generated_sequence, custom_wordlist. Prefer semantic catalog families over raw paths. "
    "Anti-patterns: do not mix payload sources, do not omit FUZZ from the path template, and do not "
    "use path discovery catalogs for non-path workflows."
)

FFUF_FUZZER_PLANNER_GUIDANCE = (
    "Decision order: choose fuzz_surface, set the target/request templates for that surface, choose "
    "one payload_source, then configure response_strategy and runtime_controls. Supported payload "
    "sources: catalog, catalog_combo, inline_values, generated_sequence, custom_wordlist, "
    "custom_named_wordlists. Prefer semantic catalog families over raw paths. Anti-patterns: do not "
    "mix payload sources, do not supply raw shell in planner args, and do not place FUZZ/keywords in "
    "a different surface than the declared fuzz_surface."
)


class PlannerModel(BaseModel):
    """Shared strict base model for planner-facing ffuf schemas."""

    model_config = ConfigDict(extra="forbid")


class PlannerBasicAuthArgs(PlannerModel):
    """Planner-facing basic-auth input."""

    user: str = Field(..., min_length=1, description="Username for HTTP basic authentication.")
    password: str = Field(..., min_length=1, description="Password for HTTP basic authentication.")


class PlannerResponseSelectors(PlannerModel):
    """Human-readable matcher or filter selectors."""

    status_codes: Optional[str] = Field(None, description="HTTP status selection string such as '200,204,301'.")
    line_counts: Optional[str] = Field(None, description="Line-count selection string accepted by ffuf.")
    word_counts: Optional[str] = Field(None, description="Word-count selection string accepted by ffuf.")
    response_sizes: Optional[str] = Field(None, description="Response-size selection string accepted by ffuf.")
    first_byte_time: Optional[str] = Field(None, description="First-byte timing selector such as '>100' or '<50'.")
    regex: Optional[str] = Field(None, description="Regular expression used to match or filter responses.")


class PlannerCalibrationSettings(PlannerModel):
    """Manual calibration controls when automatic defaults are not enough."""

    per_host: bool = Field(False, description="Enable per-host calibration.")
    strings: List[str] = Field(default_factory=list, description="Custom calibration probe strings.")
    strategies: List[str] = Field(default_factory=list, description="Calibration strategies such as 'basic'.")
    keyword: Optional[str] = Field(None, description="Override calibration keyword when needed.")


class PlannerResponseStrategy(PlannerModel):
    """Response triage strategy for noisy or repetitive targets."""

    calibration_mode: Literal["automatic", "off", "manual"] = Field(
        "off",
        description=(
            "Controls ffuf auto-calibration (-ac). Default 'off' preserves every "
            "server response in the output. "
            "'off': capture all responses verbatim. Use for small or explicit "
            "enumerations (~<=100 payloads), when investigating uniform/redirect "
            "responses, or whenever per-path evidence matters more than noise "
            "reduction. "
            "'automatic': enable -ac. ffuf first probes the target with random "
            "values to learn a 'not found' baseline (response size/words/lines), "
            "then SILENTLY DROPS every real response matching that baseline. "
            "Use for large wordlist scans (hundreds-to-thousands of payloads) "
            "against servers that return boilerplate 404 pages. WARNING: "
            "filtered responses are not recorded anywhere - if real targets "
            "happen to share the noise shape (e.g. uniform 302 redirects, "
            "auth-walled routes) they will all be lost from the output. "
            "'manual': enable -ac with custom strings/strategies via "
            "calibration_settings. Use when the noise tokens or strategies are "
            "known up front."
        ),
    )
    calibration_settings: Optional[PlannerCalibrationSettings] = Field(
        None,
        description="Manual calibration settings. Only valid when calibration_mode is 'manual'.",
    )
    match: Optional[PlannerResponseSelectors] = Field(
        None,
        description="Optional response matching selectors.",
    )
    filter: Optional[PlannerResponseSelectors] = Field(
        None,
        description="Optional response filtering selectors.",
    )
    match_mode: Literal["and", "or"] = Field("or", description="Matcher set operator.")
    filter_mode: Literal["and", "or"] = Field("or", description="Filter set operator.")
    stop_on_403: bool = Field(False, description="Stop if the target becomes predominantly forbidden.")
    stop_on_errors: bool = Field(False, description="Stop on repeated spurious errors.")
    stop_on_any: bool = Field(False, description="Stop on the first major error condition.")

    @model_validator(mode="after")
    def _validate_manual_calibration(self) -> "PlannerResponseStrategy":
        if self.calibration_mode == "manual":
            return self
        if self.calibration_settings is not None:
            raise ValueError("calibration_settings is only valid when calibration_mode='manual'.")
        return self


class PlannerRuntimeControls(PlannerModel):
    """Planner-facing runtime controls."""

    threads: int = Field(40, ge=1, le=100, description="Concurrent ffuf threads.")
    request_timeout_seconds: int = Field(10, ge=1, le=300, description="Per-request timeout in seconds.")
    rate_limit: Optional[int] = Field(None, ge=1, le=10_000, description="Optional requests-per-second cap.")
    delay: Optional[str] = Field(
        None,
        description="Optional ffuf-compatible delay such as '0.1' or '0.1-2.0'.",
    )
    max_runtime_seconds: Optional[int] = Field(None, ge=1, le=86_400, description="Optional whole-job timeout.")

    @field_validator("delay")
    @classmethod
    def _validate_delay(cls, value: Optional[str]) -> Optional[str]:
        return validate_delay(value)


class PlannerAdvancedOptions(PlannerModel):
    """Less-common ffuf execution controls kept out of the main planning flow."""

    follow_redirects: bool = Field(False, description="Follow redirects.")
    http2: bool = Field(False, description="Use HTTP/2.")
    ignore_body: bool = Field(False, description="Skip response bodies to save bandwidth.")
    ignore_tls: bool = Field(False, description="Ignore TLS verification errors.")
    proxy: Optional[str] = Field(None, description="Optional upstream proxy URL.")
    replay_proxy: Optional[str] = Field(None, description="Optional replay proxy URL.")
    ignore_wordlist_comments: bool = Field(False, description="Ignore commented lines in wordlists.")
    append_extensions: Optional[str] = Field(
        None,
        description="Optional comma-separated extension list appended with ffuf -e.",
    )


class CrawlerAdvancedOptions(PlannerAdvancedOptions):
    """Crawler-specific advanced options."""

    dirsearch_compat: bool = Field(False, description="Enable DirSearch-compatible wordlist handling.")
    silent: bool = Field(
        False,
        description="Use ffuf -s only when concise matched-payload stdout is desired.",
    )

class CrawlerRecursionSettings(PlannerModel):
    """Optional recursive path enumeration settings."""

    enabled: bool = Field(False, description="Enable recursive path enumeration.")
    depth: int = Field(0, ge=0, le=20, description="Maximum recursion depth.")
    strategy: Literal["default", "greedy"] = Field("default", description="Recursion strategy.")
    max_runtime_per_job_seconds: Optional[int] = Field(
        None,
        ge=1,
        le=86_400,
        description="Optional timeout per recursion job.",
    )

    @model_validator(mode="after")
    def _validate_depth_usage(self) -> "CrawlerRecursionSettings":
        if self.depth and not self.enabled:
            raise ValueError("Set recursion.enabled=true before supplying recursion.depth.")
        if self.max_runtime_per_job_seconds and not self.enabled:
            raise ValueError("max_runtime_per_job_seconds is only valid when recursion.enabled=true.")
        return self


class CrawlerRequestShape(PlannerModel):
    """Headers and auth used during path enumeration."""

    header_templates: List[str] = Field(default_factory=list, description="Repeated HTTP headers using 'Name: Value' format.")
    cookie_template: Optional[str] = Field(None, description="Cookie header content.")
    basic_auth: Optional[PlannerBasicAuthArgs] = Field(None, description="Optional HTTP basic authentication.")

    @field_validator("header_templates")
    @classmethod
    def _validate_headers(cls, value: List[str]) -> List[str]:
        for header in value:
            if ":" not in header:
                raise ValueError(f"header_templates must use 'Name: Value' format. Invalid header: {header!r}")
        return value


class FuzzerRequestShape(PlannerModel):
    """Request parts that can carry fuzz keywords."""

    method: Literal["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"] = Field(
        "GET",
        description="HTTP method.",
    )
    header_templates: List[str] = Field(default_factory=list, description="Repeated HTTP headers using 'Name: Value' format.")
    cookie_template: Optional[str] = Field(None, description="Cookie header content. May contain FUZZ or named keywords.")
    body_template: Optional[str] = Field(None, description="Request body template. May contain FUZZ or named keywords.")
    basic_auth: Optional[PlannerBasicAuthArgs] = Field(None, description="Optional HTTP basic authentication.")
    raw_request_file: Optional[str] = Field(
        None,
        description="Workspace-relative raw HTTP request template file used with ffuf -request.",
    )
    request_proto: Literal["http", "https"] = Field("https", description="Protocol paired with raw_request_file.")

    @field_validator("header_templates")
    @classmethod
    def _validate_headers(cls, value: List[str]) -> List[str]:
        for header in value:
            if ":" not in header:
                raise ValueError(f"header_templates must use 'Name: Value' format. Invalid header: {header!r}")
        return value


class CatalogPayloadSource(PlannerModel):
    """Single semantic payload catalog selection."""

    kind: Literal["catalog"] = "catalog"
    family: PayloadFamily = Field(..., description="Semantic payload family, not a raw wordlist path.")
    profile: PayloadProfile = Field("medium", description="Small, medium, or large catalog profile.")
    keyword: Optional[str] = Field(
        None,
        description="Optional named keyword when the fuzz template uses something other than FUZZ.",
    )


class CatalogComboItem(PlannerModel):
    """Named semantic payload catalog used in multi-keyword fuzzing."""

    family: PayloadFamily = Field(..., description="Semantic payload family for this named source.")
    keyword: str = Field(..., min_length=1, description="Keyword placed in target, headers, cookies, or body.")
    profile: PayloadProfile = Field("medium", description="Small, medium, or large catalog profile.")

    @field_validator("keyword")
    @classmethod
    def _validate_keyword(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("keyword must be alphanumeric or underscore")
        return value


class CatalogComboPayloadSource(PlannerModel):
    """Multiple semantic catalogs combined with named ffuf keywords."""

    kind: Literal["catalog_combo"] = "catalog_combo"
    items: List[CatalogComboItem] = Field(..., min_length=2, description="Named semantic catalog sources.")
    combo_mode: Literal["clusterbomb", "pitchfork", "sniper"] = Field(
        "clusterbomb",
        description="ffuf combination mode for named sources.",
    )


class InlineValuesPayloadSource(PlannerModel):
    """Concrete inline payload values materialized into a workspace wordlist."""

    kind: Literal["inline_values"] = "inline_values"
    values: List[str] = Field(..., min_length=1, description="Concrete payload values.")

    @field_validator("values")
    @classmethod
    def _validate_values(cls, value: List[str]) -> List[str]:
        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("inline_values may not contain empty strings.")
        if any(item == "..." for item in normalized):
            raise ValueError("inline_values must contain concrete payloads, not placeholder markers such as '...'.")
        return normalized


class GeneratedSequencePayloadSource(PlannerModel):
    """Vetted payload generation for bounded numeric sweeps."""

    kind: Literal["generated_sequence"] = "generated_sequence"
    sequence_kind: Literal["numeric_range"] = Field("numeric_range", description="Generated sequence strategy.")
    start: int = Field(..., description="Inclusive range start.")
    end: int = Field(..., description="Inclusive range end.")
    step: int = Field(1, ge=1, description="Positive range step.")

    @model_validator(mode="after")
    def _validate_range(self) -> "GeneratedSequencePayloadSource":
        if self.end < self.start:
            raise ValueError("generated_sequence end must be greater than or equal to start.")
        return self


class CustomWordlistPayloadSource(PlannerModel):
    """Advanced custom wordlist path."""

    kind: Literal["custom_wordlist"] = "custom_wordlist"
    path: str = Field(..., min_length=1, description="Workspace-relative or approved in-container wordlist path.")
    keyword: Optional[str] = Field(
        None,
        description="Optional named keyword when the fuzz template uses something other than FUZZ.",
    )


class CustomNamedWordlistItem(PlannerModel):
    """Advanced named custom wordlist entry."""

    path: str = Field(..., min_length=1, description="Workspace-relative or approved in-container wordlist path.")
    keyword: str = Field(..., min_length=1, description="Keyword placed in target, headers, cookies, or body.")

    @field_validator("keyword")
    @classmethod
    def _validate_keyword(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("keyword must be alphanumeric or underscore")
        return value


class CustomNamedWordlistsPayloadSource(PlannerModel):
    """Advanced named custom wordlists combined in one ffuf run."""

    kind: Literal["custom_named_wordlists"] = "custom_named_wordlists"
    items: List[CustomNamedWordlistItem] = Field(..., min_length=2, description="Named custom wordlists.")
    combo_mode: Literal["clusterbomb", "pitchfork", "sniper"] = Field(
        "clusterbomb",
        description="ffuf combination mode for named sources.",
    )


CrawlerPayloadSource = Annotated[
    Union[
        CatalogPayloadSource,
        InlineValuesPayloadSource,
        GeneratedSequencePayloadSource,
        CustomWordlistPayloadSource,
    ],
    Field(discriminator="kind"),
]

FuzzerPayloadSource = Annotated[
    Union[
        CatalogPayloadSource,
        CatalogComboPayloadSource,
        InlineValuesPayloadSource,
        GeneratedSequencePayloadSource,
        CustomWordlistPayloadSource,
        CustomNamedWordlistsPayloadSource,
    ],
    Field(discriminator="kind"),
]


class FfufCrawlerPlannerArgs(PlannerModel):
    """Planner-facing crawler contract focused on path enumeration."""

    fuzz_surface: CrawlerSurface = Field(
        "path",
        description="Crawler ffuf enumerates paths only.",
    )
    target_template: str = Field(
        ...,
        description="Absolute target URL template ending with '/FUZZ' or '/FUZZ/' for path enumeration.",
    )
    payload_source: CrawlerPayloadSource = Field(..., description="Exactly one payload source.")
    request_shape: CrawlerRequestShape = Field(default_factory=CrawlerRequestShape)
    response_strategy: PlannerResponseStrategy = Field(default_factory=PlannerResponseStrategy)
    runtime_controls: PlannerRuntimeControls = Field(default_factory=PlannerRuntimeControls)
    recursion: CrawlerRecursionSettings = Field(default_factory=CrawlerRecursionSettings)
    advanced: CrawlerAdvancedOptions = Field(default_factory=CrawlerAdvancedOptions)

    @field_validator("target_template")
    @classmethod
    def _validate_target_template(cls, value: str) -> str:
        normalized = validate_http_target(value)
        if not (normalized.endswith("/FUZZ") or normalized.endswith("/FUZZ/")):
            raise ValueError("Crawler target_template must end with '/FUZZ' or '/FUZZ/'.")
        return normalized

    @model_validator(mode="after")
    def _validate_surface_and_payload(self) -> "FfufCrawlerPlannerArgs":
        _validate_payload_source_for_surface(self.fuzz_surface, self.payload_source)
        _validate_surface_placement(
            fuzz_surface=self.fuzz_surface,
            target_template=self.target_template,
            headers=self.request_shape.header_templates,
            body_template=None,
            cookie_template=self.request_shape.cookie_template,
            raw_request_file=None,
            keywords=_payload_keywords(self.payload_source),
        )
        return self


class FfufFuzzerPlannerArgs(PlannerModel):
    """Planner-facing fuzzer contract for value and request-part fuzzing."""

    fuzz_surface: FuzzerSurface = Field(
        ...,
        description="Declare where the fuzz keywords live: path, query, header, cookie, body, raw_request, or multi_surface.",
    )
    target_template: str = Field(
        ...,
        description="Absolute target URL template. Keep it stable unless the declared fuzz_surface is path, query, or multi_surface.",
    )
    payload_source: FuzzerPayloadSource = Field(..., description="Exactly one payload source.")
    request_shape: FuzzerRequestShape = Field(default_factory=FuzzerRequestShape)
    response_strategy: PlannerResponseStrategy = Field(default_factory=PlannerResponseStrategy)
    runtime_controls: PlannerRuntimeControls = Field(default_factory=PlannerRuntimeControls)
    advanced: PlannerAdvancedOptions = Field(default_factory=PlannerAdvancedOptions)

    @field_validator("target_template")
    @classmethod
    def _validate_target_template(cls, value: str) -> str:
        return validate_http_target(value)

    @model_validator(mode="after")
    def _validate_surface_and_payload(self) -> "FfufFuzzerPlannerArgs":
        _validate_payload_source_for_surface(self.fuzz_surface, self.payload_source)
        _validate_surface_placement(
            fuzz_surface=self.fuzz_surface,
            target_template=self.target_template,
            headers=self.request_shape.header_templates,
            body_template=self.request_shape.body_template,
            cookie_template=self.request_shape.cookie_template,
            raw_request_file=self.request_shape.raw_request_file,
            keywords=_payload_keywords(self.payload_source),
        )
        return self


def resolve_payload_catalog_path(family: PayloadFamily, profile: PayloadProfile) -> str:
    """Resolve a semantic payload family and profile to a concrete wordlist path."""

    return FFUF_PAYLOAD_CATALOG[family][profile]


def compile_crawler_planner_args(
    planner_args: FfufCrawlerPlannerArgs,
    *,
    action_target: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile crawler planner args into execution-facing ffuf crawler args."""

    _ = action_target
    compiled: Dict[str, Any] = {
        "target": planner_args.target_template,
        **_compile_payload_source(planner_args.payload_source),
        **_compile_common_request_shape(planner_args.request_shape),
        **_compile_response_strategy(planner_args.response_strategy),
        **_compile_runtime_controls(planner_args.runtime_controls),
        **_compile_advanced_options(planner_args.advanced),
    }
    if planner_args.advanced.dirsearch_compat:
        compiled["dirsearch_compat"] = True
    if planner_args.advanced.silent:
        compiled["silent"] = True
    if planner_args.recursion.enabled:
        compiled["recursion"] = True
        if planner_args.recursion.depth:
            compiled["recursion_depth"] = planner_args.recursion.depth
        if planner_args.recursion.strategy != "default":
            compiled["recursion_strategy"] = planner_args.recursion.strategy
        if planner_args.recursion.max_runtime_per_job_seconds:
            compiled["job_max_time_per_recursion"] = planner_args.recursion.max_runtime_per_job_seconds
    _validate_compiled_custom_sources(planner_args.payload_source)
    return compiled


def compile_fuzzer_planner_args(
    planner_args: FfufFuzzerPlannerArgs,
    *,
    action_target: Optional[str] = None,
) -> Dict[str, Any]:
    """Compile fuzzer planner args into execution-facing ffuf fuzzer args."""

    _ = action_target
    compiled: Dict[str, Any] = {
        "target": planner_args.target_template,
        **_compile_payload_source(planner_args.payload_source),
        **_compile_fuzzer_request_shape(planner_args.request_shape),
        **_compile_response_strategy(planner_args.response_strategy),
        **_compile_runtime_controls(planner_args.runtime_controls),
        **_compile_advanced_options(planner_args.advanced),
    }
    _validate_compiled_custom_sources(planner_args.payload_source)
    if planner_args.request_shape.raw_request_file:
        resolve_workspace_file_path(planner_args.request_shape.raw_request_file)
    return compiled


def _compile_common_request_shape(shape: CrawlerRequestShape) -> Dict[str, Any]:
    compiled: Dict[str, Any] = {}
    if shape.header_templates:
        compiled["headers"] = list(shape.header_templates)
    if shape.cookie_template:
        compiled["cookies"] = shape.cookie_template
    if shape.basic_auth:
        compiled["basic_auth"] = shape.basic_auth.model_dump()
    return compiled


def _compile_fuzzer_request_shape(shape: FuzzerRequestShape) -> Dict[str, Any]:
    compiled = _compile_common_request_shape(shape)
    compiled["method"] = shape.method
    if shape.body_template:
        compiled["data"] = shape.body_template
    if shape.raw_request_file:
        compiled["raw_request_file"] = shape.raw_request_file
        compiled["request_proto"] = shape.request_proto
    return compiled


def _compile_response_strategy(strategy: PlannerResponseStrategy) -> Dict[str, Any]:
    compiled: Dict[str, Any] = {}
    if strategy.calibration_mode == "automatic":
        compiled["auto_calibrate"] = True
    elif strategy.calibration_mode == "manual":
        compiled["auto_calibrate"] = True
        settings = strategy.calibration_settings or PlannerCalibrationSettings()
        if settings.per_host:
            compiled["auto_calibrate_per_host"] = True
        if settings.strings:
            compiled["auto_calibrate_strings"] = list(settings.strings)
        if settings.strategies:
            compiled["auto_calibrate_strategies"] = list(settings.strategies)
        if settings.keyword:
            compiled["auto_calibrate_keyword"] = settings.keyword
    if strategy.match:
        compiled.update(
            {
                "match_status": strategy.match.status_codes,
                "match_lines": strategy.match.line_counts,
                "match_words": strategy.match.word_counts,
                "match_size": strategy.match.response_sizes,
                "match_time": strategy.match.first_byte_time,
                "match_regex": strategy.match.regex,
            }
        )
    if strategy.filter:
        compiled.update(
            {
                "filter_status": strategy.filter.status_codes,
                "filter_lines": strategy.filter.line_counts,
                "filter_words": strategy.filter.word_counts,
                "filter_size": strategy.filter.response_sizes,
                "filter_time": strategy.filter.first_byte_time,
                "filter_regex": strategy.filter.regex,
            }
        )
    if strategy.match_mode != "or":
        compiled["matcher_mode"] = strategy.match_mode
    if strategy.filter_mode != "or":
        compiled["filter_mode"] = strategy.filter_mode
    if strategy.stop_on_403:
        compiled["stop_on_403"] = True
    if strategy.stop_on_errors:
        compiled["stop_on_errors"] = True
    if strategy.stop_on_any:
        compiled["stop_on_any"] = True
    return {key: value for key, value in compiled.items() if value is not None}


def _compile_runtime_controls(controls: PlannerRuntimeControls) -> Dict[str, Any]:
    compiled: Dict[str, Any] = {
        "threads": controls.threads,
        "request_timeout": controls.request_timeout_seconds,
    }
    if controls.rate_limit:
        compiled["rate"] = controls.rate_limit
    if controls.delay:
        compiled["delay"] = controls.delay
    if controls.max_runtime_seconds:
        compiled["job_max_time"] = controls.max_runtime_seconds
    return compiled


def _compile_advanced_options(options: PlannerAdvancedOptions) -> Dict[str, Any]:
    compiled: Dict[str, Any] = {}
    if options.follow_redirects:
        compiled["follow_redirects"] = True
    if options.http2:
        compiled["http2"] = True
    if options.ignore_body:
        compiled["ignore_body"] = True
    if options.ignore_tls:
        compiled["ignore_tls"] = True
    if options.proxy:
        compiled["proxy"] = options.proxy
    if options.replay_proxy:
        compiled["replay_proxy"] = options.replay_proxy
    if options.ignore_wordlist_comments:
        compiled["ignore_wordlist_comments"] = True
    if options.append_extensions:
        compiled["extensions"] = options.append_extensions
    return compiled


def _compile_payload_source(source: Any) -> Dict[str, Any]:
    if isinstance(source, CatalogPayloadSource):
        path = resolve_payload_catalog_path(source.family, source.profile)
        if source.keyword:
            return {"wordlist": f"{path}:{source.keyword}"}
        return {"wordlist": path}

    if isinstance(source, CatalogComboPayloadSource):
        return {
            "wordlists": [
                {"path": resolve_payload_catalog_path(item.family, item.profile), "keyword": item.keyword}
                for item in source.items
            ],
            "combo_mode": source.combo_mode,
        }

    if isinstance(source, InlineValuesPayloadSource):
        return {"inline_wordlist": [str(item) for item in source.values]}

    if isinstance(source, GeneratedSequencePayloadSource):
        values = [str(value) for value in range(source.start, source.end + 1, source.step)]
        return {"inline_wordlist": values}

    if isinstance(source, CustomWordlistPayloadSource):
        if source.keyword:
            return {"wordlist": f"{source.path}:{source.keyword}"}
        return {"wordlist": source.path}

    if isinstance(source, CustomNamedWordlistsPayloadSource):
        return {
            "wordlists": [{"path": item.path, "keyword": item.keyword} for item in source.items],
            "combo_mode": source.combo_mode,
        }

    raise ValueError("Unsupported payload_source")

def _payload_keywords(source: Any) -> List[str]:
    if isinstance(source, (InlineValuesPayloadSource, GeneratedSequencePayloadSource)):
        return ["FUZZ"]
    if isinstance(source, CatalogPayloadSource):
        return [source.keyword or "FUZZ"]
    if isinstance(source, CatalogComboPayloadSource):
        return [item.keyword for item in source.items]
    if isinstance(source, CustomWordlistPayloadSource):
        return [source.keyword or "FUZZ"]
    if isinstance(source, CustomNamedWordlistsPayloadSource):
        return [item.keyword for item in source.items]
    return ["FUZZ"]


def _validate_payload_source_for_surface(fuzz_surface: FuzzSurface, payload_source: Any) -> None:
    allowed_families = SURFACE_FAMILY_ALLOWLIST[fuzz_surface]
    families = _payload_families(payload_source)
    unsupported = [family for family in families if family not in allowed_families]
    if unsupported:
        joined = ", ".join(sorted(set(unsupported)))
        raise ValueError(
            f"payload_source family/families {joined} are not appropriate for fuzz_surface='{fuzz_surface}'."
        )


def _payload_families(source: Any) -> List[str]:
    if isinstance(source, CatalogPayloadSource):
        return [source.family]
    if isinstance(source, CatalogComboPayloadSource):
        return [item.family for item in source.items]
    return []


def _validate_surface_placement(
    *,
    fuzz_surface: FuzzSurface,
    target_template: str,
    headers: Sequence[str],
    body_template: Optional[str],
    cookie_template: Optional[str],
    raw_request_file: Optional[str],
    keywords: Sequence[str],
) -> None:
    if fuzz_surface == "multi_surface":
        validate_fuzz_keyword_present(
            target_template,
            headers,
            body_template,
            cookie_template,
            keywords,
            raw_request_file=raw_request_file,
        )
        populated_surfaces = [
            surface
            for surface in ("path", "query", "header", "cookie", "body", "raw_request")
            if any(_surface_contains_keyword(surface, keyword, target_template, headers, body_template, cookie_template, raw_request_file) for keyword in keywords)
        ]
        if len(populated_surfaces) < 2:
            raise ValueError("multi_surface requires fuzz keywords to appear in at least two different surfaces.")
        return

    missing = [
        keyword
        for keyword in keywords
        if not _surface_contains_keyword(
            fuzz_surface,
            keyword,
            target_template,
            headers,
            body_template,
            cookie_template,
            raw_request_file,
        )
    ]
    if missing:
        joined = ", ".join(sorted(set(missing)))
        raise ValueError(f"Declared fuzz keyword(s) {joined} are missing from the declared fuzz_surface '{fuzz_surface}'.")


def _surface_contains_keyword(
    surface: FuzzSurface,
    keyword: str,
    target_template: str,
    headers: Sequence[str],
    body_template: Optional[str],
    cookie_template: Optional[str],
    raw_request_file: Optional[str],
) -> bool:
    parsed = urlparse(target_template)
    if surface == "path":
        return keyword in (parsed.path or "")
    if surface == "query":
        return keyword in (parsed.query or "")
    if surface == "header":
        return any(keyword in header for header in headers or [])
    if surface == "cookie":
        return keyword in (cookie_template or "")
    if surface == "body":
        return keyword in (body_template or "")
    if surface == "raw_request":
        if not raw_request_file:
            return False
        request_text = _read_request_template(raw_request_file)
        return keyword in request_text
    if surface == "multi_surface":
        return any(
            _surface_contains_keyword(name, keyword, target_template, headers, body_template, cookie_template, raw_request_file)
            for name in ("path", "query", "header", "cookie", "body", "raw_request")
        )
    return False


def _read_request_template(relative_path: str) -> str:
    path = resolve_workspace_file_path(relative_path)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _validate_compiled_custom_sources(source: Any) -> None:
    if isinstance(source, CustomWordlistPayloadSource):
        reference = f"{source.path}:{source.keyword}" if source.keyword else source.path
        resolve_wordlist_reference(reference)
    elif isinstance(source, CustomNamedWordlistsPayloadSource):
        for item in source.items:
            resolve_wordlist_reference(f"{item.path}:{item.keyword}")


__all__ = [
    "FfufCrawlerPlannerArgs",
    "FfufFuzzerPlannerArgs",
    "FFUF_CRAWLER_PLANNER_GUIDANCE",
    "FFUF_FUZZER_PLANNER_GUIDANCE",
    "compile_crawler_planner_args",
    "compile_fuzzer_planner_args",
]
