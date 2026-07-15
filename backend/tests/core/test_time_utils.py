"""Unit tests for backend.core.time_utils UTC helpers."""

from datetime import UTC, datetime, timedelta, timezone

from backend.core.time_utils import format_iso, to_utc, utc_now


def test_utc_now_returns_aware_utc_datetime() -> None:
    value = utc_now()
    assert value.tzinfo is UTC


def test_utc_now_is_close_to_system_utc_now() -> None:
    now = utc_now()
    delta = abs((datetime.now(UTC) - now).total_seconds())
    assert delta <= 1


def test_to_utc_tags_naive_datetime_without_shifting() -> None:
    naive = datetime(2024, 1, 2, 3, 4, 5)
    normalized = to_utc(naive)
    assert normalized == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_to_utc_returns_same_value_for_aware_utc_datetime() -> None:
    aware_utc = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    normalized = to_utc(aware_utc)
    assert normalized == aware_utc


def test_to_utc_converts_other_timezones_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    aware_other = datetime(2024, 1, 2, 3, 4, 5, tzinfo=plus_two)
    normalized = to_utc(aware_other)
    assert normalized == datetime(2024, 1, 2, 1, 4, 5, tzinfo=UTC)


def test_format_iso_aware_datetime_ends_with_utc_offset() -> None:
    value = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_iso(value).endswith("+00:00")


def test_format_iso_naive_datetime_assumes_utc_offset() -> None:
    value = datetime(2024, 1, 2, 3, 4, 5)
    assert format_iso(value).endswith("+00:00")
