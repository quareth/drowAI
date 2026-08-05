"""Canonical identity-key helpers shared by runtime and Knowledge consumers.

This backend-free module owns the common subject-key alphabet, host IP/DNS and
relationship-edge keys, finding-key construction, and token normalization.
"""

from __future__ import annotations

import ipaddress
import re

_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9-]{1,63}$")
_RELATIONSHIP_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TOKEN_PATTERN = re.compile(r"[^a-z0-9._/-]+")
_SUBJECT_KEY_PATTERN = re.compile(
    r"^[A-Za-z0-9._~:/@#%!$&'()*+,;=\[\]-]{1,512}$"
)


def validate_subject_key_characters(value: object) -> str:
    """Return a bounded subject key without changing identity-significant case."""

    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("subject_key cannot be empty")
    if any(character.isspace() for character in normalized):
        raise ValueError("subject_key cannot contain spaces")
    if not _SUBJECT_KEY_PATTERN.fullmatch(normalized):
        raise ValueError("subject_key contains unsupported characters")
    return normalized


def canonicalize_subject_key(value: object) -> str:
    """Return a bounded lowercase subject key for case-insensitive identities."""

    return validate_subject_key_characters(value).lower()


def _normalize_ip(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("ip cannot be empty")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError as exc:
        raise ValueError(f"invalid ip address: {value}") from exc


def _normalize_dns_name(value: object) -> str:
    candidate = str(value or "").strip().rstrip(".")
    if not candidate:
        raise ValueError("hostname cannot be empty")
    try:
        hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("hostname contains invalid label characters") from exc
    labels = hostname.split(".")
    if any(not label for label in labels):
        raise ValueError("hostname contains empty labels")
    for label in labels:
        if not _DNS_LABEL_PATTERN.fullmatch(label):
            raise ValueError("hostname contains invalid label characters")
    return hostname


def sanitize_finding_token(value: str) -> str:
    """Normalize token text for finding-key compatibility."""
    return _TOKEN_PATTERN.sub("-", str(value or "").strip().lower()).strip("-")


def build_host_ip_key(ip: object) -> str:
    """Build a canonical host.ip key."""
    return f"host.ip:{_normalize_ip(ip)}"


def build_host_dns_key(hostname: object) -> str:
    """Build a canonical host.dns key."""
    return f"host.dns:{_normalize_dns_name(hostname)}"


def build_finding_vulnerability_key(
    *,
    subject_key: str,
    detector_id: str,
) -> str:
    """Build canonical key for one vulnerability finding identity."""
    normalized_subject_key = str(subject_key or "").strip().lower()
    if not normalized_subject_key:
        raise ValueError("subject_key cannot be empty")
    normalized_detector_id = sanitize_finding_token(detector_id)
    if not normalized_detector_id:
        raise ValueError("detector_id cannot be empty")
    return f"finding.vulnerability:{normalized_subject_key}:{normalized_detector_id}"


def build_relationship_edge_key(
    *,
    source_subject_key: object,
    relationship_type: object,
    target_subject_key: object,
) -> str:
    """Build a canonical relationship.edge key."""
    source = str(source_subject_key or "").strip().lower()
    target = str(target_subject_key or "").strip().lower()
    rel_type = str(relationship_type or "").strip().lower()

    if not source:
        raise ValueError("source_subject_key cannot be empty")
    if not target:
        raise ValueError("target_subject_key cannot be empty")
    if not _RELATIONSHIP_TYPE_PATTERN.fullmatch(rel_type):
        raise ValueError("relationship_type must be a lowercase token")

    return f"relationship.edge:{source}:{rel_type}:{target}"
