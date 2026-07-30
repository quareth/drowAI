"""Pure semantic helpers for web-finding key construction.

This module centralizes deterministic token, URL, and finding-subject-key
helpers that must be shared by runtime-image tool semantics and backend
knowledge adapters without backend imports.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from runtime_shared.semantic.canonical_keys import build_host_ip_key
from runtime_shared.semantic.service_identity import build_service_socket_key

_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9._:/@#-]+")


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
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if not host:
            return ""
        port = parts.port
        include_port = port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        )
        netloc = f"{host}:{port}" if include_port else host
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        return f"{scheme}://{netloc}{path}"
    # Fallback for path-only values.
    return sanitize_token(raw)


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
    DNS-backed URLs remain path facts until a resolved IP is available because
    the canonical service identity is intentionally socket-based.
    """

    canonical_url = normalize_url(url)
    source_name = str(source or "").strip()
    target = str(target_url or "").strip()
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

    try:
        ip = str(ipaddress.ip_address(parsed.hostname))
    except ValueError:
        return [path_observation]

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    service_key = build_service_socket_key(ip=ip, protocol="tcp", port=port)
    common_payload = {
        "source": source_name,
        "target_url": target,
        "evidence_source": "web_response",
    }
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
