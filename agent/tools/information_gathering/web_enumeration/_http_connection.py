"""Connection and request assembly helpers for HTTP curl tooling.

This module contains URL validation and common curl argv construction used by
both HTTP request and download tools.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

SUPPORTED_HTTP_SCHEMES = {"http", "https"}


def validate_http_url(target: str) -> str:
    """Validate that a target is an absolute HTTP(S) URL."""
    value = (target or "").strip()
    if not value:
        raise ValueError("target URL is required")

    parsed = urlparse(value)
    if parsed.scheme.lower() not in SUPPORTED_HTTP_SCHEMES:
        raise ValueError("target scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("target must be an absolute URL with hostname")
    return value


def build_curl_common_args(
    *,
    timeout: int,
    follow_redirects: bool,
    max_redirects: int,
    insecure_tls: bool,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    headers: Optional[Mapping[str, str]] = None,
    content_type: Optional[str] = None,
    connect_timeout: Optional[int] = None,
    speed_limit: Optional[int] = None,
    speed_time: Optional[int] = None,
) -> List[str]:
    """Build shared curl arguments used by HTTP tools."""
    cmd: List[str] = ["curl", "--silent", "--show-error"]
    cmd.extend(["--max-time", str(timeout)])
    if connect_timeout is not None:
        cmd.extend(["--connect-timeout", str(connect_timeout)])
    if speed_limit is not None:
        cmd.extend(["--speed-limit", str(speed_limit)])
    if speed_time is not None:
        cmd.extend(["--speed-time", str(speed_time)])

    if follow_redirects:
        cmd.append("--location")
        cmd.extend(["--max-redirs", str(max_redirects)])
    if insecure_tls:
        cmd.append("--insecure")
    if proxy:
        cmd.extend(["--proxy", proxy])
    if user_agent:
        cmd.extend(["--user-agent", user_agent])

    header_items = dict(headers or {})
    has_content_type = any(k.lower() == "content-type" for k in header_items.keys())
    if content_type and not has_content_type:
        header_items["Content-Type"] = content_type

    for key, value in header_items.items():
        cmd.extend(["--header", f"{key}: {value}"])

    return cmd


def build_tls_curl_args(
    *,
    client_cert_path: Optional[str] = None,
    client_key_path: Optional[str] = None,
    client_key_passphrase: Optional[str] = None,
    ca_cert_path: Optional[str] = None,
) -> List[str]:
    """Build curl argv flags for mTLS and custom CA trust material."""
    cmd: List[str] = []
    if client_cert_path:
        cmd.extend(["--cert", client_cert_path])
    if client_key_path:
        cmd.extend(["--key", client_key_path])
    if client_key_passphrase:
        cmd.extend(["--pass", client_key_passphrase])
    if ca_cert_path:
        cmd.extend(["--cacert", ca_cert_path])
    return cmd


def build_connection_control_curl_args(
    *,
    resolve: Optional[Sequence[str]] = None,
    connect_to: Optional[Sequence[str]] = None,
    interface: Optional[str] = None,
    local_port: Optional[int] = None,
    ipv4_only: bool = False,
    ipv6_only: bool = False,
) -> tuple[List[str], Dict[str, Any]]:
    """Build curl argv flags and metadata for DNS/connection controls."""
    cmd: List[str] = []
    applied: Dict[str, Any] = {
        "resolve": list(resolve or []),
        "connect_to": list(connect_to or []),
        "interface": interface,
        "local_port": local_port,
        "ipv4_only": bool(ipv4_only),
        "ipv6_only": bool(ipv6_only),
    }

    for entry in list(resolve or []):
        cmd.extend(["--resolve", entry])
    for entry in list(connect_to or []):
        cmd.extend(["--connect-to", entry])
    if interface:
        cmd.extend(["--interface", interface])
    if local_port is not None:
        cmd.extend(["--local-port", str(local_port)])
    if ipv4_only:
        cmd.append("-4")
    if ipv6_only:
        cmd.append("-6")

    return cmd, applied
