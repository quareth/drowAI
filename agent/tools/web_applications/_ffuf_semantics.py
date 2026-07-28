"""Shared semantic observation/evidence builders for ffuf tools.

This module is the single semantic mapping authority for both ffuf variants
(`web_application_fuzzers.ffuf` and `web_crawlers.ffuf`). It emits:
- canonical host, service, and web-path observations for confirmed responses
- bounded semantic evidence entries using the locked shared vocabulary

Result-summary policy:
- Always emit `results_count` from parsed ffuf results.
- Emit `results_count_after_filters` only when metadata exposes a distinct
  post-filter count. Do not synthesize duplicate counts.

No backend imports are allowed here; this module is agent-runtime only.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse, urlsplit

from runtime_shared.semantic.pentest_facts import SemanticEvidenceType
from runtime_shared.semantic.web_common import (
    build_web_response_observations,
    normalize_url,
)

FFUF_VARIANT_CRAWLER = "crawler"
FFUF_VARIANT_FUZZER = "fuzzer"
_FFUF_SOURCE_BY_VARIANT = {
    FFUF_VARIANT_CRAWLER: "web_applications.web_crawlers.ffuf",
    FFUF_VARIANT_FUZZER: "web_applications.web_application_fuzzers.ffuf",
}
_MAX_PATHS_PER_ORIGIN = 200
_SOFT_404_STATUS_CODE = 404
_SOFT_404_RESPONSE_SIZE_MAX = 255

_MATCHER_FIELDS: tuple[str, ...] = (
    "match_status",
    "match_lines",
    "match_words",
    "match_size",
    "match_time",
    "match_regex",
)
_FILTER_FIELDS: tuple[str, ...] = (
    "filter_status",
    "filter_lines",
    "filter_words",
    "filter_size",
    "filter_time",
    "filter_regex",
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    return 0


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
    return None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"true", "1", "yes", "on"}:
            return True
        if raw in {"false", "0", "no", "off"}:
            return False
    return None


def _pick_value(source: Mapping[str, Any], *keys: str) -> Any:
    if not source:
        return None
    lowered = {str(k).lower(): v for k, v in source.items()}
    for key in keys:
        if key in source:
            return source[key]
        lowered_key = key.lower()
        if lowered_key in lowered:
            return lowered[lowered_key]
    return None


def _target_template(metadata: Mapping[str, Any], args: Any) -> str:
    config = _as_mapping(metadata.get("config"))
    target = _pick_value(config, "url", "target")
    if isinstance(target, str) and target.strip():
        return target.strip()
    arg_target = getattr(args, "target", None)
    if isinstance(arg_target, str) and arg_target.strip():
        return arg_target.strip()
    return ""


def _normalize_variant_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {FFUF_VARIANT_CRAWLER, FFUF_VARIANT_FUZZER}:
        return normalized
    return None


def detect_ffuf_variant(metadata: Mapping[str, Any]) -> str:
    """Return deterministic ffuf variant label ('crawler' or 'fuzzer')."""
    explicit_variant = _normalize_variant_value(metadata.get("ffuf_variant"))
    if explicit_variant is not None:
        return explicit_variant

    config = _as_mapping(metadata.get("config"))
    target = _pick_value(config, "url", "target")
    if isinstance(target, str):
        normalized = target.strip().rstrip("/")
        if normalized.endswith("/FUZZ"):
            return FFUF_VARIANT_CRAWLER

    commandline = _as_list(metadata.get("commandline"))
    command_tokens = {str(token).strip() for token in commandline}
    if "-recursion" in command_tokens or "-D" in command_tokens:
        return FFUF_VARIANT_CRAWLER

    return FFUF_VARIANT_FUZZER


def build_ffuf_semantic_observations(
    metadata: Mapping[str, Any],
    args: Any,
) -> list[dict[str, Any]]:
    """Emit canonical web-response observations for supported ffuf rows."""
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    variant = detect_ffuf_variant(metadata_dict)
    source = _FFUF_SOURCE_BY_VARIANT[variant]
    target = _target_template(metadata, args)
    rows = _apply_per_origin_cap(
        _apply_pre_observation_drop_rules(
            _candidate_web_path_payloads(metadata_dict, args=args, source=source, target=target)
        )
    )

    observations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    for row in rows:
        canonical_url = str(row.pop("_subject_key")).removeprefix("web.path:")
        for observation in build_web_response_observations(
            url=canonical_url,
            source=row.get("source"),
            target_url=row.get("target_url"),
            status_code=row.get("status_code"),
            response_size=row.get("response_size"),
            calibrated=bool(row.get("calibrated")),
        ):
            payload = _as_mapping(observation.get("payload"))
            marker = (
                str(observation.get("observation_type") or ""),
                str(observation.get("subject_type") or ""),
                str(observation.get("subject_key") or ""),
                tuple(sorted((str(key), str(value)) for key, value in payload.items())),
            )
            if marker in seen:
                continue
            seen.add(marker)
            observations.append(observation)

    return observations


def _candidate_web_path_payloads(
    metadata: Mapping[str, Any],
    *,
    args: Any,
    source: str,
    target: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    calibration = _calibration_policy(metadata, args)
    target_url = target or ""

    for row in _as_list(metadata.get("results")):
        result = _as_mapping(row)
        canonical_url = normalize_url(_pick_value(result, "url", "target_url"))
        if not canonical_url:
            continue
        parsed = urlsplit(canonical_url)
        if not parsed.scheme or not parsed.netloc:
            continue

        payload: dict[str, Any] = {
            "_subject_key": f"web.path:{canonical_url}",
            "url": canonical_url,
            "source": source,
            "path": parsed.path or "/",
            "target_url": target_url or canonical_url,
        }

        status = _coerce_int(_pick_value(result, "status_code", "status"))
        if status is not None and status > 0:
            payload["status_code"] = status

        response_size = _coerce_int(_pick_value(result, "response_size", "length", "size"))
        if response_size is not None and response_size >= 0:
            payload["response_size"] = response_size

        content_words = _coerce_int(_pick_value(result, "content_words", "words"))
        content_lines = _coerce_int(_pick_value(result, "content_lines", "lines"))
        matched_calibration = _matched_calibration_fields(
            status_code=status,
            response_size=response_size,
            content_words=content_words,
            content_lines=content_lines,
            calibration=calibration,
        )
        if matched_calibration:
            payload["calibrated"] = True

        payloads.append(payload)

    return payloads


def _apply_pre_observation_drop_rules(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for row in rows:
        status_code = row.get("status_code")
        response_size = row.get("response_size")
        if (
            isinstance(status_code, int)
            and status_code == _SOFT_404_STATUS_CODE
            and isinstance(response_size, int)
            and response_size <= _SOFT_404_RESPONSE_SIZE_MAX
        ):
            continue
        kept.append(row)
    return kept


def _apply_per_origin_cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        url_value = str(row.get("url") or "")
        origin_key = _web_origin_key(url_value)
        if not origin_key:
            continue
        grouped.setdefault(origin_key, []).append(row)

    capped_rows: list[dict[str, Any]] = []
    for origin_key in sorted(grouped):
        ranked_rows = sorted(
            grouped[origin_key],
            key=lambda item: (
                _status_rank(item),
                str(item.get("url") or ""),
                str(item.get("path") or ""),
            ),
        )
        capped_rows.extend(ranked_rows[:_MAX_PATHS_PER_ORIGIN])
    return capped_rows


def _web_origin_key(url_value: str) -> str:
    normalized = normalize_url(url_value)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _status_rank(item: Mapping[str, Any]) -> int:
    status_code = item.get("status_code")
    if not isinstance(status_code, int):
        return 999
    if status_code == 200:
        return 0
    if status_code == 301:
        return 1
    if status_code == 302:
        return 2
    if 200 <= status_code < 300:
        return 3
    if 300 <= status_code < 400:
        return 4
    if status_code == 401:
        return 5
    if status_code == 403:
        return 6
    return 7


def _calibration_policy(metadata: Mapping[str, Any], args: Any) -> dict[str, set[int]]:
    config = _as_mapping(metadata.get("config"))
    matchers_block = _as_mapping(_pick_value(config, "matchers"))
    filters_block = _as_mapping(_pick_value(matchers_block, "filters", "Filters"))
    is_calibrated = _safe_bool(_pick_value(matchers_block, "IsCalibrated", "is_calibrated"))
    if is_calibrated is None:
        is_calibrated = bool(getattr(args, "auto_calibrate", False))
    if not is_calibrated:
        return {}
    return {
        "status_code": _int_set_from_filter(
            _pick_value(filters_block, "status", "status_code")
            or getattr(args, "filter_status", None)
        ),
        "response_size": _int_set_from_filter(
            _pick_value(filters_block, "size", "length", "response_size")
            or getattr(args, "filter_size", None)
        ),
        "content_words": _int_set_from_filter(
            _pick_value(filters_block, "words", "content_words")
            or getattr(args, "filter_words", None)
        ),
        "content_lines": _int_set_from_filter(
            _pick_value(filters_block, "lines", "content_lines")
            or getattr(args, "filter_lines", None)
        ),
    }


def _int_set_from_filter(value: Any) -> set[int]:
    values: set[int] = set()
    if isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = str(value or "").split(",")
    for candidate in candidates:
        raw = str(candidate).strip()
        if raw.isdigit():
            values.add(int(raw))
    return values


def _matched_calibration_fields(
    *,
    status_code: int | None,
    response_size: int | None,
    content_words: int | None,
    content_lines: int | None,
    calibration: Mapping[str, set[int]],
) -> dict[str, int]:
    matched: dict[str, int] = {}
    for name, value in (
        ("status_code", status_code),
        ("response_size", response_size),
        ("content_words", content_words),
        ("content_lines", content_lines),
    ):
        if value is not None and value in calibration.get(name, set()):
            matched[name] = value
    return matched


def _append_execution_parameter(
    evidence: list[dict[str, Any]],
    *,
    name: str,
    value: Any,
    unit: str | None = None,
) -> None:
    if value in (None, "", []):
        return
    entry: dict[str, Any] = {
        "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
        "name": name,
        "value": value,
    }
    if unit:
        entry["detail"] = {"unit": unit}
    evidence.append(entry)


def _append_matcher_or_filter(
    evidence: list[dict[str, Any]],
    *,
    name: str,
    value: Any,
    kind: str,
    source: str,
    negated: bool = False,
) -> None:
    if value in (None, "", []):
        return
    detail: dict[str, Any] = {"kind": kind, "source": source}
    if negated:
        detail["negated"] = True
    evidence.append(
        {
            "type": SemanticEvidenceType.MATCHER_OR_FILTER.value,
            "name": name,
            "value": str(value),
            "detail": detail,
        }
    )


def _append_baseline(
    evidence: list[dict[str, Any]],
    *,
    name: str,
    value: Any,
    source: str,
    strategy: str | None = None,
    unit: str | None = None,
    note: str | None = None,
) -> None:
    if value in (None, "", []):
        return
    detail: dict[str, Any] = {"source": source}
    if strategy:
        detail["strategy"] = strategy
    if unit:
        detail["unit"] = unit
    if note:
        detail["note"] = note
    evidence.append(
        {
            "type": SemanticEvidenceType.BASELINE.value,
            "name": name,
            "value": value,
            "detail": detail,
        }
    )


def build_ffuf_semantic_evidence(
    metadata: Mapping[str, Any],
    args: Any,
) -> list[dict[str, Any]]:
    """Build vocabulary-conformant semantic evidence entries for ffuf runs."""
    evidence: list[dict[str, Any]] = []
    metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
    config = _as_mapping(metadata_dict.get("config"))
    matchers_block = _as_mapping(_pick_value(config, "matchers"))
    filters_block = _as_mapping(_pick_value(matchers_block, "filters", "Filters"))

    variant = detect_ffuf_variant(metadata_dict)
    evidence.append(
        {
            "type": SemanticEvidenceType.VARIANT.value,
            "name": "ffuf_variant",
            "value": variant,
        }
    )

    target = _target_template(metadata_dict, args)
    if target:
        parsed_target = urlparse(target)
        detail: dict[str, Any] = {
            "placeholder": "FUZZ" if "FUZZ" in target else "",
            "scheme": parsed_target.scheme or "",
            "host": parsed_target.hostname or "",
        }
        if parsed_target.port is not None:
            detail["port"] = parsed_target.port
        evidence.append(
            {
                "type": SemanticEvidenceType.TARGET_TEMPLATE.value,
                "name": "target_template",
                "value": target,
                "detail": detail,
            }
        )

    _append_execution_parameter(evidence, name="threads", value=getattr(args, "threads", None), unit="workers")
    _append_execution_parameter(
        evidence,
        name="request_timeout",
        value=getattr(args, "request_timeout", None),
        unit="seconds",
    )
    _append_execution_parameter(evidence, name="method", value=getattr(args, "method", "GET"))
    _append_execution_parameter(evidence, name="wordlist_ref", value=getattr(args, "wordlist", None))

    input_mode = "input_cmd" if getattr(args, "input_cmd", None) else "wordlist"
    if getattr(args, "inline_wordlist", None):
        input_mode = "inline_wordlist"
    elif getattr(args, "wordlists", None):
        input_mode = "multi_wordlist"
    _append_execution_parameter(evidence, name="inputmode", value=input_mode)

    def _join_field_values(*field_names: str, source: Mapping[str, Any] | None = None) -> str:
        parts: list[str] = []
        for field_name in field_names:
            raw_value = (
                source.get(field_name)
                if source is not None
                else getattr(args, field_name, None)
            )
            if raw_value in (None, "", []):
                continue
            label = field_name.removeprefix("match_").removeprefix("filter_")
            parts.append(f"{label}={raw_value}")
        return ", ".join(parts)

    matcher_status_ranges = _join_field_values("match_status", "match_time")
    _append_matcher_or_filter(
        evidence,
        name="matcher_status_ranges",
        value=matcher_status_ranges,
        kind="status_range",
        source="args",
    )
    matcher_size_filters = _join_field_values("match_size", "match_lines", "match_words")
    _append_matcher_or_filter(
        evidence,
        name="matcher_size_filters",
        value=matcher_size_filters,
        kind="size_filter",
        source="args",
    )
    matcher_regex_keyword = _join_field_values("match_regex")
    _append_matcher_or_filter(
        evidence,
        name="matcher_regex_keyword_filters",
        value=matcher_regex_keyword,
        kind="pattern_filter",
        source="args",
    )
    filter_exclusions = _join_field_values(*_FILTER_FIELDS)
    _append_matcher_or_filter(
        evidence,
        name="filter_status_size_exclusions",
        value=filter_exclusions,
        kind="filter_exclusion",
        source="args",
        negated=True,
    )

    is_calibrated = _safe_bool(_pick_value(matchers_block, "IsCalibrated", "is_calibrated"))
    if is_calibrated is None:
        is_calibrated = bool(getattr(args, "auto_calibrate", False))
    calibrated_filter_values = _join_field_values(
        "status",
        "size",
        "lines",
        "words",
        "time",
        "regex",
        source=filters_block,
    )
    _append_matcher_or_filter(
        evidence,
        name="calibrated_filter_group",
        value=calibrated_filter_values if is_calibrated else None,
        kind="filter_exclusion",
        source="calibration",
        negated=True,
    )
    _append_baseline(
        evidence,
        name="autocalibration",
        value=is_calibrated,
        source="ffuf",
        strategy="automatic",
    )

    strategies = getattr(args, "auto_calibrate_strategies", None)
    if isinstance(strategies, list) and strategies:
        _append_baseline(
            evidence,
            name="autocalibration_strategies",
            value=",".join(str(item) for item in strategies if str(item).strip()),
            source="args",
            strategy="manual",
        )

    calibrated_filter_size = _pick_value(filters_block, "size", "Size", "filter_size")
    if calibrated_filter_size in (None, ""):
        calibrated_filter_size = getattr(args, "filter_size", None)
    _append_baseline(
        evidence,
        name="filter_size",
        value=str(calibrated_filter_size) if calibrated_filter_size not in (None, "") else None,
        source="calibration" if is_calibrated else "args",
        unit="bytes",
        note="autocalibration_filter" if is_calibrated else None,
    )

    results = [row for row in _as_list(metadata_dict.get("results")) if isinstance(row, Mapping)]
    results_count = len(results)
    evidence.append(
        {
            "type": SemanticEvidenceType.RESULT_SUMMARY.value,
            "name": "results_count",
            "value": results_count,
            "detail": {
                "before_filter_count": results_count,
                "after_filter_count": results_count,
                "unit": "results",
            },
        }
    )
    post_filter_count_raw = _pick_value(
        metadata_dict,
        "results_count_after_filters",
        "post_filter_count",
    )
    post_filter_count = _safe_int(post_filter_count_raw)
    if post_filter_count_raw not in (None, "") and post_filter_count != results_count:
        evidence.append(
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "results_count_after_filters",
                "value": post_filter_count,
                "detail": {"unit": "results"},
            }
        )

    active_stop_flags = [
        stop_flag
        for stop_flag in ("stop_on_403", "stop_on_errors", "stop_on_any")
        if bool(getattr(args, stop_flag, False))
    ]
    if active_stop_flags:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "stop_flags_active",
                "value": True,
                "detail": {
                    "severity": "info",
                    "note": ",".join(active_stop_flags),
                },
            }
        )

    has_wordlist_input = any(
        bool(getattr(args, field, None))
        for field in ("wordlist", "wordlists", "inline_wordlist", "input_cmd")
    )
    if not has_wordlist_input:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "wordlist_missing",
                "value": True,
                "detail": {"severity": "warning", "note": "no_input_source"},
            }
        )

    timeout_meta = _as_mapping(metadata_dict.get("timeout"))
    if timeout_meta:
        evidence.append(
            {
                "type": SemanticEvidenceType.DIAGNOSTIC.value,
                "name": "timeout_hit",
                "value": True,
                "detail": {"severity": "warning", "note": "execution_timeout"},
            }
        )

    return evidence


__all__ = (
    "FFUF_VARIANT_CRAWLER",
    "FFUF_VARIANT_FUZZER",
    "build_ffuf_semantic_evidence",
    "build_ffuf_semantic_observations",
    "detect_ffuf_variant",
)
