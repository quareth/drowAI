"""UTC datetime helpers used by backend services.

Scope:
- Provide a single source for "current UTC time" and datetime normalization.

Boundary:
- No persistence, no API formatting policy beyond ISO conversion.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    - Naive datetimes are assumed UTC and tagged.
    - Aware datetimes are converted to UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_iso(value: datetime) -> str:
    """Format a datetime as ISO 8601 with UTC offset."""
    return to_utc(value).isoformat()
