"""Canonical key builders for identity domains.

This module defines deterministic key normalization/building for:
- hosts (IP and DNS)
- services (socket)
- web URLs
- findings (vulnerability identity)
- relationships (edge identity)"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import urlsplit
from runtime_shared.semantic.canonical_keys import (
    build_finding_vulnerability_key as shared_build_finding_vulnerability_key,
    build_host_dns_key as shared_build_host_dns_key,
    build_host_ip_key as shared_build_host_ip_key,
    build_relationship_edge_key as shared_build_relationship_edge_key,
    sanitize_finding_token as shared_sanitize_finding_token,
)
from runtime_shared.semantic.service_identity import (
    build_service_socket_key as shared_build_service_socket_key,
)


def normalize_web_url(value: str) -> str:
    """Normalize URL into stable scheme://host/path form (no query/fragment)."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("url cannot be empty")

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        raise ValueError("url must include scheme and host")

    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if not host:
        raise ValueError("url host cannot be empty")

    # Keep non-default ports only.
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    normalized_path = parts.path or "/"
    normalized_path = re.sub(r"/{2,}", "/", normalized_path)
    normalized_path = posixpath.normpath(normalized_path)
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_path == "/.":
        normalized_path = "/"

    return f"{scheme}://{netloc}{normalized_path}"


def build_host_ip_key(ip: str) -> str:
    return shared_build_host_ip_key(ip)


def build_host_dns_key(hostname: str) -> str:
    return shared_build_host_dns_key(hostname)


def build_service_socket_key(*, ip: str, protocol: str, port: int | str) -> str:
    return shared_build_service_socket_key(ip=ip, protocol=protocol, port=port)


def build_web_url_key(url: str) -> str:
    return f"web.url:{normalize_web_url(url)}"


def build_finding_vulnerability_key(
    *,
    subject_key: str,
    detector_id: str,
) -> str:
    return shared_build_finding_vulnerability_key(
        subject_key=subject_key,
        detector_id=detector_id,
    )


def build_secret_exposure_finding_key(
    *,
    subject_key: str,
    detector_id: str,
    protocol: str,
    exposure_kind: str,
    proof_id: str,
    flow_key: str = "",
) -> str:
    """Build a redaction-safe key for one observed credential exposure proof."""
    normalized_subject_key = str(subject_key or "").strip().lower()
    normalized_detector_id = shared_sanitize_finding_token(detector_id)
    normalized_protocol = shared_sanitize_finding_token(protocol)
    normalized_kind = shared_sanitize_finding_token(exposure_kind)
    normalized_proof = shared_sanitize_finding_token(proof_id)
    normalized_flow = shared_sanitize_finding_token(flow_key)
    if not normalized_subject_key:
        raise ValueError("subject_key cannot be empty")
    if not normalized_detector_id:
        raise ValueError("detector_id cannot be empty")
    if not normalized_protocol:
        raise ValueError("protocol cannot be empty")
    if not normalized_kind:
        raise ValueError("exposure_kind cannot be empty")
    if not normalized_proof:
        raise ValueError("proof_id cannot be empty")

    detector_parts = [
        "secret-exposure",
        normalized_detector_id,
        normalized_protocol,
        normalized_kind,
    ]
    if normalized_flow:
        detector_parts.append(normalized_flow)
    detector_parts.append(normalized_proof)
    return shared_build_finding_vulnerability_key(
        subject_key=normalized_subject_key,
        detector_id="/".join(detector_parts),
    )


def build_relationship_edge_key(
    *,
    source_subject_key: str,
    relationship_type: str,
    target_subject_key: str,
) -> str:
    return shared_build_relationship_edge_key(
        source_subject_key=source_subject_key,
        relationship_type=relationship_type,
        target_subject_key=target_subject_key,
    )
