"""Shared semantic observation/evidence builders for ffuf tools.

This module is the single semantic mapping authority for both ffuf variants
(`web_application_fuzzers.ffuf` and `web_crawlers.ffuf`). It emits:
- canonical semantic observations (`web.path_discovered`) for crawler results
- bounded semantic evidence entries using the locked shared vocabulary

Result-summary policy:
- Always emit `results_count` from parsed ffuf results.
- Emit `results_count_after_filters` only when metadata exposes a distinct
  post-filter count. Do not synthesize duplicate counts.

No backend imports are allowed here; this module is agent-runtime only.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse

from agent.semantic.evidence_vocabulary import SemanticEvidenceType

FFUF_VARIANT_CRAWLER = "crawler"
FFUF_VARIANT_FUZZER = "fuzzer"

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
    """Emit crawler-only `web.path_discovered` observations for concrete results."""
    if detect_ffuf_variant(metadata) != FFUF_VARIANT_CRAWLER:
        return []

    target = _target_template(metadata, args)
    observations: list[dict[str, Any]] = []
    seen_subject_keys: set[str] = set()

    for row in _as_list(metadata.get("results")):
        result = _as_mapping(row)
        url_value = result.get("url")
        if not isinstance(url_value, str) or not url_value.strip():
            continue
        discovered_url = url_value.strip()
        parsed = urlparse(discovered_url)
        path = parsed.path or "/"
        subject_key = f"web.path:{discovered_url.lower()}"
        if subject_key in seen_subject_keys:
            continue
        seen_subject_keys.add(subject_key)

        payload: dict[str, Any] = {
            "source": "ffuf",
            "path": path,
            "url": discovered_url,
        }
        if target:
            payload["target_template"] = target
        status = _safe_int(result.get("status"))
        if status > 0:
            payload["status_code"] = status
        size = _safe_int(result.get("length"))
        if size > 0:
            payload["response_size"] = size

        observations.append(
            {
                "observation_type": "web.path_discovered",
                "subject_type": "web.path",
                "subject_key": subject_key,
                "payload": payload,
            }
        )

    return observations


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
