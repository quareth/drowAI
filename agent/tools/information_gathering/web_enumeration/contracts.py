"""Pydantic contracts for HTTP web enumeration tools.

This module owns schema-only request and download contracts for HTTP tools. It
must not build curl commands, run subprocesses, probe curl capabilities,
materialize workspace files, redact output, or parse runtime responses.
"""

from __future__ import annotations

import re
from typing import Dict, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.tools.schemas import CONTAINER_TRANSPORT_DESCRIPTION, ContainerTransport

_HTTP_SCHEMES = {"http", "https"}
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _validate_http_target(value: str) -> str:
    """Validate an absolute HTTP(S) URL target for HTTP tool schemas."""
    target = (value or "").strip()
    if not target:
        raise ValueError("target URL is required")
    parsed = urlparse(target)
    if parsed.scheme.lower() not in _HTTP_SCHEMES:
        raise ValueError("target scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("target must be an absolute URL with hostname")
    return target


class HttpRequestArgs(BaseModel):
    """Arguments for the HTTP request reconnaissance tool."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description="HTTP or HTTPS URL to request.",
    )
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] = Field(
        "GET",
        description="HTTP method to use for the request.",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="Request headers as key-value pairs.",
    )
    body: Optional[str] = Field(
        None,
        max_length=1_048_576,
        description="Optional raw request body content (max 1 MiB).",
    )
    body_file_path: Optional[str] = Field(
        None,
        description="Workspace-relative file path for binary request body (--data-binary @file).",
    )
    body_base64: Optional[str] = Field(
        None,
        max_length=8_388_608,
        description="Base64-encoded request body payload for binary-safe request sending.",
    )
    http_version: Literal["auto", "1.1", "2", "3"] = Field(
        "auto",
        description="Requested HTTP protocol version preference (auto, 1.1, 2, 3).",
    )
    form_fields: Dict[str, str] = Field(
        default_factory=dict,
        description="Multipart form fields mapped as {field_name: value}.",
    )
    form_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Multipart file uploads mapped as {field_name: workspace_relative_path}.",
    )
    cookie: Optional[str] = Field(
        None,
        max_length=16_384,
        description="Inline cookie string sent via curl --cookie (for example: 'a=1; b=2').",
    )
    cookie_file: Optional[str] = Field(
        None,
        description="Workspace-relative cookie file path used as curl --cookie input.",
    )
    auth_mode: Literal["none", "basic", "bearer"] = Field(
        "none",
        description="Authentication mode: none, basic (username/password), or bearer (bearer_token).",
    )
    username: Optional[str] = Field(
        None,
        max_length=512,
        description="Username used when auth_mode=basic.",
    )
    password: Optional[str] = Field(
        None,
        max_length=4_096,
        description="Password used when auth_mode=basic.",
    )
    bearer_token: Optional[str] = Field(
        None,
        max_length=16_384,
        description="Bearer token used when auth_mode=bearer.",
    )
    client_cert_path: Optional[str] = Field(
        None,
        description="Workspace-relative client certificate file path for mTLS.",
    )
    client_key_path: Optional[str] = Field(
        None,
        description="Workspace-relative private key file path for mTLS.",
    )
    client_key_passphrase: Optional[str] = Field(
        None,
        max_length=4_096,
        description="Optional passphrase for encrypted client private key.",
    )
    ca_cert_path: Optional[str] = Field(
        None,
        description="Workspace-relative CA certificate bundle path for custom trust.",
    )
    resolve: List[str] = Field(
        default_factory=list,
        description="curl --resolve entries in form host:port:address.",
    )
    connect_to: List[str] = Field(
        default_factory=list,
        description="curl --connect-to entries in form host1:port1:host2:port2.",
    )
    interface: Optional[str] = Field(
        None,
        description="Optional outgoing interface or host for curl --interface.",
    )
    local_port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Optional local source port for curl --local-port.",
    )
    ipv4_only: bool = Field(
        False,
        description="Force IPv4 resolution/connection when true.",
    )
    ipv6_only: bool = Field(
        False,
        description="Force IPv6 resolution/connection when true.",
    )
    retries: Optional[int] = Field(
        None,
        ge=0,
        le=20,
        description="Optional curl retry count (--retry). Omit to preserve no-retry default behavior.",
    )
    retry_delay: Optional[int] = Field(
        None,
        ge=0,
        le=120,
        description="Optional delay between retries in seconds (--retry-delay).",
    )
    retry_max_time: Optional[int] = Field(
        None,
        ge=1,
        le=600,
        description="Optional maximum cumulative retry time in seconds (--retry-max-time).",
    )
    retry_connrefused: bool = Field(
        False,
        description="Treat connection refused as retryable when retries are enabled.",
    )
    limit_rate: Optional[str] = Field(
        None,
        max_length=32,
        description="Optional transfer rate limit string for curl --limit-rate (for example: 200K, 2M).",
    )
    content_type: Optional[str] = Field(
        None,
        description="Optional Content-Type shortcut header value.",
    )
    timeout: int = Field(
        30,
        ge=1,
        le=300,
        description="Request timeout in seconds.",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects when true.",
    )
    max_redirects: int = Field(
        10,
        ge=0,
        le=20,
        description="Maximum redirects to follow when enabled.",
    )
    insecure_tls: bool = Field(
        False,
        description="Disable TLS certificate validation when true (less secure).",
    )
    proxy: Optional[str] = Field(
        None,
        description="Optional proxy URL used by curl.",
    )
    user_agent: Optional[str] = Field(
        None,
        description="Optional User-Agent override.",
    )
    capture_body: bool = Field(
        True,
        description="Capture response body in tool output.",
    )
    max_body_bytes: int = Field(
        524_288,
        ge=1_024,
        le=5_242_880,
        description="Maximum response body bytes to retain in memory.",
    )
    redact_output: bool = Field(
        True,
        description="Apply secret redaction before returning output.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return _validate_http_target(value)

    @field_validator("resolve")
    @classmethod
    def validate_resolve_entries(cls, value: List[str]) -> List[str]:
        validated: List[str] = []
        for entry in value:
            parts = (entry or "").split(":")
            if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
                raise ValueError("resolve entries must be host:port:address")
            try:
                port = int(parts[1])
            except Exception as exc:  # pragma: no cover - defensive
                raise ValueError("resolve entry port must be numeric") from exc
            if port < 1 or port > 65535:
                raise ValueError("resolve entry port must be between 1 and 65535")
            validated.append(entry)
        return validated

    @field_validator("connect_to")
    @classmethod
    def validate_connect_to_entries(cls, value: List[str]) -> List[str]:
        validated: List[str] = []
        for entry in value:
            parts = (entry or "").split(":")
            if len(parts) != 4 or any(not part for part in parts):
                raise ValueError("connect_to entries must be host1:port1:host2:port2")
            try:
                port1 = int(parts[1])
                port2 = int(parts[3])
            except Exception as exc:  # pragma: no cover - defensive
                raise ValueError("connect_to entry ports must be numeric") from exc
            if not (1 <= port1 <= 65535 and 1 <= port2 <= 65535):
                raise ValueError("connect_to entry ports must be between 1 and 65535")
            validated.append(entry)
        return validated

    @model_validator(mode="after")
    def validate_session_cookie_inputs(self) -> "HttpRequestArgs":
        if self.cookie and self.cookie_file:
            raise ValueError("cookie and cookie_file are mutually exclusive")
        body_sources = int(self.body is not None) + int(self.body_file_path is not None) + int(self.body_base64 is not None)
        if body_sources > 1:
            raise ValueError("body, body_file_path, and body_base64 are mutually exclusive")
        if body_sources > 0 and (self.form_fields or self.form_files):
            raise ValueError("body cannot be combined with form_fields/form_files")
        if self.body_file_path is not None and not str(self.body_file_path).strip():
            raise ValueError("body_file_path cannot be empty")
        if self.body_base64 is not None and not str(self.body_base64).strip():
            raise ValueError("body_base64 cannot be empty")
        for key in self.form_fields.keys():
            if not str(key).strip():
                raise ValueError("form_fields keys must be non-empty")
        for key, value in self.form_files.items():
            if not str(key).strip():
                raise ValueError("form_files keys must be non-empty")
            if not str(value).strip():
                raise ValueError("form_files values must be non-empty paths")
        has_auth_header = any(str(k).strip().lower() == "authorization" for k in self.headers.keys())
        has_basic_creds = bool(self.username or self.password)
        has_bearer_token = bool(self.bearer_token)

        if self.auth_mode == "none":
            if has_basic_creds or has_bearer_token:
                raise ValueError("auth_mode must be set when username/password/bearer_token is provided")
        elif self.auth_mode == "basic":
            if has_auth_header:
                raise ValueError("auth_mode=basic cannot be combined with Authorization header")
            if not self.username or not self.password:
                raise ValueError("auth_mode=basic requires both username and password")
            if has_bearer_token:
                raise ValueError("auth_mode=basic cannot be combined with bearer_token")
        elif self.auth_mode == "bearer":
            if has_auth_header:
                raise ValueError("auth_mode=bearer cannot be combined with Authorization header")
            if not self.bearer_token:
                raise ValueError("auth_mode=bearer requires bearer_token")
            if has_basic_creds:
                raise ValueError("auth_mode=bearer cannot be combined with username/password")
        if self.client_key_path and not self.client_cert_path:
            raise ValueError("client_key_path requires client_cert_path")
        if self.client_key_passphrase and not self.client_key_path:
            raise ValueError("client_key_passphrase requires client_key_path")
        if self.ipv4_only and self.ipv6_only:
            raise ValueError("ipv4_only and ipv6_only are mutually exclusive")
        if self.limit_rate is not None and not str(self.limit_rate).strip():
            raise ValueError("limit_rate cannot be empty")
        has_retry_option = (
            self.retry_delay is not None
            or self.retry_max_time is not None
            or self.retry_connrefused
        )
        if has_retry_option and self.retries is None:
            raise ValueError("retry_delay/retry_max_time/retry_connrefused require retries")
        return self


class HttpDownloadArgs(BaseModel):
    """Arguments for the HTTP download tool."""

    target: str = Field(
        ...,
        description="HTTP or HTTPS URL to download.",
    )
    output_path: str = Field(
        ...,
        description="Workspace-relative destination file path.",
    )
    cookie: Optional[str] = Field(
        None,
        max_length=16_384,
        description="Inline cookie string sent via curl --cookie (for example: 'a=1; b=2').",
    )
    cookie_file: Optional[str] = Field(
        None,
        description="Workspace-relative cookie file path used as curl --cookie input.",
    )
    cookie_jar: Optional[str] = Field(
        None,
        description="Workspace-relative cookie jar output path used as curl --cookie-jar target.",
    )
    persist_cookies: bool = Field(
        False,
        description="Persist received cookies to cookie_jar (or fallback jar path) when true.",
    )
    auth_mode: Literal["none", "basic", "bearer"] = Field(
        "none",
        description="Authentication mode: none, basic (username/password), or bearer (bearer_token).",
    )
    username: Optional[str] = Field(
        None,
        max_length=512,
        description="Username used when auth_mode=basic.",
    )
    password: Optional[str] = Field(
        None,
        max_length=4_096,
        description="Password used when auth_mode=basic.",
    )
    bearer_token: Optional[str] = Field(
        None,
        max_length=16_384,
        description="Bearer token used when auth_mode=bearer.",
    )
    client_cert_path: Optional[str] = Field(
        None,
        description="Workspace-relative client certificate file path for mTLS.",
    )
    client_key_path: Optional[str] = Field(
        None,
        description="Workspace-relative private key file path for mTLS.",
    )
    client_key_passphrase: Optional[str] = Field(
        None,
        max_length=4_096,
        description="Optional passphrase for encrypted client private key.",
    )
    ca_cert_path: Optional[str] = Field(
        None,
        description="Workspace-relative CA certificate bundle path for custom trust.",
    )
    resolve: List[str] = Field(
        default_factory=list,
        description="curl --resolve entries in form host:port:address.",
    )
    connect_to: List[str] = Field(
        default_factory=list,
        description="curl --connect-to entries in form host1:port1:host2:port2.",
    )
    interface: Optional[str] = Field(
        None,
        description="Optional outgoing interface or host for curl --interface.",
    )
    local_port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="Optional local source port for curl --local-port.",
    )
    ipv4_only: bool = Field(
        False,
        description="Force IPv4 resolution/connection when true.",
    )
    ipv6_only: bool = Field(
        False,
        description="Force IPv6 resolution/connection when true.",
    )
    retries: Optional[int] = Field(
        None,
        ge=0,
        le=20,
        description="Optional curl retry count (--retry). Omit to preserve no-retry default behavior.",
    )
    retry_delay: Optional[int] = Field(
        None,
        ge=0,
        le=120,
        description="Optional delay between retries in seconds (--retry-delay).",
    )
    retry_max_time: Optional[int] = Field(
        None,
        ge=1,
        le=600,
        description="Optional maximum cumulative retry time in seconds (--retry-max-time).",
    )
    retry_connrefused: bool = Field(
        False,
        description="Treat connection refused as retryable when retries are enabled.",
    )
    limit_rate: Optional[str] = Field(
        None,
        max_length=32,
        description="Optional transfer rate limit string for curl --limit-rate (for example: 200K, 2M).",
    )
    connect_timeout: Optional[int] = Field(
        None,
        ge=1,
        description="Optional connection timeout in seconds (--connect-timeout).",
    )
    speed_limit: Optional[int] = Field(
        None,
        ge=1,
        description="Optional minimum transfer speed in bytes per second (--speed-limit); requires speed_time.",
    )
    speed_time: Optional[int] = Field(
        None,
        ge=1,
        description="Optional low-speed duration in seconds (--speed-time); requires speed_limit.",
    )
    dump_headers_artifact: bool = Field(
        False,
        description="Persist response headers to a deterministic artifact file.",
    )
    trace_mode: Literal["none", "trace", "trace_ascii"] = Field(
        "none",
        description="Optional curl trace mode for debug artifacts.",
    )
    trace_artifact: Optional[str] = Field(
        None,
        description="Workspace-relative trace artifact path; requires trace_mode != none.",
    )
    http_version: Literal["auto", "1.1", "2", "3"] = Field(
        "auto",
        description="Requested HTTP protocol version preference (auto, 1.1, 2, 3).",
    )
    overwrite: bool = Field(
        False,
        description="Allow replacing an existing destination file.",
    )
    create_parents: bool = Field(
        True,
        description="Create parent directories for output path when missing.",
    )
    timeout: int = Field(
        60,
        ge=1,
        description="Download timeout in seconds.",
    )
    follow_redirects: bool = Field(
        True,
        description="Follow HTTP redirects when true.",
    )
    max_redirects: int = Field(
        10,
        ge=0,
        le=20,
        description="Maximum redirects to follow when enabled.",
    )
    insecure_tls: bool = Field(
        False,
        description="Disable TLS certificate validation when true (less secure).",
    )
    proxy: Optional[str] = Field(
        None,
        description="Optional proxy URL used by curl.",
    )
    user_agent: Optional[str] = Field(
        None,
        description="Optional User-Agent override.",
    )
    resume: bool = Field(
        False,
        description="Resume partial download when supported.",
    )
    expected_sha256: Optional[str] = Field(
        None,
        description="Optional expected SHA-256 digest for integrity verification.",
    )
    min_bytes: Optional[int] = Field(
        None,
        ge=0,
        description="Optional minimum accepted file size in bytes.",
    )
    max_bytes: Optional[int] = Field(
        None,
        ge=1,
        le=1_073_741_824,
        description="Optional maximum accepted file size in bytes.",
    )
    transport: Optional[ContainerTransport] = Field(
        None,
        description=CONTAINER_TRANSPORT_DESCRIPTION,
    )

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return _validate_http_target(value)

    @field_validator("expected_sha256")
    @classmethod
    def validate_expected_sha256(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("expected_sha256 must be a 64-character hex digest")
        return normalized

    @field_validator("resolve")
    @classmethod
    def validate_resolve_entries(cls, value: List[str]) -> List[str]:
        validated: List[str] = []
        for entry in value:
            parts = (entry or "").split(":")
            if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
                raise ValueError("resolve entries must be host:port:address")
            try:
                port = int(parts[1])
            except Exception as exc:  # pragma: no cover - defensive
                raise ValueError("resolve entry port must be numeric") from exc
            if port < 1 or port > 65535:
                raise ValueError("resolve entry port must be between 1 and 65535")
            validated.append(entry)
        return validated

    @field_validator("connect_to")
    @classmethod
    def validate_connect_to_entries(cls, value: List[str]) -> List[str]:
        validated: List[str] = []
        for entry in value:
            parts = (entry or "").split(":")
            if len(parts) != 4 or any(not part for part in parts):
                raise ValueError("connect_to entries must be host1:port1:host2:port2")
            try:
                port1 = int(parts[1])
                port2 = int(parts[3])
            except Exception as exc:  # pragma: no cover - defensive
                raise ValueError("connect_to entry ports must be numeric") from exc
            if not (1 <= port1 <= 65535 and 1 <= port2 <= 65535):
                raise ValueError("connect_to entry ports must be between 1 and 65535")
            validated.append(entry)
        return validated

    @model_validator(mode="after")
    def validate_session_cookie_inputs(self) -> "HttpDownloadArgs":
        if self.cookie and self.cookie_file:
            raise ValueError("cookie and cookie_file are mutually exclusive")
        has_basic_creds = bool(self.username or self.password)
        has_bearer_token = bool(self.bearer_token)

        if self.auth_mode == "none":
            if has_basic_creds or has_bearer_token:
                raise ValueError("auth_mode must be set when username/password/bearer_token is provided")
        elif self.auth_mode == "basic":
            if not self.username or not self.password:
                raise ValueError("auth_mode=basic requires both username and password")
            if has_bearer_token:
                raise ValueError("auth_mode=basic cannot be combined with bearer_token")
        elif self.auth_mode == "bearer":
            if not self.bearer_token:
                raise ValueError("auth_mode=bearer requires bearer_token")
            if has_basic_creds:
                raise ValueError("auth_mode=bearer cannot be combined with username/password")
        if self.client_key_path and not self.client_cert_path:
            raise ValueError("client_key_path requires client_cert_path")
        if self.client_key_passphrase and not self.client_key_path:
            raise ValueError("client_key_passphrase requires client_key_path")
        if self.ipv4_only and self.ipv6_only:
            raise ValueError("ipv4_only and ipv6_only are mutually exclusive")
        if self.limit_rate is not None and not str(self.limit_rate).strip():
            raise ValueError("limit_rate cannot be empty")
        if (self.speed_limit is None) != (self.speed_time is None):
            raise ValueError("speed_limit and speed_time must be provided together")
        if self.trace_artifact is not None and not str(self.trace_artifact).strip():
            raise ValueError("trace_artifact cannot be empty")
        if self.trace_artifact and self.trace_mode == "none":
            raise ValueError("trace_artifact requires trace_mode")
        has_retry_option = (
            self.retry_delay is not None
            or self.retry_max_time is not None
            or self.retry_connrefused
        )
        if has_retry_option and self.retries is None:
            raise ValueError("retry_delay/retry_max_time/retry_connrefused require retries")
        return self

