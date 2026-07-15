"""TShark - Command-line network protocol analyzer (Wireshark CLI)."""

from __future__ import annotations

import os
import hashlib
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from agent.utils.workspace_helpers import resolve_container_path

from ...pcap_compaction import build_pcap_compaction, render_pcap_compact_json
from ...filesystem._helpers import (
    resolve_workspace_path_safe,
    workspace_root,
)
from ...base_tool import BaseTool, ToolPostprocessResult
from ...canonical_capture import CanonicalCaptureFormat, CaptureFamily, ToolCaptureContract
from ...enhanced_metadata_registry import (
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)
from ...schemas import BaseToolArgs, ToolResult
from .tshark_semantics import (
    build_tshark_semantic_evidence,
    build_tshark_semantic_observations,
    normalize_tshark_field_extract_fields,
    parse_tshark_output,
)

DEFAULT_INTERFACE = "any"
DEFAULT_TIMEOUT = 60
EXECUTOR_WORKSPACE_ROOT = "/workspace"
TSHARK_MAX_ROWS = 1_000
TSHARK_SAFE_TARGET_PLACEHOLDER = "unused"
TSHARK_LIVE_PACKET_LIMIT = 200
TSHARK_HARD_TIMEOUT_SECONDS = 15
TSHARK_DEFAULT_SNAPLEN = 256
TSHARK_TIMEOUT_EXIT_CODE = 124


class TSharkOutputFormat(str, Enum):
    """TShark output formats."""

    TEXT = "text"
    JSON = "json"
    PDML = "pdml"
    PSML = "psml"
    FIELDS = "fields"


class TSharkAnalysisMode(str, Enum):
    """Bounded TShark analysis intents exposed to the planner."""

    SURVEY = "survey"
    ANOMALY_DETECTION = "anomaly_detection"
    INVESTIGATE_PROTOCOL = "investigate_protocol"
    EXTRACT_EVIDENCE = "extract_evidence"
    FIND_SECURITY_RELEVANT_ARTIFACTS = "find_security_relevant_artifacts"

class TSharkSensitiveProofMode(str, Enum):
    """Proof modes for sensitive packet evidence."""

    METADATA_ONLY = "metadata_only"
    PROOF_EXCERPT = "proof_excerpt"
    FINGERPRINT = "fingerprint"


_LEGACY_ANALYSIS_MODE_ALIASES = {
    "pcap_summary": TSharkAnalysisMode.SURVEY.value,
    "conversations": TSharkAnalysisMode.SURVEY.value,
    "dns": TSharkAnalysisMode.INVESTIGATE_PROTOCOL.value,
    "http": TSharkAnalysisMode.INVESTIGATE_PROTOCOL.value,
    "tls": TSharkAnalysisMode.INVESTIGATE_PROTOCOL.value,
    "auth_indicators": TSharkAnalysisMode.FIND_SECURITY_RELEVANT_ARTIFACTS.value,
    "secret_exposure": TSharkAnalysisMode.FIND_SECURITY_RELEVANT_ARTIFACTS.value,
    "field_extract": TSharkAnalysisMode.EXTRACT_EVIDENCE.value,
}
_LEGACY_PROTOCOL_ALIASES = {"dns", "http", "tls"}

