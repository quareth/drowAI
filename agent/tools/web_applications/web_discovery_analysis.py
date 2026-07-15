"""Family-level web discovery result analysis.

This module normalizes parsed web discovery tool results into reusable records
and response fingerprint groups. It is pure analysis code: no runtime calls,
filesystem reads, backend imports, compression contracts, or LLM behavior.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WebDiscoveryRecord:
    """Normalized result row emitted by a web discovery tool."""

    source_tool: str
    url: Optional[str]
    path: Optional[str]
    status_code: Optional[int]
    response_size: Optional[int]
    words: Optional[int]
    lines: Optional[int]
    redirect: Optional[str]
    content_type: Optional[str]
    input_value: Optional[str]


@dataclass(frozen=True)
class WebDiscoveryGroup:
    """Response fingerprint group derived from discovery records."""

    count: int
    status_code: Optional[int]
    response_size: Optional[int]
    words: Optional[int]
    lines: Optional[int]
    redirect: Optional[str]
    content_type: Optional[str]
    input_ranges: tuple[str, ...]
    input_examples: tuple[str, ...]
    examples: tuple[str, ...]


@dataclass(frozen=True)
class WebDiscoveryAnalysis:
    """Reusable analysis output for one web discovery result set."""

    source_tool: str
    target_template: Optional[str]
    records: tuple[WebDiscoveryRecord, ...]
    groups: tuple[WebDiscoveryGroup, ...]
    status_distribution: Mapping[str, int]


def analyze_ffuf_web_discovery(
    results: Any,
    *,
    source_tool: str,
    max_examples_per_group: int,
    max_input_ranges_per_group: int,
    target_template: Optional[str] = None,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> WebDiscoveryAnalysis:
    """Analyze parsed ffuf result rows as web discovery records."""

    records = normalize_ffuf_web_discovery_records(
        results,
        source_tool=source_tool,
        safe_url=safe_url,
    )
    groups = group_web_discovery_records(
        records,
        max_examples=max_examples_per_group,
        max_input_ranges=max_input_ranges_per_group,
    )
    return WebDiscoveryAnalysis(
        source_tool=source_tool,
        target_template=target_template,
        records=records,
        groups=groups,
        status_distribution=_status_distribution(records),
    )


def normalize_ffuf_web_discovery_records(
    results: Any,
    *,
    source_tool: str,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]] = None,
) -> tuple[WebDiscoveryRecord, ...]:
    """Normalize parsed ffuf rows into family-level discovery records."""

    rows = results if isinstance(results, list) else []
    records: list[WebDiscoveryRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        record = _normalize_ffuf_row(row, source_tool=source_tool, safe_url=safe_url)
        if record.url or record.path or record.input_value:
            records.append(record)
    return tuple(records)


def group_web_discovery_records(
    records: Sequence[WebDiscoveryRecord],
    *,
    max_examples: int,
    max_input_ranges: int,
) -> tuple[WebDiscoveryGroup, ...]:
    """Group discovery records by response fingerprint and rank the groups."""

    grouped: dict[tuple[Any, ...], list[WebDiscoveryRecord]] = defaultdict(list)
    for record in records:
        grouped[_fingerprint(record)].append(record)

    groups = [
        _build_group(
            bucket,
            max_examples=max_examples,
            max_input_ranges=max_input_ranges,
        )
        for bucket in grouped.values()
    ]
    return tuple(sorted(groups, key=_group_rank))


def _normalize_ffuf_row(
    row: Mapping[str, Any],
    *,
    source_tool: str,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> WebDiscoveryRecord:
    raw_url = _first_text(row.get("url"))
    url = _sanitize_url(raw_url, safe_url=safe_url)
    path = _path_from_row(row, url=url)
    redirect = _sanitize_url(
        _first_text(row.get("redirectlocation"), row.get("redirect_location")),
        safe_url=safe_url,
    )
    return WebDiscoveryRecord(
        source_tool=source_tool,
        url=url,
        path=path,
        status_code=_as_int(_pick(row, "status", "status_code")),
        response_size=_as_int(
            _pick(row, "length", "response_size", "size", "content_length")
        ),
        words=_as_int(_pick(row, "words", "word_count")),
        lines=_as_int(_pick(row, "lines", "line_count")),
        redirect=redirect,
        content_type=_first_text(
            _pick(
                row,
                "content_type",
                "content-type",
                "contenttype",
                "Content-Type",
            )
        ),
        input_value=_input_value(row.get("input")),
    )


def _fingerprint(record: WebDiscoveryRecord) -> tuple[Any, ...]:
    return (
        record.status_code,
        record.response_size,
        record.words,
        record.lines,
        record.redirect,
        record.content_type,
    )


def _build_group(
    records: Sequence[WebDiscoveryRecord],
    *,
    max_examples: int,
    max_input_ranges: int,
) -> WebDiscoveryGroup:
    first = records[0]
    input_values = tuple(
        value for value in (record.input_value for record in records) if value
    )
    numeric_ranges = _numeric_ranges(input_values, limit=max_input_ranges)
    input_examples: tuple[str, ...] = ()
    if not numeric_ranges:
        input_examples = tuple(_representative_values(input_values, limit=max_examples))

    examples = tuple(
        _representative_values(
            [_example_value(record) for record in records],
            limit=max_examples,
        )
    )
    return WebDiscoveryGroup(
        count=len(records),
        status_code=first.status_code,
        response_size=first.response_size,
        words=first.words,
        lines=first.lines,
        redirect=first.redirect,
        content_type=first.content_type,
        input_ranges=numeric_ranges,
        input_examples=input_examples,
        examples=examples,
    )


def _group_rank(group: WebDiscoveryGroup) -> tuple[int, int, int, str, str]:
    status = group.status_code or 0
    size = group.response_size or 0
    first_example = group.examples[0] if group.examples else ""
    return (
        _status_priority(status),
        group.count,
        -size,
        group.redirect or "",
        first_example,
    )


def _status_priority(status: int) -> int:
    if 200 <= status <= 299:
        return 0
    if status in {401, 403}:
        return 1
    if 500 <= status <= 599:
        return 2
    if 300 <= status <= 399:
        return 3
    if 400 <= status <= 499:
        return 4
    if status > 0:
        return 5
    return 6


def _numeric_ranges(values: Sequence[str], *, limit: int) -> tuple[str, ...]:
    numbers: list[int] = []
    for value in values:
        stripped = value.strip()
        if not stripped.isdigit():
            return ()
        numbers.append(int(stripped))
    if not numbers:
        return ()

    ranges: list[str] = []
    unique_numbers = sorted(set(numbers))
    start = previous = unique_numbers[0]
    for number in unique_numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(_format_range(start, previous))
        start = previous = number
    ranges.append(_format_range(start, previous))

    if len(ranges) <= limit:
        return tuple(ranges)
    remaining = len(ranges) - limit
    return tuple(ranges[:limit] + [f"+{remaining} more ranges"])


def _format_range(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _representative_values(values: Sequence[Optional[str]], *, limit: int) -> list[str]:
    cleaned = _dedupe_string_list((value for value in values if value), limit=None)
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return cleaned[:limit]
    return _dedupe_string_list(cleaned[: limit - 1] + cleaned[-1:], limit=limit)


def _example_value(record: WebDiscoveryRecord) -> Optional[str]:
    return record.path or record.url or record.input_value


def _status_distribution(records: Sequence[WebDiscoveryRecord]) -> Mapping[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        if record.status_code is not None:
            counts[str(record.status_code)] += 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _input_value(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        if "FUZZ" in value:
            return _text_or_none(value.get("FUZZ"))

        usable_values = [
            item
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).upper() != "FFUFHASH"
        ]
        if len(usable_values) == 1:
            return _text_or_none(usable_values[0])
        parts = [
            f"{key}={item}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).upper() != "FFUFHASH"
        ]
        return _compact_line(",".join(parts)) if parts else None
    return _text_or_none(value)


def _path_from_row(row: Mapping[str, Any], *, url: Optional[str]) -> Optional[str]:
    path = _first_text(row.get("path"))
    if path:
        return path
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    return parsed.path or "/"


def _sanitize_url(
    value: Optional[str],
    *,
    safe_url: Optional[Callable[[Optional[str]], Optional[str]]],
) -> Optional[str]:
    if not value:
        return None
    if safe_url is None:
        return value
    return safe_url(value)


def _pick(source: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in source.items()}
    for key in keys:
        if key in source:
            return source[key]
        lowered_key = key.lower()
        if lowered_key in lowered:
            return lowered[lowered_key]
    return None


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _text_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe_string_list(values: Any, *, limit: Optional[int]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _compact_line(value: Any) -> str:
    text = str(value or "").strip()
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(text) <= 240:
        return text
    return text[:237].rstrip() + "..."
