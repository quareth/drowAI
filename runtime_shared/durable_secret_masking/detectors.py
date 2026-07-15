"""Reusable secret detectors for app-owned durable payload masking."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .types import SecretMatch

_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization\s*:\s*)?bearer\s+)(?P<secret>[A-Za-z0-9._~+/=-]{6,})"
)
_KEY_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|token|secret|private[_-]?key|credential|session[_-]?id)"
    r"\b\s*[:=]\s*['\"]?)"
    r"(?P<secret>[^'\"\s,;&|<>]{4,})"
)
_SENSITIVE_FIELD_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"['\"]?"
    r"(?:[a-z0-9_-]+\.)*"
    r"(?:authorization|proxy_authorization|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token|"
    r"secret|private[_-]?key|credential|session[_-]?id|"
    r"request\.command_parameter|auth\.argument|command_parameter)"
    r"['\"]?\s*[:=]\s*['\"]?)"
    r"(?P<secret>(?!bearer\b)[^'\"\s,;&|<>}\]]{4,})"
)
_COOKIE_PAIR_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:cookie|set-cookie)\s*:\s*[^=\s;]{2,64}=)(?P<secret>[^;\s]{4,})"
)


def detect_durable_secret_spans(text: str) -> tuple[SecretMatch, ...]:
    """Return non-overlapping secret-like spans in ``text``."""

    value = str(text or "")
    matches: list[SecretMatch] = []
    for pattern, kind, group_name in (
        (_PRIVATE_KEY_BLOCK_RE, "private_key", None),
        (_BEARER_RE, "token", "secret"),
        (_KEY_VALUE_RE, "secret", "secret"),
        (_SENSITIVE_FIELD_VALUE_RE, "secret", "secret"),
        (_COOKIE_PAIR_RE, "cookie", "secret"),
    ):
        for match in pattern.finditer(value):
            span = match.span(group_name) if group_name else match.span()
            matches.append(SecretMatch(start=span[0], end=span[1], kind=kind))
    return tuple(_without_overlaps(matches))


def _without_overlaps(matches: Iterable[SecretMatch]) -> list[SecretMatch]:
    ordered = sorted(matches, key=lambda item: (item.start, -(item.end - item.start)))
    selected: list[SecretMatch] = []
    occupied_until = -1
    for match in ordered:
        if match.start < occupied_until:
            continue
        selected.append(match)
        occupied_until = match.end
    return selected
