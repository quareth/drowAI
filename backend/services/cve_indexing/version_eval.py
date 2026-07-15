"""Conservative helpers for CVE version applicability evaluation.

Scope:
- Evaluates whether a product-version fingerprint is affected by one CVE
  affected-entry version payload with deterministic, explainable results.

Boundary:
- No SQL queries, service orchestration, or tool/runtime context coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
from typing import Any, Literal

from backend.services.cve_indexing.version_parser import compare_versions, parse_version


VersionApplicabilityStatus = Literal["applicable", "possible", "no_match"]


@dataclass(slots=True, frozen=True)
class VersionApplicability:
    """Deterministic applicability outcome with a stable explanation."""

    status: VersionApplicabilityStatus
    explanation: str


def evaluate_version_applicability(
    *,
    fingerprint_version: str | None,
    versions_json: list[dict[str, Any]] | None,
    default_status: str | None,
) -> VersionApplicability:
    """Classify version applicability with conservative semantics.

    Behavior:
    - missing/weak version evidence => "possible"
    - confidently matched affected rule => "applicable"
    - confidently matched unaffected rule, or outside supported affected rules => "no_match"
    - unsupported expressions => "possible"
    """

    normalized_version = _clean_text(fingerprint_version)
    if normalized_version is None:
        return VersionApplicability(status="possible", explanation="product match; version missing")

    if not versions_json:
        return VersionApplicability(status="possible", explanation="product match; version evidence unavailable")

    unsupported_seen = False
    affected_match_seen = False

    for entry in versions_json:
        if not isinstance(entry, dict):
            unsupported_seen = True
            continue

        status = _normalize_status(entry.get("status"), default_status=default_status)
        if status is None:
            unsupported_seen = True
            continue

        verdict = _evaluate_supported_rule(version=normalized_version, entry=entry, status=status)
        if verdict is None:
            unsupported_seen = True
            continue
        if verdict.status == "no_match" and verdict.explanation == "product match; version exact unaffected":
            return verdict
        if verdict.status == "applicable":
            affected_match_seen = True
            continue

    if affected_match_seen:
        return VersionApplicability(status="applicable", explanation="product match; version affected")

    if unsupported_seen:
        return VersionApplicability(
            status="possible",
            explanation="product match; version rule unsupported by MVP evaluator",
        )

    return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")


def _evaluate_supported_rule(*, version: str, entry: dict[str, Any], status: str) -> VersionApplicability | None:
    version_type = _clean_text(entry.get("versionType"))
    if version_type is not None and version_type.lower() not in {"semver", "custom"}:
        return None

    rule_version = _clean_text(entry.get("version"))
    if rule_version is None:
        return None
    if _is_unsupported_marker(rule_version):
        return None

    less_than = _clean_text(entry.get("lessThan"))
    less_than_or_equal = _clean_text(entry.get("lessThanOrEqual"))
    if less_than and less_than_or_equal:
        return None

    compared_lower = compare_versions(version, rule_version)
    if compared_lower is None:
        return None

    if compared_lower < 0:
        return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")

    changes = _evaluate_changes_rule(version=version, status=status, entry=entry)

    in_range = True
    upper_bound_supported = True
    if less_than is not None:
        upper_cmp = compare_versions(version, less_than)
        if upper_cmp is None:
            upper_bound_supported = False
            in_range = False
        else:
            in_range = upper_cmp < 0
    elif less_than_or_equal is not None:
        upper_cmp = compare_versions(version, less_than_or_equal)
        if upper_cmp is None:
            upper_bound_supported = False
            in_range = False
        else:
            in_range = upper_cmp <= 0
    else:
        in_range = compared_lower == 0

    if not upper_bound_supported:
        if changes is not None:
            return changes
        return None

    if changes is not None and (less_than is not None or less_than_or_equal is not None):
        if in_range:
            return changes
        return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")

    if not in_range:
        if changes is not None:
            return changes
        return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")

    if status == "affected":
        if less_than is None and less_than_or_equal is None:
            return VersionApplicability(status="applicable", explanation="product match; version exact affected")
        return VersionApplicability(status="applicable", explanation="product match; version range affected")

    return VersionApplicability(status="no_match", explanation="product match; version exact unaffected")


def _evaluate_changes_rule(*, version: str, status: str, entry: dict[str, Any]) -> VersionApplicability | None:
    raw_changes = entry.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        return None

    base_version = _clean_text(entry.get("version"))
    if base_version is None:
        return None
    if compare_versions(version, base_version) is None:
        return None
    if compare_versions(version, base_version) < 0:
        return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")

    parsed_changes: list[tuple[str, str]] = []
    for change in raw_changes:
        if not isinstance(change, dict):
            return None
        at = _clean_text(change.get("at"))
        if at is None:
            return None
        if parse_version(at) is None:
            return None
        change_status = _normalize_status(change.get("status"), default_status=status)
        if change_status is None:
            return None
        parsed_changes.append((at, change_status))

    parsed_changes.sort(
        key=functools.cmp_to_key(
            lambda left, right: compare_versions(left[0], right[0]) or 0,
        )
    )
    current_status = status
    for at, change_status in parsed_changes:
        compared = compare_versions(version, at)
        if compared is None:
            return None
        if compared >= 0:
            current_status = change_status
            continue
        break

    if current_status == "affected":
        return VersionApplicability(status="applicable", explanation="product match; version range affected")

    return VersionApplicability(status="no_match", explanation="product match; version outside affected rules")


def _normalize_status(value: Any, *, default_status: str | None) -> str | None:
    status = _clean_text(value) or _clean_text(default_status)
    if status is None:
        return "affected"
    lowered = status.lower()
    if lowered in {"affected", "unaffected"}:
        return lowered
    return None


def _is_unsupported_marker(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"*", "x", "any", "all", "n/a"}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


__all__ = ["VersionApplicability", "VersionApplicabilityStatus", "evaluate_version_applicability"]
