"""Pure semantic helpers for canonical web observations and finding keys.

This module centralizes deterministic web-response facts, token, URL, and
finding-subject-key helpers shared by runtime tool semantics and backend
Knowledge consumers without backend imports.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from runtime_shared.semantic.canonical_keys import (
    build_host_dns_key,
    build_host_ip_key,
    validate_subject_key_characters,
)
from runtime_shared.semantic.service_identity import build_service_socket_key

_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9._:/@#-]+")
_WEB_PATH_PREFIX = "web.path:"


def sanitize_token(value: Any) -> str:
    """Return lowercase token constrained to subject-key safe characters."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _SAFE_TOKEN_RE.sub("-", raw).strip("-")


def normalize_url(value: Any) -> str:
    """Normalize URL-like values into stable scheme://host[:port]/path form."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if any(unicodedata.category(character).startswith("C") for character in raw):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme and parts.netloc:
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if not host:
            return ""
        try:
            port = parts.port
        except ValueError:
            return ""
        include_port = port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        )
        serialized_host = f"[{host}]" if ":" in host else host
        netloc = f"{serialized_host}:{port}" if include_port else serialized_host
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        return f"{scheme}://{netloc}{path}"
    # Fallback for path-only values.
    return sanitize_token(raw)


def canonicalize_web_path_subject_key(value: Any) -> str:
    """Return a canonical web-path key while preserving URL path case."""

    raw = validate_subject_key_characters(value)
    if not raw.startswith(_WEB_PATH_PREFIX):
        raise ValueError("web.path subject_key must use web.path prefix")
    canonical_url = normalize_url(raw.removeprefix(_WEB_PATH_PREFIX))
    if not canonical_url:
        raise ValueError("web.path subject_key must contain a valid URL")
    return validate_subject_key_characters(f"{_WEB_PATH_PREFIX}{canonical_url}")


def strip_url_userinfo(value: Any) -> str:
    """Return a URL with username/password userinfo removed for durable semantics."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        username = parts.username
        password = parts.password
    except ValueError:
        return ""
    if username is None and password is None:
        return raw

    host = parts.hostname or ""
    if not host:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    serialized_host = f"[{host}]" if ":" in host else host
    netloc = f"{serialized_host}:{port}" if port is not None else serialized_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_web_origin_key(url: Any) -> str:
    """Return canonical web origin key derived from URL input."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def build_web_response_observations(
    *,
    url: Any,
    source: Any,
    target_url: Any,
    status_code: Any = None,
    response_size: Any = None,
    calibrated: bool = False,
) -> list[dict[str, Any]]:
    """Build canonical facts for one observed HTTP response.

    IP-backed URLs prove a host and socket alongside the discovered path.
    DNS-backed URLs prove a DNS asset and path, but not a socket service until
    a resolved IP is available for the canonical service identity.
    """

    canonical_url = normalize_url(url)
    source_name = str(source or "").strip()
    target = strip_url_userinfo(target_url)
    parsed = urlsplit(canonical_url)
    if (
        not canonical_url
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not source_name
        or not target
    ):
        return []

    path_payload: dict[str, Any] = {
        "url": canonical_url,
        "source": source_name,
        "path": parsed.path or "/",
        "target_url": target,
    }
    normalized_status = _nonnegative_int(status_code)
    if normalized_status is not None and normalized_status > 0:
        path_payload["status_code"] = normalized_status
    normalized_size = _nonnegative_int(response_size)
    if normalized_size is not None:
        path_payload["response_size"] = normalized_size
    if calibrated:
        path_payload["calibrated"] = True

    path_observation = {
        "observation_type": "web.path_discovered",
        "subject_type": "web.path",
        "subject_key": f"web.path:{canonical_url}",
        "payload": path_payload,
    }

    common_payload = {
        "source": source_name,
        "target_url": target,
        "evidence_source": "web_response",
    }

    try:
        ip = str(ipaddress.ip_address(parsed.hostname))
    except ValueError:
        try:
            dns_key = build_host_dns_key(parsed.hostname)
        except ValueError:
            return [path_observation]
        return [
            {
                "observation_type": "dns.name_discovered",
                "subject_type": "host.dns",
                "subject_key": dns_key,
                "payload": {
                    **common_payload,
                    "hostname": dns_key.removeprefix("host.dns:"),
                },
            },
            path_observation,
        ]

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    service_key = build_service_socket_key(ip=ip, protocol="tcp", port=port)
    return [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": build_host_ip_key(ip),
            "payload": {
                **common_payload,
                "ip": ip,
            },
        },
        {
            "observation_type": "network.service_observed",
            "subject_type": "service.socket",
            "subject_key": service_key,
            "payload": {
                **common_payload,
                "ip": ip,
                "protocol": "tcp",
                "port": port,
                "service_name": parsed.scheme,
                "application_protocol": parsed.scheme,
                "reachability": "observed",
            },
        },
        path_observation,
    ]


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def build_finding_subject_key(
    *,
    detector_id: str,
    target_url: str,
    parameter: str | None = None,
    variant_id: str | None = None,
) -> str:
    """Build canonical finding key tied to detector+target(+parameter/variant)."""
    detector = sanitize_token(detector_id)
    target = normalize_url(target_url)
    pieces = [detector, target]
    if parameter:
        pieces.append(f"param-{sanitize_token(parameter)}")
    if variant_id:
        pieces.append(f"variant-{sanitize_token(variant_id)}")
    compact = ":".join(piece for piece in pieces if piece)
    return f"finding.instance:{compact}"