_COMMON_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.protocols",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "tcp.srcport",
    "tcp.dstport",
    "tcp.stream",
    "udp.srcport",
    "udp.dstport",
]
_SURVEY_FIELDS = [
    *_COMMON_FIELDS,
    "frame.len",
    "dns.qry.name",
    "dns.flags.rcode",
    "http.host",
    "http.request.method",
    "http.request.uri",
    "http.response.code",
    "tls.handshake.extensions_server_name",
    "tls.alert_message.desc",
    "ftp.request.command",
    "smtp.req.command",
    "pop.request.command",
    "imap.request.command",
    "tcp.analysis.retransmission",
    "tcp.analysis.fast_retransmission",
    "tcp.analysis.lost_segment",
    "tcp.analysis.duplicate_ack",
    "icmp.type",
    "icmp.code",
]
_ANOMALY_FIELDS = [
    *_COMMON_FIELDS,
    "tcp.analysis.retransmission",
    "tcp.analysis.fast_retransmission",
    "tcp.analysis.lost_segment",
    "tcp.analysis.duplicate_ack",
    "icmp.type",
    "icmp.code",
    "dns.flags.rcode",
    "http.response.code",
    "tls.alert_message.desc",
]
_HTTP_FIELDS = [
    *_COMMON_FIELDS,
    "http.host",
    "http.request.method",
    "http.request.uri",
    "http.response.code",
    "http.user_agent",
    "http.content_type",
    "http.authorization",
    "http.cookie",
    "http.set_cookie",
]
_DNS_FIELDS = [
    *_COMMON_FIELDS,
    "dns.qry.name",
    "dns.qry.type",
    "dns.a",
    "dns.aaaa",
    "dns.cname",
    "dns.flags.rcode",
]
_TLS_FIELDS = [
    *_COMMON_FIELDS,
    "tls.handshake.type",
    "tls.handshake.version",
    "tls.handshake.ciphersuite",
    "tls.handshake.extensions_server_name",
    "tls.alert_message.desc",
    "x509sat.printableString",
]
_FTP_FIELDS = [
    *_COMMON_FIELDS,
    "ftp.request.command",
    "ftp.request.arg",
    "ftp.response.code",
    "ftp.response.arg",
]
_MAIL_FIELDS = [
    *_COMMON_FIELDS,
    "smtp.req.command",
    "smtp.req.parameter",
    "pop.request.command",
    "pop.request.parameter",
    "imap.request.command",
    "imap.request",
]
_SECURITY_FIELDS = [
    *_COMMON_FIELDS,
    "http.host",
    "http.request.method",
    "http.request.uri",
    "http.authorization",
    "http.cookie",
    "http.set_cookie",
    "ftp.request.command",
    "ftp.request.arg",
    "smtp.req.command",
    "smtp.req.parameter",
    "pop.request.command",
    "pop.request.parameter",
    "imap.request.command",
    "imap.request",
]
_DEFAULT_EVIDENCE_FIELDS = [*_COMMON_FIELDS, "data-text-lines"]


@dataclass(frozen=True)
class _TSharkProfile:
    """Resolved command profile for one bounded TShark intent."""

    output_format: TSharkOutputFormat
    fields: List[str]
    display_filter: Optional[str]
    max_rows: int


def _effective_workspace_root() -> Path:
    """Return the active task workspace, falling back to cwd for local tests."""

    try:
        return workspace_root()
    except OSError:
        fallback = Path(os.getenv("WORKSPACE") or Path.cwd()).resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "artifacts").mkdir(parents=True, exist_ok=True)
        return fallback


def _validate_workspace_relative_path(value: str, *, field_name: str) -> str:
    """Validate a planner/direct PCAP path before workspace resolution."""

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    normalized_posix = normalized.replace("\\", "/")
    workspace_prefix = f"{EXECUTOR_WORKSPACE_ROOT}/"
    if normalized_posix == EXECUTOR_WORKSPACE_ROOT:
        raise ValueError(f"{field_name} must point to a file under the workspace")
    if normalized_posix.startswith(workspace_prefix):
        normalized = normalized_posix[len(workspace_prefix) :]
    if Path(normalized).is_absolute():
        raise ValueError(f"{field_name} must be workspace-relative")
    if ".." in normalized.replace("\\", "/").split("/"):
        raise ValueError(f"{field_name} must not contain '..' path segments")
    return normalized


def _resolve_pcap_path_for_execution(
    relative_path: str,
    *,
    create_parent: bool = False,
) -> str:
    """Resolve a workspace-relative PCAP path to the executor-visible path."""

    normalized = _validate_workspace_relative_path(relative_path, field_name="pcap path")
    root = _effective_workspace_root()
    host_path = resolve_workspace_path_safe(normalized, workspace=root)
    if create_parent:
        host_path.parent.mkdir(parents=True, exist_ok=True)
    return resolve_container_path(
        str(host_path),
        host_workspace=str(root),
        container_workspace=EXECUTOR_WORKSPACE_ROOT,
    )


def _resolve_pcap_host_path(relative_path: str) -> Path:
    """Resolve a workspace-relative PCAP path to a host filesystem path."""

    normalized = _validate_workspace_relative_path(relative_path, field_name="pcap path")
    return resolve_workspace_path_safe(normalized, workspace=_effective_workspace_root())


