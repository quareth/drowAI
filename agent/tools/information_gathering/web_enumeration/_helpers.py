"""Compatibility facade for shared HTTP helper primitives.

This module keeps the existing helper import surface stable while delegating
implementation to concern-specific modules introduced in Phase 0.
"""

from __future__ import annotations

from ._http_auth import build_auth_curl_args
from ._http_body import build_multipart_form_args, parse_response_headers, parse_status_line, split_http_response
from ._http_capabilities import (
    UnsupportedHttpVersionError,
    build_http_version_curl_args,
    detect_curl_http_capabilities,
    reset_curl_http_capabilities_cache,
)
from ._http_connection import (
    build_connection_control_curl_args,
    build_curl_common_args,
    build_tls_curl_args,
    validate_http_url,
)
from ._http_redaction import redact_sensitive_headers, redact_text_secrets, redact_url_credentials
from ._http_retry import build_retry_rate_curl_args
from ._http_session import build_session_curl_args

__all__ = [
    "validate_http_url",
    "redact_sensitive_headers",
    "redact_text_secrets",
    "redact_url_credentials",
    "build_curl_common_args",
    "build_auth_curl_args",
    "build_session_curl_args",
    "build_multipart_form_args",
    "build_tls_curl_args",
    "build_connection_control_curl_args",
    "build_retry_rate_curl_args",
    "build_http_version_curl_args",
    "detect_curl_http_capabilities",
    "reset_curl_http_capabilities_cache",
    "UnsupportedHttpVersionError",
    "split_http_response",
    "parse_status_line",
    "parse_response_headers",
]
