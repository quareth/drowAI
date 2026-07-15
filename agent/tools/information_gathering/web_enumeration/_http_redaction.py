"""Redaction primitives for HTTP tool outputs and metadata.

This module centralizes secret masking helpers used by HTTP request/download
tooling so new capability surfaces can reuse one redaction seam.
"""

from __future__ import annotations

import re
from typing import Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

REDACTED_VALUE = "<REDACTED>"
_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
_AUTH_HEADER_RE = re.compile(r"(?im)^(authorization\s*:\s*)(.+)$")
_COOKIE_HEADER_RE = re.compile(r"(?im)^((?:set-)?cookie\s*:\s*)(.+)$")
_SECRET_HEADER_RE = re.compile(
    r"(?im)^((?:x[-_])?(?:api[-_]?key|auth[-_]?token|access[-_]?token|refresh[-_]?token|id[-_]?token)\s*:\s*)(.+)$"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_TOKEN_RE = re.compile(r"(?i)\bbasic\s+[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIALS_RE = re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^@\s]+@")
_CURL_USER_ARG_RE = re.compile(r"(?i)(--user\s+)([^\s]+)")
_CURL_BEARER_ARG_RE = re.compile(r"(?i)(--oauth2-bearer\s+)([^\s]+)")
_CURL_PASS_ARG_RE = re.compile(r"(?i)(--pass\s+)([^\s]+)")
_KEY_PASSPHRASE_RE = re.compile(r"(?im)^((?:client[_-]?key[_-]?passphrase|key[_-]?passphrase)\s*[:=]\s*)(.+)$")


def redact_sensitive_headers(headers: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Return a copy of headers with sensitive values redacted."""
    if not headers:
        return {}

    redacted: Dict[str, str] = {}
    for key, value in headers.items():
        key_lc = key.lower().strip()
        if (
            key_lc in _SENSITIVE_HEADER_NAMES
            or "token" in key_lc
            or "secret" in key_lc
            or "key" in key_lc
            or "password" in key_lc
            or "auth" in key_lc
        ):
            redacted[key] = REDACTED_VALUE
        else:
            redacted[key] = value
    return redacted


def redact_url_credentials(url: Optional[str]) -> Optional[str]:
    """Redact embedded credentials from URL-style strings."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return url

    if parts.username is None and parts.password is None:
        return url

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    redacted_netloc = f"{REDACTED_VALUE}@{host}" if host else REDACTED_VALUE
    return urlunsplit((parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment))


def redact_text_secrets(text: str) -> str:
    """Redact common credential patterns from free-form text output."""
    if not text:
        return text

    redacted = _URL_CREDENTIALS_RE.sub(f"https://{REDACTED_VALUE}@", text)
    redacted = _AUTH_HEADER_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _COOKIE_HEADER_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _SECRET_HEADER_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _BEARER_TOKEN_RE.sub(f"bearer {REDACTED_VALUE}", redacted)
    redacted = _BASIC_TOKEN_RE.sub(f"basic {REDACTED_VALUE}", redacted)
    redacted = _CURL_USER_ARG_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _CURL_BEARER_ARG_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _CURL_PASS_ARG_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _KEY_PASSPHRASE_RE.sub(rf"\1{REDACTED_VALUE}", redacted)
    return redacted
