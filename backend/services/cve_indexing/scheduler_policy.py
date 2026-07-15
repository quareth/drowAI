"""Pure scheduling policy helpers for CVE sync dispatch decisions.

Scope:
- Computes whether daily CVE sync is due from last-success cursor and current time.

Boundary:
- Contains no scheduler task management or DB/session interaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from backend.services.cve_indexing.primitives import normalize_hour, to_utc


def is_daily_sync_due(
    *,
    last_successful_sync_at: datetime | None,
    daily_sync_hour_utc: int,
    now: datetime,
) -> bool:
    """Return True when the daily schedule window has not yet been satisfied."""
    normalized_now = to_utc(now)
    schedule_hour = normalize_hour(daily_sync_hour_utc)
    scheduled_today = normalized_now.replace(
        hour=schedule_hour,
        minute=0,
        second=0,
        microsecond=0,
    )

    if last_successful_sync_at is None:
        # For first automatic run, wait until the selected daily hour is reached.
        return normalized_now >= scheduled_today

    normalized_last = to_utc(last_successful_sync_at)
    if normalized_now <= normalized_last:
        return False

    window_start = (
        scheduled_today
        if normalized_now >= scheduled_today
        else scheduled_today - timedelta(days=1)
    )
    return normalized_last < window_start


__all__ = ["is_daily_sync_due"]
