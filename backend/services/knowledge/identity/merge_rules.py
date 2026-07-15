"""Deterministic merge helpers for identity resolution.

This module centralizes reusable merge logic for identity decisions:
- first/last seen timestamp updates
- corroboration-gated confidence updates
- evidence reference accumulation without duplicates
- contradiction capture for projector-visible metadata"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from ..evidence_refs import normalize_canonical_evidence_refs


_CONFIDENCE_RANK: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "confirmed": 4,
}


def normalize_confidence(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric < 0.0:
            numeric = 0.0
        if numeric > 1.0:
            numeric = 1.0
        if numeric >= 0.85:
            return "high"
        if numeric >= 0.50:
            return "medium"
        return "low"
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return raw if raw in _CONFIDENCE_RANK else None


def merge_confidence(current: str | None, incoming: str | None) -> str | None:
    current_norm = normalize_confidence(current)
    incoming_norm = normalize_confidence(incoming)
    if current_norm is None:
        return incoming_norm
    if incoming_norm is None:
        return current_norm
    return incoming_norm if _CONFIDENCE_RANK[incoming_norm] > _CONFIDENCE_RANK[current_norm] else current_norm


def merge_confidence_with_corroboration(
    *,
    current: str | None,
    incoming: str | None,
    is_corroborated: bool,
) -> str | None:
    """Advance confidence only when corroboration exists."""
    current_norm = normalize_confidence(current)
    incoming_norm = normalize_confidence(incoming)
    if current_norm is None:
        return incoming_norm
    if incoming_norm is None:
        return current_norm
    if _CONFIDENCE_RANK[incoming_norm] <= _CONFIDENCE_RANK[current_norm]:
        return current_norm
    if not is_corroborated:
        return current_norm
    return incoming_norm


def merge_seen_timestamps(
    *,
    first_seen_at: datetime | None,
    last_seen_at: datetime | None,
    observed_at: datetime,
) -> tuple[datetime, datetime]:
    first = observed_at if first_seen_at is None or observed_at < first_seen_at else first_seen_at
    last = observed_at if last_seen_at is None or observed_at > last_seen_at else last_seen_at
    return first, last


def merge_evidence_refs(
    *,
    existing: Iterable[Mapping[str, Any]] | None,
    incoming: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def _append(rows: Iterable[Mapping[str, Any]] | None) -> None:
        if rows is None:
            return
        for compact in normalize_canonical_evidence_refs(list(rows), strict=False):
            marker = (compact["evidence_archive_id"], compact.get("excerpt"))
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(compact)

    _append(existing)
    _append(incoming)
    return merged


def derive_identity_state(
    *,
    observation_type: str,
    subject_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build deterministic state fragments for contradiction tracking."""
    state: dict[str, Any] = {}
    obs = str(observation_type or "").strip().lower()
    subj = str(subject_type or "").strip().lower()

    if subj == "service.socket":
        for key in (
            "service_name",
            "product",
            "product_hint",
            "version",
            "version_raw",
            "version_relation",
            "status",
        ):
            value = payload.get(key)
            if value is not None and str(value).strip() != "":
                state[key] = str(value).strip().lower()
        # Rich service profile fields from network.service_profiled
        if obs == "network.service_profiled":
            for key in ("http_title", "server_header"):
                value = payload.get(key)
                if value is not None and str(value).strip():
                    state[key] = str(value).strip()
            script_summaries = payload.get("script_summaries")
            if isinstance(script_summaries, list) and script_summaries:
                state["script_summaries"] = script_summaries

    if subj in {"host.ip", "host.dns"}:
        host_status = payload.get("host_status", payload.get("status"))
        if host_status is not None and str(host_status).strip():
            state["host_status"] = str(host_status).strip().lower()
        # Rich host profile fields from network.host_profiled
        if obs == "network.host_profiled":
            for key in ("os_top_guess",):
                value = payload.get(key)
                if value is not None and str(value).strip():
                    state[key] = str(value).strip()
            hostnames = payload.get("hostnames")
            if isinstance(hostnames, list) and hostnames:
                state["hostnames"] = hostnames
            os_matches = payload.get("os_matches")
            if isinstance(os_matches, list) and os_matches:
                state["os_matches"] = os_matches
            host_scripts = payload.get("host_script_summaries")
            if isinstance(host_scripts, list) and host_scripts:
                state["host_script_summaries"] = host_scripts
            trace_summary = payload.get("trace_summary")
            if isinstance(trace_summary, dict) and trace_summary:
                state["trace_summary"] = trace_summary

    if obs.startswith("finding."):
        if obs in {"finding.vulnerability_absent", "finding.vulnerability_not_detected"}:
            state["finding_presence"] = "absent"
        else:
            state["finding_presence"] = "present"
        for key in ("detector_id", "script_id", "summary"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                state[key] = str(value).strip()
        # Rich nuclei finding stable state fields
        for key in ("title", "description_summary", "matched_at", "matcher_id"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                state[key] = str(value).strip()

    if obs.startswith("relationship."):
        rel_type = payload.get("relationship_type")
        if rel_type is not None and str(rel_type).strip():
            state["relationship_type"] = str(rel_type).strip().lower()

    return state


def derive_rich_finding_details(
    *,
    observation_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Extract additive rich finding detail from payload for finding observations.

    Returns a bounded dict of classification, tags, references, and extracted
    results suitable for merging into finding metadata outside the
    contradiction-tracked state. Returns None when no rich detail is present.
    """
    obs = str(observation_type or "").strip().lower()
    if not obs.startswith("finding."):
        return None

    details: dict[str, Any] = {}

    classification = payload.get("classification")
    if isinstance(classification, Mapping):
        normalized: dict[str, list[str]] = {}
        for key in ("cve_ids", "cwe_ids"):
            ids = classification.get(key)
            if isinstance(ids, list) and ids:
                normalized[key] = ids
        if normalized:
            details["classification"] = normalized

    tags = payload.get("tags")
    if isinstance(tags, list) and tags:
        details["tags"] = tags

    references = payload.get("references")
    if isinstance(references, list) and references:
        details["references"] = references

    extracted_results = payload.get("extracted_results")
    if isinstance(extracted_results, list) and extracted_results:
        details["extracted_results"] = extracted_results

    return details if details else None


def merge_rich_details(
    *,
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge additive rich finding details, preferring latest incoming values.

    This is a simple last-writer-wins merge for bounded rich detail fields
    that are not contradiction-tracked. Returns None when both inputs are empty.
    """
    if not existing and not incoming:
        return None
    merged = dict(existing or {})
    for key, value in dict(incoming or {}).items():
        if value is not None:
            merged[key] = value
    return merged if merged else None


def merge_state_with_contradictions(
    *,
    existing_state: Mapping[str, Any] | None,
    incoming_state: Mapping[str, Any] | None,
    observed_at: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Merge state and capture contradictions instead of silently overwriting."""
    merged = dict(existing_state or {})
    contradictions: list[dict[str, Any]] = []
    for key, raw_value in dict(incoming_state or {}).items():
        if raw_value is None or str(raw_value).strip() == "":
            continue
        value = raw_value
        if key not in merged:
            merged[key] = value
            continue
        current = merged.get(key)
        if current == value:
            continue
        contradictions.append(
            {
                "field": str(key),
                "previous": current,
                "incoming": value,
                "observed_at": observed_at.isoformat(),
            }
        )
        merged[key] = value
    return merged, contradictions
