"""Runtime configuration for reporting preparation safety gates.

This module owns environment parsing for reporting-specific operational
settings that must be shared by services without embedding raw env reads in
business logic.
"""

from __future__ import annotations

import os
from typing import Final, Mapping


DEFAULT_MEMO_PREPARING_STALE_TIMEOUT_SECONDS: Final[int] = 1800
MEMO_PREPARING_STALE_TIMEOUT_ENV: Final[str] = (
    "REPORTING_MEMO_PREPARING_STALE_TIMEOUT_SECONDS"
)


def get_memo_preparing_stale_timeout_seconds(
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return the stale timeout for in-flight task memo preparation attempts."""

    env = environ if environ is not None else os.environ
    raw_value = env.get(MEMO_PREPARING_STALE_TIMEOUT_ENV)
    if raw_value is None:
        return DEFAULT_MEMO_PREPARING_STALE_TIMEOUT_SECONDS
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return DEFAULT_MEMO_PREPARING_STALE_TIMEOUT_SECONDS
    if parsed <= 0:
        return DEFAULT_MEMO_PREPARING_STALE_TIMEOUT_SECONDS
    return parsed


__all__ = [
    "DEFAULT_MEMO_PREPARING_STALE_TIMEOUT_SECONDS",
    "MEMO_PREPARING_STALE_TIMEOUT_ENV",
    "get_memo_preparing_stale_timeout_seconds",
]