def _sha256_file(path: Path) -> str | None:
    """Return a file SHA-256 digest, or None when the file is unavailable."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _clamp_max_rows(value: Optional[int]) -> int:
    """Clamp direct/runtime row limits to the hard planner maximum."""

    if value is None:
        return 100
    return max(1, min(int(value), TSHARK_MAX_ROWS))


def _normalize_analysis_mode_value(value: Any) -> Any:
    """Normalize legacy analysis mode strings to current intent keys."""

    if isinstance(value, TSharkAnalysisMode):
        return value.value
    normalized = str(value or "").strip().lower()
    return _LEGACY_ANALYSIS_MODE_ALIASES.get(normalized, normalized)


def _legacy_protocol_from_mode(value: Any) -> Optional[str]:
    """Return protocol implied by a legacy mode string, if any."""

    if isinstance(value, TSharkAnalysisMode):
        return None
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _LEGACY_PROTOCOL_ALIASES else None


def _strip_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _validate_search_terms(value: Optional[List[str]]) -> Optional[List[str]]:
    """Normalize bounded text terms used by security-artifact profiles."""

    if value is None:
        return None
    normalized: list[str] = []
    for raw_term in value:
        term = str(raw_term or "").strip()
        if not term:
            continue
        if len(term) > 64:
            raise ValueError("terms entries must be 64 characters or shorter")
        normalized.append(term)
    return normalized or None


def _validate_scope_host(value: str) -> str:
    """Validate planner host filters that compile into TShark display filters."""

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("host must not be empty")
    try:
        return str(ip_address(normalized))
    except ValueError as exc:
        raise ValueError("host must be an IPv4 or IPv6 address; use display_filter for names") from exc


def _validate_scope_protocol(value: str) -> str:
    """Validate protocol filters before embedding them in display filters."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("protocol must not be empty")
    if not normalized.replace("_", "").isalnum():
        raise ValueError("protocol must contain only letters, digits, or '_'")
    return normalized


def _combine_display_filters(*filters: Optional[str]) -> Optional[str]:
    """Combine non-empty display filters with explicit grouping."""

    clauses = [str(item).strip() for item in filters if str(item or "").strip()]
    if not clauses:
        return None
    return " && ".join(f"({clause})" for clause in clauses)


def _display_filter_string(value: str) -> str:
    """Return a quoted display-filter string literal."""

    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _terms_display_filter(terms: Optional[List[str]]) -> Optional[str]:
    """Build a bounded `frame contains` display filter from search terms."""

    normalized = _validate_search_terms(terms)
    if not normalized:
        return None
    return " || ".join(f"frame contains {_display_filter_string(term)}" for term in normalized)


def _pivot_display_filter(args: "TSharkArgs") -> Optional[str]:
    """Build display filters from concrete evidence pivots."""

    clauses: list[str] = []
    if args.stream_id is not None:
        clauses.append(f"tcp.stream == {int(args.stream_id)}")
    if args.frame_number is not None:
        clauses.append(f"frame.number == {int(args.frame_number)}")
    if args.frame_start is not None and args.frame_end is not None:
        clauses.append(
            f"frame.number >= {int(args.frame_start)} && frame.number <= {int(args.frame_end)}"
        )
    elif args.frame_start is not None:
        clauses.append(f"frame.number >= {int(args.frame_start)}")
    elif args.frame_end is not None:
        clauses.append(f"frame.number <= {int(args.frame_end)}")
    return _combine_display_filters(*clauses)


def _compile_scope_display_filter(compiled: Dict[str, Any]) -> None:
    """Translate planner host/port/protocol fields into an actual display filter."""

    clauses: list[str] = []
    host = compiled.get("host")
    port = compiled.get("port")
    protocol = compiled.get("protocol")

    if host:
        host_text = _validate_scope_host(str(host))
        family = "ipv6.addr" if ":" in host_text else "ip.addr"
        clauses.append(f"{family} == {host_text}")
    if port:
        clauses.append(f"(tcp.port == {int(port)} || udp.port == {int(port)})")
    if protocol:
        clauses.append(_validate_scope_protocol(str(protocol)))

    if not clauses:
        return

    scope_filter = " && ".join(f"({clause})" for clause in clauses)
    display_filter = str(compiled.get("display_filter") or "").strip()
    compiled["display_filter"] = (
        f"({display_filter}) && ({scope_filter})" if display_filter else scope_filter
    )


def _dedupe_fields(fields: List[str]) -> List[str]:
    """Return field names in first-seen order without duplicates."""

    deduped: list[str] = []
    for field in fields:
        if field not in deduped:
            deduped.append(field)
    return deduped


