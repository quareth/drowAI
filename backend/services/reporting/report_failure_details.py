"""Project safe report-generation failure details for job status reads.

This module extracts only bounded operational diagnostics from persisted report
metadata. It never returns generated report prose, prompts, or raw references.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_FAILURE_DETAIL_KEYS = (
    "failed_section_id",
    "failed_section_order",
    "failed_section_type",
)


def report_job_failure_details(
    generation_metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return safe failure details from a failed report attempt metadata payload."""

    if not isinstance(generation_metadata, Mapping):
        return None

    details: dict[str, Any] = {}
    for key in _FAILURE_DETAIL_KEYS:
        value = generation_metadata.get(key)
        if value is None:
            continue
        if key == "failed_section_order":
            details[key] = _safe_int(value)
        else:
            details[key] = _safe_string(value)

    validation_issues = _safe_validation_issues(
        generation_metadata.get("validation_issues")
    )
    if validation_issues:
        details["validation_issues"] = validation_issues

    return details if details else None


def _safe_validation_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    issues: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        code = _safe_string(item.get("code"))
        path = _safe_string(item.get("path"))
        if not code or not path:
            continue
        issues.append({"code": code, "path": path})
    return issues


def _safe_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 256:
        return None
    return text


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = ["report_job_failure_details"]
