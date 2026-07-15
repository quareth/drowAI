"""CPE 2.3 parsing helpers for CVE product identity matching.

Scope:
- Parses CPE 2.3 formatted URIs into structured vendor/product fields.
- Exposes normalized product identity tokens used by match candidate resolution.

Boundary:
- No database/session access and no CVE lookup orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

_CPE_PREFIX = "cpe:2.3:"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(slots=True, frozen=True)
class ParsedCpe23:
    """Parsed CPE 2.3 URI fields used by product matching."""

    raw: str
    part: str
    vendor: str
    product: str
    version: str
    update: str
    edition: str
    language: str
    sw_edition: str
    target_sw: str
    target_hw: str
    other: str

    @property
    def vendor_norm(self) -> str | None:
        return _normalize_name(self.vendor)

    @property
    def product_norm(self) -> str | None:
        return _normalize_name(self.product)

    @property
    def identity_tokens(self) -> tuple[str, ...]:
        """Return normalized vendor/product tokens for identity matching."""
        tokens: list[str] = []
        for value in (self.vendor_norm, self.product_norm):
            if value is None:
                continue
            tokens.extend(_TOKEN_RE.findall(value))
        deduped: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        return tuple(deduped)


def parse_cpe23(value: Any) -> ParsedCpe23 | None:
    """Parse one CPE 2.3 URI into a typed object; return None when malformed."""
    raw = _clean_text(value)
    if raw is None or not raw.lower().startswith(_CPE_PREFIX):
        return None
    parts = raw.split(":")
    if len(parts) != 13:
        return None
    if parts[0].lower() != "cpe" or parts[1] != "2.3":
        return None
    part = _clean_text(parts[2])
    vendor = _clean_text(parts[3])
    product = _clean_text(parts[4])
    if part is None or vendor is None or product is None:
        return None
    return ParsedCpe23(
        raw=raw,
        part=part,
        vendor=vendor,
        product=product,
        version=_clean_text(parts[5]) or "*",
        update=_clean_text(parts[6]) or "*",
        edition=_clean_text(parts[7]) or "*",
        language=_clean_text(parts[8]) or "*",
        sw_edition=_clean_text(parts[9]) or "*",
        target_sw=_clean_text(parts[10]) or "*",
        target_hw=_clean_text(parts[11]) or "*",
        other=_clean_text(parts[12]) or "*",
    )


def extract_cpe_identity_tokens(values: list[str] | None) -> tuple[str, ...]:
    """Return distinct vendor/product tokens extracted from CPE URI values."""
    if not values:
        return ()
    tokens: list[str] = []
    for item in values:
        parsed = parse_cpe23(item)
        if parsed is None:
            continue
        tokens.extend(parsed.identity_tokens)
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return tuple(deduped)


def _normalize_name(value: str) -> str | None:
    lowered = value.lower()
    normalized = " ".join(lowered.split())
    return normalized or None


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = ["ParsedCpe23", "extract_cpe_identity_tokens", "parse_cpe23"]