def _scope_display_filter(args: "TSharkArgs", *, include_protocol: bool = True) -> Optional[str]:
    """Build host/port/protocol display-filter clauses from bounded args."""

    clauses: list[str] = []
    if args.host:
        host_text = _validate_scope_host(str(args.host))
        family = "ipv6.addr" if ":" in host_text else "ip.addr"
        clauses.append(f"{family} == {host_text}")
    if args.port:
        clauses.append(f"(tcp.port == {int(args.port)} || udp.port == {int(args.port)})")
    if include_protocol and args.protocol:
        clauses.append(_validate_scope_protocol(str(args.protocol)))
    return _combine_display_filters(*clauses)


def _protocol_profile_fields(protocol: Optional[str]) -> List[str]:
    """Return deterministic field bundle for a protocol investigation."""

    normalized = _validate_scope_protocol(protocol or "")
    if normalized == "http":
        return list(_HTTP_FIELDS)
    if normalized == "dns":
        return list(_DNS_FIELDS)
    if normalized in {"tls", "ssl"}:
        return list(_TLS_FIELDS)
    if normalized == "ftp":
        return list(_FTP_FIELDS)
    if normalized in {"smtp", "pop", "imap"}:
        return list(_MAIL_FIELDS)
    return list(_COMMON_FIELDS)


def _resolve_tshark_profile(args: "TSharkArgs") -> _TSharkProfile:
    """Resolve one bounded command profile from validated TShark arguments."""

    max_rows = _clamp_max_rows(args.max_rows)
    mode = args.analysis_mode
    if mode == TSharkAnalysisMode.ANOMALY_DETECTION:
        anomaly_filter = (
            "tcp.analysis.retransmission || tcp.analysis.fast_retransmission || "
            "tcp.analysis.lost_segment || tcp.analysis.duplicate_ack || "
            "icmp || dns.flags.rcode != 0 || http.response.code >= 400 || tls.alert_message"
        )
        return _TSharkProfile(
            output_format=TSharkOutputFormat.FIELDS,
            fields=_dedupe_fields(_ANOMALY_FIELDS),
            display_filter=_combine_display_filters(
                args.display_filter,
                _scope_display_filter(args),
                anomaly_filter,
            ),
            max_rows=max_rows,
        )
    if mode == TSharkAnalysisMode.INVESTIGATE_PROTOCOL:
        protocol = _validate_scope_protocol(args.protocol or "")
        return _TSharkProfile(
            output_format=TSharkOutputFormat.FIELDS,
            fields=_dedupe_fields(_protocol_profile_fields(protocol)),
            display_filter=_combine_display_filters(
                args.display_filter,
                _scope_display_filter(args, include_protocol=False),
                protocol,
            ),
            max_rows=max_rows,
        )
    if mode == TSharkAnalysisMode.EXTRACT_EVIDENCE:
        fields = normalize_tshark_field_extract_fields(args.fields) if args.fields else list(_DEFAULT_EVIDENCE_FIELDS)
        return _TSharkProfile(
            output_format=TSharkOutputFormat.FIELDS,
            fields=_dedupe_fields(fields),
            display_filter=_combine_display_filters(
                args.display_filter,
                _scope_display_filter(args),
                _pivot_display_filter(args),
            ),
            max_rows=max_rows,
        )
    if mode == TSharkAnalysisMode.FIND_SECURITY_RELEVANT_ARTIFACTS:
        security_filter = (
            "http.authorization || http.cookie || http.set_cookie || "
            'ftp.request.command == "USER" || ftp.request.command == "PASS" || '
            "smtp.req.command || pop.request.command || imap.request.command"
        )
        term_filter = _terms_display_filter(args.terms)
        return _TSharkProfile(
            output_format=TSharkOutputFormat.FIELDS,
            fields=_dedupe_fields(_SECURITY_FIELDS),
            display_filter=_combine_display_filters(
                args.display_filter,
                _scope_display_filter(args),
                f"({security_filter}) || ({term_filter})" if term_filter else security_filter,
            ),
            max_rows=max_rows,
        )
    return _TSharkProfile(
        output_format=TSharkOutputFormat.FIELDS,
        fields=_dedupe_fields(_SURVEY_FIELDS),
        display_filter=_combine_display_filters(
            args.display_filter,
            _scope_display_filter(args),
        ),
        max_rows=max_rows,
    )


