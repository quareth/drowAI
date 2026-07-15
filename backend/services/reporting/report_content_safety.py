"""Detect internal reporting identifiers in customer-facing report text."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_INTERNAL_IDENTIFIER_PATTERNS = (
    re.compile(r"\bevidence_archive:[A-Za-z0-9_.:-]+"),
    re.compile(r"\bknowledge_[A-Za-z0-9_.:-]+"),
    re.compile(r"\bsource_watermark(?:_[A-Za-z0-9_]+)?\b"),
    re.compile(r"\b(?:task_memo_ids|knowledge_refs|evidence_refs)\b"),
)
_INTERNAL_REFERENCE_REPLACEMENT = "[internal reference removed]"


def internal_identifier_markers(
    value: Any,
    *,
    forbidden_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return internal identifier markers found in customer-facing text."""

    text = _text(value)
    if not text:
        return ()

    markers: list[str] = []
    for pattern in _INTERNAL_IDENTIFIER_PATTERNS:
        markers.extend(match.group(0) for match in pattern.finditer(text))

    for ref in _ordered_forbidden_refs(forbidden_refs):
        if ref in text:
            markers.append(ref)

    return _unique(markers)


def sanitize_customer_text(
    value: Any,
    *,
    forbidden_refs: Iterable[str] = (),
) -> str:
    """Return customer-facing text with internal reporting refs removed."""

    text = _text(value)
    if not text:
        return ""

    sanitized = text
    for pattern in _INTERNAL_IDENTIFIER_PATTERNS:
        sanitized = pattern.sub(_INTERNAL_REFERENCE_REPLACEMENT, sanitized)
    for ref in _ordered_forbidden_refs(forbidden_refs):
        sanitized = sanitized.replace(ref, _INTERNAL_REFERENCE_REPLACEMENT)
    return " ".join(sanitized.split())


def _ordered_forbidden_refs(forbidden_refs: Iterable[str]) -> tuple[str, ...]:
    refs = _unique(str(ref).strip() for ref in forbidden_refs if str(ref).strip())
    return tuple(sorted(refs, key=lambda ref: (-len(ref), ref)))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return tuple(unique)


__all__ = [
    "internal_identifier_markers",
    "sanitize_customer_text",
]
