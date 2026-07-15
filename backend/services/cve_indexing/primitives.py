"""Shared CVE indexing primitives for UTC normalization and scheduling values.

Scope:
- Centralizes datetime normalization helpers reused across CVE services.
- Provides canonical "now" and hour-normalization helpers.

Boundary:
- Contains no DB access, scheduling loops, or sync orchestration.
"""

from __future__ import annotations

from datetime import datetime

from backend.core.time_utils import utc_now, to_utc  # noqa: F401 (re-export)


def to_utc_hour(value: datetime) -> datetime:
    """Normalize datetime to UTC hour boundary."""
    normalized = to_utc(value)
    return normalized.replace(minute=0, second=0, microsecond=0)


def normalize_hour(value: int) -> int:
    """Clamp hour values to valid UTC 0-23 range."""
    return min(23, max(0, int(value)))


__all__ = ["normalize_hour", "to_utc", "to_utc_hour", "utc_now"]
