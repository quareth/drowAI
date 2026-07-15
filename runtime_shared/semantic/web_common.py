"""Pure semantic helpers for web-finding key construction.

This module centralizes deterministic token, URL, and finding-subject-key
helpers that must be shared by runtime-image tool semantics and backend
knowledge adapters without backend imports.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

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
