"""Pure semantic helpers for network-discovery metadata normalization.

This module stays backend-free so runtime-image tool semantics and management
adapters can share deterministic service-version normalization behavior.
"""

from __future__ import annotations

import re
from typing import Any

_VERSION_EXTRACT_RE = re.compile(
    r"(?P<version>v?\d+[a-z0-9]*(?:\.\d+[a-z0-9]*){0,5}(?:[-_][a-z0-9]+)*)",
    re.IGNORECASE,
)
_VERSION_RELATION_PHRASES: tuple[tuple[str, str], ...] = (
    ("or later", "gte"),
    ("and later", "gte"),
    ("or newer", "gte"),
    ("and newer", "gte"),
    ("or above", "gte"),
    ("and above", "gte"),
    ("or greater", "gte"),
    ("and greater", "gte"),
    ("or earlier", "lte"),
    ("and earlier", "lte"),
    ("or lower", "lte"),
    ("and lower", "lte"),
    ("or below", "lte"),
    ("and below", "lte"),
)


def normalize_service_version(value: Any) -> tuple[str | None, str | None, str | None]:
    """Return normalized version, raw version text, and optional relation qualifier."""
    raw = str(value or "").strip()
    if not raw:
        return (None, None, None)

    lowered = raw.lower()
    relation: str | None = None
    for phrase, candidate in _VERSION_RELATION_PHRASES:
        if phrase in lowered:
            relation = candidate
            lowered = lowered.replace(phrase, " ")
            break

    if raw.endswith("+"):
        relation = relation or "gte"
        lowered = lowered.rstrip("+").strip()

    matched = _VERSION_EXTRACT_RE.search(lowered)
    if matched is None:
        # Keep original value for display when no machine-comparable token is found.
        return (raw, None, relation)

    normalized = matched.group("version").strip()
    if not normalized:
        return (None, None, relation)

    raw_value = raw if normalized != raw else None
    return (normalized, raw_value, relation)
