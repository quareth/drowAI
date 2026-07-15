"""HTTP deterministic compression helpers.

This module projects structured HTTP request/download metadata into compact
facts. It does not execute curl, read downloaded files, inspect artifacts, or
promote raw request bodies, cookies, bearer tokens, or response payload text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS

from agent.tools.web_applications.web_response_analysis import (
    WebResponseAnalysis,
    WebResponseCookie,
    WebResponseForm,
    analyze_http_download_response,
    analyze_http_request_response,
)

from .common import (
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)
from .contracts import CompressionInput, DeterministicCompressionResult

HTTP_REQUEST_TOOL_ID = "information_gathering.web_enumeration.http_request"
HTTP_DOWNLOAD_TOOL_ID = "information_gathering.web_enumeration.http_download"

_HTTP_TOOL_IDS: tuple[str, ...] = (HTTP_REQUEST_TOOL_ID, HTTP_DOWNLOAD_TOOL_ID)
_HEADER_FACT_LIMIT = 6
_ARTIFACT_REF_LIMIT = 5
_HTTP_LINK_DISPLAY_LIMIT = 12
_HTTP_EXTERNAL_REF_DISPLAY_LIMIT = 8
_HTTP_ASSET_DISPLAY_LIMIT = 10
_HTTP_DOWNLOAD_DISPLAY_LIMIT = 8
_HTTP_FORM_DISPLAY_LIMIT = 5
_HTTP_COOKIE_DISPLAY_LIMIT = 6
_HTTP_TECH_HINT_DISPLAY_LIMIT = 8
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "code",
        "credential",
        "key",
        "password",
        "secret",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
    }
)
_SECURITY_RESPONSE_HEADERS = (
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "cross-origin-embedder-policy",
)
_AUTH_RESPONSE_HEADERS = (
    "www-authenticate",
    "proxy-authenticate",
    "set-cookie",
)
_REDIRECT_RESPONSE_HEADERS = ("location",)
_VALUE_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)


def http_adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
    """Project HTTP request/download metadata into compact deterministic facts."""

    metadata = _mapping_or_empty(input_data.raw_result.get("metadata"))
    parameters = _mapping_or_empty(input_data.raw_result.get("parameters"))

    if input_data.tool_name == HTTP_REQUEST_TOOL_ID:
        return _adapt_http_request(
            tool_name=input_data.tool_name,
            metadata=metadata,
            parameters=parameters,
            raw_result=input_data.raw_result,
        )
    if input_data.tool_name == HTTP_DOWNLOAD_TOOL_ID:
        return _adapt_http_download(
            tool_name=input_data.tool_name,
            metadata=metadata,
            parameters=parameters,
            raw_result=input_data.raw_result,
        )
    return DeterministicCompressionResult.none(fallback_reason="unsupported_http_tool")


def registered_http_tool_ids() -> tuple[str, ...]:
    """Return HTTP tool ids registered for deterministic MVP coverage."""

    return _HTTP_TOOL_IDS


def register_http_adapters() -> None:
    """Register deterministic HTTP adapters for visible HTTP tools."""

    from .registry import register_adapter

    for tool_id in _HTTP_TOOL_IDS:
        register_adapter(tool_id, http_adapter)


def _adapt_http_request(
    *,
    tool_name: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> DeterministicCompressionResult:
    analysis = analyze_http_request_response(
        source_tool=tool_name,
        metadata=metadata,
        parameters=parameters,
        response_text=_first_text(raw_result.get("stdout")),
        safe_url=_safe_url,
    )
    error = _http_error(metadata=metadata, raw_result=raw_result)

    if error:
        return _error_result(
            kind="http_request_error",
            tool_name=tool_name,
            operation=f"HTTP {analysis.method}",
            url=analysis.url,
            error=error,
            status_code=analysis.status_code,
        )

    if not metadata and not analysis.url:
        return DeterministicCompressionResult.none(fallback_reason="no_http_metadata")

    findings = _request_findings(
        analysis=analysis,
        metadata=metadata,
        parameters=parameters,
    )
    artifact_refs = _http_artifact_refs(raw_result=raw_result, metadata=metadata)
    signal = _compact_signal(
        kind="http_request",
        tool=tool_name,
        method=analysis.method,
        url=analysis.url,
        status_code=analysis.status_code,
        content_type=analysis.content_type,
        content_length=analysis.content_length,
        redirect_count=analysis.redirect_count,
        body_truncated=analysis.body_truncated,
        body_captured=analysis.body_captured,
        response_mode=analysis.response_mode,
        title=analysis.title,
        internal_link_count=len(analysis.internal_links) or None,
        external_link_count=len(analysis.external_links) or None,
        asset_count=len(analysis.asset_refs) or None,
        download_links=list(analysis.download_links[:_HTTP_DOWNLOAD_DISPLAY_LIMIT]),
        form_count=len(analysis.forms) or None,
        cookie_names=[cookie.name for cookie in analysis.cookies[:_HTTP_COOKIE_DISPLAY_LIMIT]],
        body_line_count=analysis.body_line_count,
        artifacts=artifact_refs,
    )

    return DeterministicCompressionResult(
        summary=_summary(
            _http_summary(
                operation=f"HTTP {analysis.method}",
                url=analysis.url,
                status_code=analysis.status_code,
                content_type=analysis.content_type,
                content_length=analysis.content_length,
                redirect_count=analysis.redirect_count,
            )
        ),
        key_findings=tuple(findings),
        structured_signals=(signal,),
        decision_evidence=tuple(_decision_evidence(findings)),
        completeness="partial",
        lossiness_risk="low",
    )


def _adapt_http_download(
    *,
    tool_name: str,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> DeterministicCompressionResult:
    analysis = analyze_http_download_response(
        source_tool=tool_name,
        metadata=metadata,
        parameters=parameters,
        safe_url=_safe_url,
        safe_workspace_path=_safe_workspace_path,
    )
    error = _http_error(metadata=metadata, raw_result=raw_result)

    if error:
        return _error_result(
            kind="http_download_error",
            tool_name=tool_name,
            operation="HTTP download",
            url=analysis.url,
            error=error,
            status_code=analysis.status_code,
            saved_path=analysis.saved_path,
        )

    if not metadata and not analysis.url and not analysis.saved_path:
        return DeterministicCompressionResult.none(fallback_reason="no_http_metadata")

    findings = _download_findings(
        metadata=metadata,
        parameters=parameters,
        saved_path=analysis.saved_path,
        bytes_written=analysis.bytes_written,
        sha256=analysis.sha256,
        redirect_count=analysis.redirect_count,
        url=analysis.url,
    )
    artifact_refs = _http_artifact_refs(raw_result=raw_result, metadata=metadata)
    signal = _compact_signal(
        kind="http_download",
        tool=tool_name,
        method=analysis.method,
        url=analysis.url,
        status_code=analysis.status_code,
        content_type=analysis.content_type,
        content_length=analysis.content_length,
        redirect_count=analysis.redirect_count,
        saved_path=analysis.saved_path,
        bytes_written=analysis.bytes_written,
        sha256=analysis.sha256,
        checksum_verified=analysis.checksum_verified,
        artifacts=artifact_refs,
    )

    return DeterministicCompressionResult(
        summary=_summary(
            _http_summary(
                operation="HTTP GET download",
                url=analysis.url,
                status_code=analysis.status_code,
                content_type=analysis.content_type,
                content_length=(
                    analysis.content_length
                    if analysis.content_length is not None
                    else analysis.bytes_written
                ),
                redirect_count=analysis.redirect_count,
                saved_path=analysis.saved_path,
            )
        ),
        key_findings=tuple(findings),
        structured_signals=(signal,),
        decision_evidence=tuple(_decision_evidence(findings)),
        completeness="partial",
        lossiness_risk="low",
    )


def _request_findings(
    *,
    analysis: WebResponseAnalysis,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    _add_redirect_findings(
        findings,
        redirect_count=analysis.redirect_count,
        effective_url=analysis.url,
        headers=_mapping_or_empty(metadata.get("response_headers")),
    )
    findings.extend(_response_analysis_findings(analysis))
    findings.extend(_auth_findings(metadata=metadata, parameters=parameters))
    findings.extend(_selected_header_findings(_mapping_or_empty(metadata.get("response_headers"))))
    if bool(metadata.get("body_truncated")):
        findings.append("response body truncated; full body may be available as artifact")
    return dedupe_string_list(findings, limit=None)


def _response_analysis_findings(analysis: WebResponseAnalysis) -> list[str]:
    findings: list[str] = []
    if analysis.title:
        findings.append(f'page title: "{analysis.title}"')
    if analysis.headings:
        findings.append(_bounded_join_line("headings", analysis.headings, limit=5))
    if analysis.internal_links:
        findings.append(
            _bounded_join_line(
                "internal links",
                analysis.internal_links,
                limit=_HTTP_LINK_DISPLAY_LIMIT,
            )
        )
    if analysis.external_links:
        findings.append(
            _bounded_join_line(
                "external refs",
                analysis.external_links,
                limit=_HTTP_EXTERNAL_REF_DISPLAY_LIMIT,
            )
        )
    if analysis.download_links:
        findings.append(
            _bounded_join_line(
                "download links",
                analysis.download_links,
                limit=_HTTP_DOWNLOAD_DISPLAY_LIMIT,
            )
        )
    if analysis.asset_refs:
        findings.append(
            _asset_summary_line(
                asset_refs=analysis.asset_refs,
                script_srcs=analysis.script_srcs,
                stylesheet_refs=analysis.stylesheet_refs,
                image_refs=analysis.image_refs,
            )
        )
    for form in analysis.forms[:_HTTP_FORM_DISPLAY_LIMIT]:
        findings.append(_form_line(form))
    if len(analysis.forms) > _HTTP_FORM_DISPLAY_LIMIT:
        findings.append(f"forms: showing {_HTTP_FORM_DISPLAY_LIMIT} of {len(analysis.forms)}")
    for cookie in analysis.cookies[:_HTTP_COOKIE_DISPLAY_LIMIT]:
        findings.append(_cookie_line(cookie))
    if len(analysis.cookies) > _HTTP_COOKIE_DISPLAY_LIMIT:
        findings.append(
            f"cookies: showing {_HTTP_COOKIE_DISPLAY_LIMIT} of {len(analysis.cookies)}"
        )
    if analysis.tech_hints:
        findings.append(
            _bounded_join_line(
                "tech hints",
                analysis.tech_hints,
                limit=_HTTP_TECH_HINT_DISPLAY_LIMIT,
            )
        )
    if analysis.body_line_count is not None:
        findings.append(f"body lines: {analysis.body_line_count}")
    return [compact_evidence_line(finding) for finding in findings if finding]


def _bounded_join_line(label: str, values: Iterable[str], *, limit: int) -> str:
    items = dedupe_string_list(values, limit=None)
    if not items:
        return ""
    shown = items[:limit]
    suffix = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return f"{label}: {', '.join(shown)}{suffix}"


def _asset_summary_line(
    *,
    asset_refs: tuple[str, ...],
    script_srcs: tuple[str, ...],
    stylesheet_refs: tuple[str, ...],
    image_refs: tuple[str, ...],
) -> str:
    parts = [
        "assets:",
        f"total={len(asset_refs)}",
        f"scripts={len(script_srcs)}",
        f"styles={len(stylesheet_refs)}",
        f"images={len(image_refs)}",
    ]
    examples = dedupe_string_list(asset_refs, limit=_HTTP_ASSET_DISPLAY_LIMIT)
    if examples:
        parts.append(f"examples={','.join(examples)}")
        if len(asset_refs) > _HTTP_ASSET_DISPLAY_LIMIT:
            parts.append(f"+{len(asset_refs) - _HTTP_ASSET_DISPLAY_LIMIT} more")
    return " ".join(parts)


def _form_line(form: WebResponseForm) -> str:
    parts = [f"form: method={form.method}"]
    if form.action:
        parts.append(f"action={form.action}")
    if form.input_names:
        parts.append(f"fields={','.join(form.input_names)}")
    if form.input_types:
        parts.append(f"types={','.join(form.input_types)}")
    return " ".join(parts)


def _cookie_line(cookie: WebResponseCookie) -> str:
    parts = [f"cookie set: {cookie.name}"]
    if cookie.flags:
        parts.append(f"flags={','.join(cookie.flags)}")
    if cookie.path:
        parts.append(f"path={cookie.path}")
    if cookie.domain:
        parts.append(f"domain={cookie.domain}")
    return " ".join(parts)


def _download_findings(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
    saved_path: Optional[str],
    bytes_written: Optional[int],
    sha256: Optional[str],
    redirect_count: int,
    url: Optional[str],
) -> list[str]:
    findings: list[str] = []
    _add_redirect_findings(
        findings,
        redirect_count=redirect_count,
        effective_url=url,
        headers=_mapping_or_empty(metadata.get("response_headers")),
    )
    findings.extend(_auth_findings(metadata=metadata, parameters=parameters))
    if saved_path:
        findings.append(f"download saved: {saved_path}")
    if bytes_written is not None:
        findings.append(f"bytes_written: {bytes_written}")
    if sha256:
        findings.append(f"sha256: {sha256}")
    if metadata.get("checksum_verified") is True:
        findings.append("checksum verified")
    elif metadata.get("checksum_verified") is False:
        findings.append("checksum not verified")
    if bool(metadata.get("download_resumed")):
        findings.append("download resumed from existing partial file")
    return dedupe_string_list(findings, limit=10)


def _add_redirect_findings(
    findings: list[str],
    *,
    redirect_count: int,
    effective_url: Optional[str],
    headers: Mapping[str, Any],
) -> None:
    location = _header_value(headers, "location")
    safe_location = _safe_url(location) if location else None
    if redirect_count > 0:
        target = safe_location or effective_url
        suffix = f" to {target}" if target else ""
        findings.append(f"redirects: {redirect_count}{suffix}")
    elif safe_location:
        findings.append(f"redirect location header present: {safe_location}")


def _auth_findings(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    auth_mode = _first_text(metadata.get("auth_mode_used"), parameters.get("auth_mode"))
    if auth_mode and auth_mode.lower() != "none":
        findings.append(f"auth mode used: {auth_mode.lower()}")
    if bool(metadata.get("mtls_used")):
        findings.append("mTLS client certificate used")
    if bool(metadata.get("ca_cert_used")):
        findings.append("custom CA certificate used")
    if _first_text(metadata.get("session_cookie_source")) or parameters.get("cookie_file"):
        findings.append("cookie input used")
    if bool(metadata.get("cookies_persisted")) or parameters.get("persist_cookies"):
        findings.append("response cookies persisted")

    request_headers = _mapping_or_empty(metadata.get("request_headers"))
    for header_name in request_headers:
        lowered = str(header_name).strip().lower()
        if lowered in {"authorization", "cookie", "proxy-authorization"}:
            findings.append(f"request header present: {lowered}")
    return findings


def _selected_header_findings(headers: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    selected_names = (
        *_REDIRECT_RESPONSE_HEADERS,
        *_AUTH_RESPONSE_HEADERS,
        *_SECURITY_RESPONSE_HEADERS,
    )
    for header_name in selected_names:
        value = _header_value(headers, header_name)
        if value is None:
            continue
        if header_name in _VALUE_SENSITIVE_HEADERS:
            findings.append(f"response header {header_name}: present")
        else:
            findings.append(
                compact_evidence_line(
                    f"response header {header_name}: {_safe_header_value(header_name, value)}"
                )
            )
        if len(findings) >= _HEADER_FACT_LIMIT:
            break
    return findings


def _http_artifact_refs(
    *,
    raw_result: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> list[dict[str, str]]:
    candidates: list[Mapping[str, Any]] = []

    artifacts = raw_result.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, str):
                candidates.append({"path": item})
            elif isinstance(item, Mapping):
                candidates.append(item)

    saved_path = _safe_workspace_path(_first_text(metadata.get("saved_path")))
    if saved_path:
        candidates.append(
            {
                "path": saved_path,
                "relative_path": saved_path,
                "artifact_kind": "download",
                "label": "Downloaded file",
            }
        )

    runtime_output_files = metadata.get("runtime_output_files")
    if isinstance(runtime_output_files, list):
        for item in runtime_output_files:
            if isinstance(item, Mapping):
                relative_path = _safe_workspace_path(_first_text(item.get("relative_path")))
                if relative_path:
                    candidates.append(
                        {
                            "path": relative_path,
                            "relative_path": relative_path,
                            "artifact_kind": "download",
                            "label": "Downloaded file",
                        }
                    )

    return sanitize_artifact_refs(candidates)[:_ARTIFACT_REF_LIMIT]


def _http_summary(
    *,
    operation: str,
    url: Optional[str],
    status_code: Optional[int],
    content_type: Optional[str],
    content_length: Optional[int],
    redirect_count: int,
    saved_path: Optional[str] = None,
) -> str:
    target = url or "HTTP target"
    parts = [f"{operation} {target}"]
    if status_code is not None:
        parts.append(f"status {status_code}")
    if content_type:
        parts.append(f"content_type {content_type}")
    if content_length is not None:
        parts.append(f"bytes {content_length}")
    if redirect_count:
        parts.append(f"redirects {redirect_count}")
    if saved_path:
        parts.append(f"saved {saved_path}")
    return "; ".join(parts)


def _http_error(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> Optional[str]:
    explicit = _first_text(metadata.get("error_type"), metadata.get("error"))
    if explicit:
        return explicit
    if as_int(metadata.get("curl_exit_code")) == -2 or raw_result.get("status") == "timeout":
        return "timeout"
    success = raw_result.get("success")
    status = _first_text(raw_result.get("status"))
    if success is False or status in {"error", "failed", "timeout", "cancelled"}:
        if status:
            return status
        exit_code = as_int(raw_result.get("exit_code"))
        if exit_code is not None:
            return f"curl_exit_code={exit_code}"
        return "http operation failed"
    return None


def _error_result(
    *,
    kind: str,
    tool_name: str,
    operation: str,
    url: Optional[str],
    error: str,
    status_code: Optional[int],
    saved_path: Optional[str] = None,
) -> DeterministicCompressionResult:
    target = f" {url}" if url else ""
    status = f" status {status_code};" if status_code is not None else ""
    compact_error = compact_evidence_line(error)
    signal = _compact_signal(
        kind=kind,
        tool=tool_name,
        url=url,
        status_code=status_code,
        saved_path=saved_path,
        error=compact_error,
    )
    return DeterministicCompressionResult(
        summary=_summary(f"{operation}{target} failed:{status} {compact_error}"),
        errors=(compact_error,),
        structured_signals=(signal,),
        completeness="partial",
        lossiness_risk="low",
    )


def _decision_evidence(findings: Iterable[str]) -> list[str]:
    return [compact_evidence_line(value) for value in dedupe_string_list(findings, limit=5)]


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _header_value(headers: Mapping[str, Any], header_name: str) -> Optional[str]:
    wanted = header_name.lower()
    for key, value in headers.items():
        if str(key).strip().lower() == wanted:
            return _first_text(value)
    return None


def _safe_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _bounded_text(value)

    if not parsed.scheme or not parsed.netloc:
        return _bounded_text(value)

    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"<REDACTED>@{host}" if parsed.username or parsed.password else parsed.netloc

    query_pairs = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        safe_value = "<REDACTED>" if key.lower() in _SENSITIVE_QUERY_KEYS else query_value
        query_pairs.append((key, safe_value))

    query = urlencode(query_pairs, doseq=True)
    return _bounded_text(urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment)))


def _safe_header_value(header_name: str, value: str) -> str:
    if header_name.lower() in _VALUE_SENSITIVE_HEADERS:
        return "present"
    text = _bounded_text(value)
    for marker in ("Bearer ", "Basic "):
        idx = text.lower().find(marker.lower())
        if idx >= 0:
            return text[:idx] + marker + "<REDACTED>"
    return text


def _safe_workspace_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().replace("\\", "/")
    if not normalized or "://" in normalized:
        return None
    if normalized.startswith("/workspace/"):
        return normalized
    if normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _bounded_text(value: str) -> str:
    return compact_evidence_line(value)


def _summary(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= COMPACT_SUMMARY_MAX_CHARS:
        return text
    return text[: max(COMPACT_SUMMARY_MAX_CHARS - 3, 0)].rstrip() + "..."


def _compact_signal(**values: Any) -> dict[str, Any]:
    signal: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or value == []:
            continue
        signal[key] = value
    return signal


register_http_adapters()
