"""Strict semantic-version parsing and comparison helpers.

Scope:
- Parses SemVer 2.0.0 strings (including prerelease/build metadata).
- Compares two parsed versions using SemVer precedence rules.

Boundary:
- No database/session access and no CVE orchestration coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Union


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


SemVerIdentifier = Union[int, str]


@dataclass(slots=True, frozen=True)
class SemVer:
    """Parsed semantic version with canonical comparison fields."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[SemVerIdentifier, ...] = ()


def parse_semver(value: str | None) -> SemVer | None:
    """Parse strict SemVer 2.0.0 string; return None for unsupported input."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    match = _SEMVER_RE.match(raw)
    if match is None:
        return None

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    prerelease_raw = match.group("prerelease")
    prerelease = _parse_prerelease(prerelease_raw)
    if prerelease is None:
        return None

    return SemVer(
        major=major,
        minor=minor,
        patch=patch,
        prerelease=prerelease,
    )


def compare_semver(left: str | None, right: str | None) -> int | None:
    """Compare SemVer strings; return None if either side is unsupported."""
    left_parsed = parse_semver(left)
    right_parsed = parse_semver(right)
    if left_parsed is None or right_parsed is None:
        return None

    left_core = (left_parsed.major, left_parsed.minor, left_parsed.patch)
    right_core = (right_parsed.major, right_parsed.minor, right_parsed.patch)
    if left_core < right_core:
        return -1
    if left_core > right_core:
        return 1

    return _compare_prerelease(left_parsed.prerelease, right_parsed.prerelease)


def _parse_prerelease(value: str | None) -> tuple[SemVerIdentifier, ...] | None:
    if value is None:
        return ()
    if not value:
        return None

    identifiers: list[SemVerIdentifier] = []
    for token in value.split("."):
        if not token:
            return None
        if token.isdigit():
            # SemVer forbids leading zeros in numeric identifiers.
            if len(token) > 1 and token.startswith("0"):
                return None
            identifiers.append(int(token))
            continue
        identifiers.append(token)
    return tuple(identifiers)


def _compare_prerelease(
    left: tuple[SemVerIdentifier, ...],
    right: tuple[SemVerIdentifier, ...],
) -> int:
    # A version without prerelease has higher precedence.
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


__all__ = ["SemVer", "compare_semver", "parse_semver"]
