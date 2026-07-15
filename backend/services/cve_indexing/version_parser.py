"""Lenient version parsing and comparison for CVE version rules.

Scope:
- Parses non-strict real-world versions that appear in CVE affected payloads.
- Compares versions with a strict-SemVer-first strategy and lenient fallback.

Boundary:
- No database/session access and no CVE lookup orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import re
from typing import Any

from backend.services.cve_indexing.semver import compare_semver

_VERSION_RE = re.compile(r"^(?P<core>\d+(?:\.\d+){0,5})(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$")


@dataclass(slots=True, frozen=True)
class ParsedVersion:
    """Parsed version for lenient comparisons."""

    raw: str
    core: tuple[int, ...]
    prerelease: tuple[int | str, ...] = ()


def parse_version(value: Any) -> ParsedVersion | None:
    """Parse a version string with lenient support for CVE custom formats."""
    text = _clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"*", "x", "any", "all", "n/a"}:
        return None
    matched = _VERSION_RE.match(text)
    if matched is None:
        return None
    try:
        core = tuple(int(part) for part in matched.group("core").split("."))
    except ValueError:
        return None
    prerelease = _parse_prerelease(matched.group("prerelease"))
    if prerelease is None:
        return None
    return ParsedVersion(raw=text, core=core, prerelease=prerelease)


def compare_versions(left: Any, right: Any) -> int | None:
    """Compare version values; return None when either side is unsupported."""
    strict = compare_semver(_clean_text(left), _clean_text(right))
    if strict is not None:
        return strict
    left_parsed = parse_version(left)
    right_parsed = parse_version(right)
    if left_parsed is None or right_parsed is None:
        return None
    return compare_parsed_versions(left_parsed, right_parsed)


def compare_parsed_versions(left: ParsedVersion, right: ParsedVersion) -> int:
    """Compare two pre-parsed versions using semver-like precedence."""
    max_len = max(len(left.core), len(right.core))
    left_core = left.core + (0,) * (max_len - len(left.core))
    right_core = right.core + (0,) * (max_len - len(right.core))
    if left_core < right_core:
        return -1
    if left_core > right_core:
        return 1
    return _compare_prerelease(left.prerelease, right.prerelease)


def sort_versions(values: list[ParsedVersion]) -> list[ParsedVersion]:
    """Return versions sorted using ParsedVersion comparison semantics."""
    return sorted(values, key=functools.cmp_to_key(compare_parsed_versions))


def _parse_prerelease(value: str | None) -> tuple[int | str, ...] | None:
    if value is None:
        return ()
    if not value:
        return None
    tokens: list[int | str] = []
    for part in value.split("."):
        if not part:
            return None
        if part.isdigit():
            if len(part) > 1 and part.startswith("0"):
                return None
            tokens.append(int(part))
            continue
        tokens.append(part)
    return tuple(tokens)


def _compare_prerelease(left: tuple[int | str, ...], right: tuple[int | str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    max_len = max(len(left), len(right))
    for idx in range(max_len):
        if idx >= len(left):
            return -1
        if idx >= len(right):
            return 1
        l_item = left[idx]
        r_item = right[idx]
        if l_item == r_item:
            continue
        l_numeric = isinstance(l_item, int)
        r_numeric = isinstance(r_item, int)
        if l_numeric and r_numeric:
            return -1 if l_item < r_item else 1
        if l_numeric and not r_numeric:
            return -1
        if not l_numeric and r_numeric:
            return 1
        return -1 if str(l_item) < str(r_item) else 1
    return 0


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = ["ParsedVersion", "compare_parsed_versions", "compare_versions", "parse_version", "sort_versions"]