class TSharkPlannerArgs(BaseModel):
    """Planner-facing TShark arguments with runtime controls removed."""

    model_config = ConfigDict(extra="forbid")

    analysis_mode: TSharkAnalysisMode = Field(
        ...,
        description=(
            "Bounded analysis intent to run: survey, anomaly_detection, "
            "investigate_protocol, extract_evidence, or find_security_relevant_artifacts."
        ),
    )
    input_file: Optional[str] = Field(
        None,
        max_length=4_096,
        description="Workspace-relative PCAP file to analyze.",
    )
    interface: Optional[str] = Field(
        None,
        max_length=64,
        description="Network interface to capture from.",
    )
    display_filter: Optional[str] = Field(
        None,
        max_length=2_048,
        description="Display filter to apply (tshark -Y).",
    )
    capture_filter: Optional[str] = Field(
        None,
        max_length=2_048,
        description="Capture filter to apply (tshark -f).",
    )
    host: Optional[str] = Field(
        None,
        max_length=255,
        description="Host filter for semantic analysis.",
    )
    port: Optional[int] = Field(
        None,
        ge=1,
        le=65_535,
        description="TCP/UDP port filter for semantic analysis.",
    )
    protocol: Optional[str] = Field(
        None,
        max_length=32,
        description="Protocol filter for semantic analysis.",
    )
    stream_id: Optional[int] = Field(
        None,
        ge=0,
        le=1_000_000,
        description="TCP stream pivot for concrete evidence extraction.",
    )
    frame_number: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000_000,
        description="Single frame pivot for concrete evidence extraction.",
    )
    frame_start: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000_000,
        description="Start frame for bounded evidence extraction.",
    )
    frame_end: Optional[int] = Field(
        None,
        ge=1,
        le=1_000_000_000,
        description="End frame for bounded evidence extraction.",
    )
    terms: Optional[List[str]] = Field(
        None,
        min_length=1,
        max_length=10,
        description="Bounded search terms for security-relevant artifact discovery.",
    )
    fields: Optional[List[str]] = Field(
        None,
        min_length=1,
        max_length=50,
        description="Allowlisted TShark fields for field_extract mode.",
    )
    include_payload_indicators: StrictBool = Field(
        False,
        description="Allow bounded payload indicator metadata only.",
    )
    max_rows: int = Field(
        100,
        ge=1,
        le=TSHARK_MAX_ROWS,
        description="Maximum rows to parse or emit.",
    )
    sensitive_proof_mode: TSharkSensitiveProofMode = Field(
        TSharkSensitiveProofMode.PROOF_EXCERPT,
        description="Sensitive proof mode for runtime packet evidence.",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_protocol = _legacy_protocol_from_mode(payload.get("analysis_mode"))
        if legacy_protocol and not payload.get("protocol"):
            payload["protocol"] = legacy_protocol
        if "analysis_mode" in payload:
            payload["analysis_mode"] = _normalize_analysis_mode_value(payload.get("analysis_mode"))
        return payload

    @field_validator("analysis_mode", mode="before")
    @classmethod
    def _normalize_analysis_mode(cls, value: Any) -> Any:
        return _normalize_analysis_mode_value(value)

    @field_validator(
        "input_file",
        "interface",
        "display_filter",
        "capture_filter",
        "host",
        "protocol",
        mode="before",
    )
    @classmethod
    def _strip_optional_strings(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("input_file")
    @classmethod
    def _validate_input_file(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_workspace_relative_path(value, field_name="input_file")

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        return normalize_tshark_field_extract_fields(value)

    @field_validator("terms")
    @classmethod
    def _validate_terms(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_search_terms(value)

    @model_validator(mode="after")
    def _validate_intent_contract(self) -> "TSharkPlannerArgs":
        if self.input_file and self.capture_filter:
            raise ValueError("capture_filter is only valid for live capture; use display_filter for input_file")
        if self.frame_start and self.frame_end and self.frame_start > self.frame_end:
            raise ValueError("frame_start must be less than or equal to frame_end")
        if self.analysis_mode == TSharkAnalysisMode.INVESTIGATE_PROTOCOL:
            if not self.protocol:
                raise ValueError("protocol is required for investigate_protocol analysis")
        if self.analysis_mode == TSharkAnalysisMode.EXTRACT_EVIDENCE:
            has_pivot = any(
                value is not None
                for value in (
                    self.stream_id,
                    self.frame_number,
                    self.frame_start,
                    self.frame_end,
                    self.host,
                    self.port,
                    self.display_filter,
                )
            )
            if not has_pivot:
                raise ValueError("extract_evidence requires a concrete pivot")
        elif self.fields:
            raise ValueError("fields are only valid for extract_evidence analysis")
        return self


class TSharkArgs(BaseToolArgs):
    """Arguments for the TShark tool."""

    model_config = ConfigDict(extra="forbid")

    analysis_mode: TSharkAnalysisMode = Field(
        TSharkAnalysisMode.SURVEY,
        description="Bounded semantic analysis intent.",
    )
    interface: Optional[str] = Field(
        None,
        description="Network interface to capture from.",
    )
    input_file: Optional[str] = Field(
        None,
        description="Input pcap file to analyze instead of live capture.",
    )
    display_filter: Optional[str] = Field(
        None,
        description="Display filter to apply (tshark -Y).",
    )
    capture_filter: Optional[str] = Field(
        None,
        description="Capture filter to apply (tshark -f).",
    )
    packet_count: Optional[int] = Field(
        None,
        description="Number of packets to capture.",
        ge=1,
        le=1_000_000,
    )
    duration_seconds: Optional[int] = Field(
        None,
        description="Capture duration in seconds.",
        ge=1,
        le=3_600,
    )
    fields: Optional[List[str]] = Field(
        None,
        description="Specific fields to extract (implies -T fields).",
    )
    host: Optional[str] = Field(
        None,
        description="Host filter retained for semantic parsing.",
    )
    port: Optional[int] = Field(
        None,
        description="TCP/UDP port filter retained for semantic parsing.",
        ge=1,
        le=65_535,
    )
    protocol: Optional[str] = Field(
        None,
        description="Protocol filter retained for semantic parsing.",
    )
    stream_id: Optional[int] = Field(
        None,
        description="TCP stream pivot for concrete evidence extraction.",
        ge=0,
        le=1_000_000,
    )
    frame_number: Optional[int] = Field(
        None,
        description="Single frame pivot for concrete evidence extraction.",
        ge=1,
        le=1_000_000_000,
    )
    frame_start: Optional[int] = Field(
        None,
        description="Start frame for bounded evidence extraction.",
        ge=1,
        le=1_000_000_000,
    )
    frame_end: Optional[int] = Field(
        None,
        description="End frame for bounded evidence extraction.",
        ge=1,
        le=1_000_000_000,
    )
    terms: Optional[List[str]] = Field(
        None,
        description="Bounded search terms for security-relevant artifact discovery.",
        min_length=1,
        max_length=10,
    )
    include_payload_indicators: bool = Field(
        False,
        description="Allow bounded payload indicator metadata only.",
    )
    max_rows: Optional[int] = Field(
        100,
        description="Direct/runtime row limit; clamped to the planner hard maximum.",
        ge=1,
    )
    sensitive_proof_mode: TSharkSensitiveProofMode = Field(
        TSharkSensitiveProofMode.PROOF_EXCERPT,
        description="Sensitive proof mode for runtime packet evidence.",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_protocol = _legacy_protocol_from_mode(payload.get("analysis_mode"))
        if legacy_protocol and not payload.get("protocol"):
            payload["protocol"] = legacy_protocol
        if "analysis_mode" in payload:
            payload["analysis_mode"] = _normalize_analysis_mode_value(payload.get("analysis_mode"))
        return payload

    @field_validator("analysis_mode", mode="before")
    @classmethod
    def _normalize_analysis_mode(cls, value: Any) -> Any:
        return _normalize_analysis_mode_value(value)

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        return normalize_tshark_field_extract_fields(value)

    @field_validator("terms")
    @classmethod
    def _validate_terms(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_search_terms(value)

    @field_validator("input_file")
    @classmethod
    def _validate_input_file(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_workspace_relative_path(value, field_name="input_file")

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_scope_host(value)

    @field_validator("protocol")
    @classmethod
    def _validate_protocol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _validate_scope_protocol(value)

    @model_validator(mode="after")
    def _validate_intent_contract(self) -> "TSharkArgs":
        if self.input_file and self.capture_filter:
            raise ValueError("capture_filter is only valid for live capture; use display_filter for input_file")
        if self.frame_start and self.frame_end and self.frame_start > self.frame_end:
            raise ValueError("frame_start must be less than or equal to frame_end")
        if self.analysis_mode == TSharkAnalysisMode.INVESTIGATE_PROTOCOL and not self.protocol:
            raise ValueError("protocol is required for investigate_protocol analysis")
        if self.analysis_mode == TSharkAnalysisMode.EXTRACT_EVIDENCE:
            has_pivot = any(
                value is not None
                for value in (
                    self.stream_id,
                    self.frame_number,
                    self.frame_start,
                    self.frame_end,
                    self.host,
                    self.port,
                    self.display_filter,
                )
            )
            if not has_pivot:
                raise ValueError("extract_evidence requires a concrete pivot")
        elif self.fields:
            raise ValueError("fields are only valid for extract_evidence analysis")
        return self


class TSharkTool(BaseTool):
    """Run TShark network protocol analysis and parse the output."""

    args_model = TSharkArgs
    planner_args_model = TSharkPlannerArgs
    informational_exit_codes = frozenset({TSHARK_TIMEOUT_EXIT_CODE})
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.STRUCTURED_NATIVE,
        canonical_format=CanonicalCaptureFormat.JSON,
    )

    @classmethod
    def compile_planner_parameters(
        cls,
        planner_args: TSharkPlannerArgs | Dict[str, Any],
        *,
        action_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compile semantic planner args into execution args."""

        if isinstance(planner_args, TSharkPlannerArgs):
            compiled = planner_args.model_dump(
                exclude_defaults=True,
                exclude_none=True,
                mode="json",
            )
        else:
            compiled = TSharkPlannerArgs(**dict(planner_args or {})).model_dump(
                exclude_defaults=True,
                exclude_none=True,
                mode="json",
            )
        compiled["target"] = action_target or TSHARK_SAFE_TARGET_PLACEHOLDER
        return compiled

    def build_command(self, args: TSharkArgs) -> List[str]:
        live_capture = not args.input_file
        profile = _resolve_tshark_profile(args)
        field_names = profile.fields
        cmd: List[str] = (
            ["timeout", f"{TSHARK_HARD_TIMEOUT_SECONDS}s", "tshark"]
            if live_capture
            else ["tshark"]
        )

        if args.input_file:
            cmd.extend(["-r", _resolve_pcap_path_for_execution(args.input_file)])
            packet_count = min(args.packet_count or profile.max_rows, TSHARK_MAX_ROWS)
            cmd.extend(["-c", str(packet_count)])
        else:
            interface = args.interface or DEFAULT_INTERFACE
            packet_count = min(args.packet_count or profile.max_rows, TSHARK_LIVE_PACKET_LIMIT)
            duration_seconds = min(
                args.duration_seconds or TSHARK_HARD_TIMEOUT_SECONDS,
                TSHARK_HARD_TIMEOUT_SECONDS,
            )
            cmd.extend(
                [
                    "-i",
                    interface,
                    "-s",
                    str(TSHARK_DEFAULT_SNAPLEN),
                    "-c",
                    str(packet_count),
                    "-a",
                    f"duration:{duration_seconds}",
                ]
            )

        cmd.extend(["-T", profile.output_format.value])

        if profile.display_filter:
            cmd.extend(["-Y", profile.display_filter])
        if args.capture_filter:
            cmd.extend(["-f", args.capture_filter])
        if args.duration_seconds and not live_capture:
            cmd.extend(["-a", f"duration:{args.duration_seconds}"])
        if field_names:
            for field in field_names:
                cmd.extend(["-e", field])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TSharkArgs,
    ) -> Dict[str, Any]:
        max_rows = _clamp_max_rows(args.max_rows)
        profile = _resolve_tshark_profile(args)
        metadata = parse_tshark_output(
            stdout,
            stderr,
            analysis_mode=args.analysis_mode.value,
            input_file=args.input_file,
            artifact_sha256=self._pcap_artifact_sha256(args),
            max_rows=max_rows,
            fields=profile.fields,
            sensitive_proof_mode=args.sensitive_proof_mode.value,
        )
        metadata["exit_code"] = exit_code
        metadata["output_format"] = (
            profile.output_format.value
        )
        metadata["max_rows"] = max_rows
        if exit_code == TSHARK_TIMEOUT_EXIT_CODE:
            metadata["bounded_timeout"] = True
            metadata["execution_outcome"] = (
                "informational" if self._has_usable_timeout_output(stdout, args) else "failed"
            )
        self._attach_pcap_compaction(metadata)
        return metadata

    @staticmethod
    def _attach_pcap_compaction(metadata: Dict[str, Any]) -> None:
        """Attach reusable deterministic PCAP compact fields to parsed metadata."""

        compact_payload = build_pcap_compaction(
            metadata,
            source_tool="sniffing_spoofing.network_sniffers.tshark",
        )
        metadata.update(compact_payload)

    @staticmethod
    def _has_usable_timeout_output(stdout: str, args: TSharkArgs) -> bool:
        """Return whether a bounded live-capture timeout still produced usable evidence."""

        return bool(str(stdout or "").strip())

    @staticmethod
    def _pcap_artifact_sha256(args: TSharkArgs) -> str | None:
        """Return the source PCAP digest for offline analysis metadata."""

        if not args.input_file:
            return None
        return _sha256_file(_resolve_pcap_host_path(args.input_file))

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TSharkArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate safe TShark semantic observation emission to parser helpers."""
        _ = stdout, stderr, exit_code
        return build_tshark_semantic_observations(metadata, args)

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: TSharkArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate bounded TShark semantic evidence emission to parser helpers."""
        _ = stdout, stderr, exit_code
        return build_tshark_semantic_evidence(metadata, args)

    def render_result_output(
        self,
        args: TSharkArgs,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        """Render command-transport output for the model-visible runtime path."""

        _ = args
        return stdout, stderr

    def render_process_output(
        self,
        args: TSharkArgs,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        """Return raw process streams for runtime artifact persistence."""

        _ = args
        return stdout, stderr

    def postprocess_execution(
        self,
        *,
        args: TSharkArgs,
        stdout: str,
        stderr: str,
        exit_code: int,
        success: bool,
        metadata: Dict[str, Any],
        artifacts: List[str],
        runtime_context: Optional[Any] = None,
    ) -> ToolPostprocessResult:
        """Replace model-visible packet rows with deterministic compact JSON."""

        _ = args, runtime_context
        post_metadata = dict(metadata or {})
        return ToolPostprocessResult(
            success=success,
            exit_code=exit_code,
            stdout=self._compact_transport_stdout(post_metadata),
            stderr="",
            metadata=post_metadata,
            artifacts=list(artifacts or []),
        )

    def create_artifacts(
        self,
        stdout: str,
        args: TSharkArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        _ = stdout, args, timestamp
        return []

    @staticmethod
    @staticmethod
    def _compact_transport_stdout(metadata: Optional[Dict[str, Any]]) -> str:
        """Render deterministic PCAP compact JSON for model-visible output."""

        metadata_map = metadata if isinstance(metadata, dict) else {}
        compact = metadata_map.get("pcap_compact")
        return render_pcap_compact_json(compact if isinstance(compact, dict) else None)

    def run(self, args: TSharkArgs) -> ToolResult:
        start = time.time()
        if args.input_file:
            timeout = args.timeout or DEFAULT_TIMEOUT
        else:
            timeout = max(args.timeout or DEFAULT_TIMEOUT, TSHARK_HARD_TIMEOUT_SECONDS + 5)

        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={"timeout": timeout},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="tshark command not found. Ensure tshark is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        rendered_stdout = self._compact_transport_stdout(metadata)
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))

        return ToolResult(
            success=self.is_success_exit_code(
                proc.returncode,
                args,
                stdout=proc.stdout,
                stderr=proc.stderr,
                parsed_metadata=metadata,
            ),
            exit_code=proc.returncode,
            stdout=rendered_stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="sniffing_spoofing.network_sniffers.tshark",
        display_name="TShark",
        category=ToolCategory.SNIFFING_SPOOFING,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="packet_analysis",
                description="Analyze passive PCAP/offline artifacts with tshark dissectors; returns structured metadata, semantic_observations, semantic_evidence, and bounded proof; use tcpdump to create raw captures",
                output_indicators=[
                    "structured_metadata",
                    "semantic_observations",
                    "semantic_evidence",
                    "packet_proof",
                ],
            ),
            ToolCapability(
                name="finite_live_capture",
                description="Run finite live capture only through hidden packet and timeout caps; prefer offline artifact analysis when a capture file already exists",
                output_indicators=["bounded_timeout", "packet_count", "structured_metadata"],
            ),
            ToolCapability(
                name="credential_key_exposure_proof",
                description="Report credential/key exposure as metadata-only proof, raw proof excerpts, or keyed fingerprints for runtime analysis; durable app-owned projections mask reusable secrets",
                output_indicators=["secret_exposure", "proof_excerpt", "fingerprint"],
            ),
            ToolCapability(
                name="artifact_followup",
                description="Use visible workspace filesystem tools such as artifact.read or artifact.search on upstream task-local PCAP paths when deeper packet inspection is required",
                output_indicators=["artifact_path", "artifact_sha256", "evidence_refs"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=7,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=5,
    )
)
