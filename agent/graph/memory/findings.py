"""Helpers for working-memory findings normalization, merging, and selection.

This module owns the bounded runtime findings surface used to preserve reusable
host and service observations across turns without relying on transcript replay.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlsplit

from agent.tool_runtime.batch.plan_view import primary_tool_call_from_metadata

from .target_resolution import resolve_target_from_working_memory

if TYPE_CHECKING:
    from agent.graph.state import InteractiveState

CAP_AVAILABLE_FINDINGS = 50
DEFAULT_OBSERVED_TTL_SECONDS = 600
DEFAULT_CANDIDATE_TTL_SECONDS = 300
DEFAULT_RELEVANT_FINDINGS_LIMIT = 8

_LIVE_HOST_STATES = {"up", "live", "open"}
_OPEN_PORT_STATES = {"", "open", "up"}


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _extract_target_host_alias(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""

    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(text)
        except Exception:
            return ""
        return _text(parsed.hostname)

    if "/" in text or text.count(":") != 1:
        return ""

    host_part, port_part = text.rsplit(":", 1)
    if not port_part.isdigit():
        return ""
    return _text(host_part)


def _target_aliases(value: Any) -> set[str]:
    text = _text(value)
    if not text:
        return set()

    aliases = {text, text.lower()}
    host_alias = _extract_target_host_alias(text)
    if host_alias:
        aliases.add(host_alias)
        aliases.add(host_alias.lower())
    return {alias for alias in aliases if alias}


def _host_level_target(value: Any) -> str:
    host_alias = _extract_target_host_alias(value)
    if host_alias:
        return host_alias
    return _text(value)


def _now_ts(now_ts: int | None = None) -> int:
    if isinstance(now_ts, int) and now_ts > 0:
        return now_ts
    return int(time.time())


def _clamp_confidence(value: Any, *, assertion_level: str) -> float:
    default = 1.0 if assertion_level == "observed" else 0.5
    try:
        confidence = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, confidence))


def _default_ttl(assertion_level: str) -> int:
    if assertion_level == "candidate":
        return DEFAULT_CANDIDATE_TTL_SECONDS
    return DEFAULT_OBSERVED_TTL_SECONDS


def normalize_available_finding(
    item: Mapping[str, Any] | Any,
    *,
    now_ts: int | None = None,
) -> dict[str, Any] | None:
    """Normalize one available-finding record to the canonical runtime shape."""
    if not isinstance(item, Mapping):
        return None

    kind = _text(item.get("kind"))
    target = _text(item.get("target"))
    subject = _text(item.get("subject"))
    assertion_level = _text(item.get("assertion_level")).lower()
    if assertion_level not in {"observed", "candidate"}:
        assertion_level = "observed"

    if not kind or not target or not subject:
        return None

    details = _as_mapping(item.get("details"))
    seen_at = _now_ts(now_ts)
    try:
        seen_at = max(0, int(item.get("seen_at") or seen_at))
    except Exception:
        seen_at = _now_ts(now_ts)

    try:
        ttl_seconds = int(item.get("ttl_seconds") or 0)
    except Exception:
        ttl_seconds = 0
    if ttl_seconds <= 0:
        ttl_seconds = _default_ttl(assertion_level)

    return {
        "kind": kind,
        "target": target,
        "subject": subject,
        "details": deepcopy(details),
        "assertion_level": assertion_level,
        "confidence": _clamp_confidence(item.get("confidence"), assertion_level=assertion_level),
        "seen_at": seen_at,
        "ttl_seconds": ttl_seconds,
    }


def normalize_available_findings(
    items: Sequence[Mapping[str, Any]] | None,
    *,
    cap: int = CAP_AVAILABLE_FINDINGS,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Normalize and merge a sequence of findings into a bounded canonical list."""
    return merge_available_findings([], items or [], cap=cap, now_ts=now_ts)


def _finding_key(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(item.get("kind")),
        _text(item.get("target")),
        _text(item.get("subject")),
        _text(item.get("assertion_level")).lower(),
    )


def _replacement_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _text(item.get("kind")),
        _text(item.get("target")),
        _text(item.get("subject")),
    )


def merge_available_findings(
    existing: Sequence[Mapping[str, Any]] | None,
    incoming: Sequence[Mapping[str, Any]] | None,
    *,
    cap: int = CAP_AVAILABLE_FINDINGS,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Merge existing and incoming findings using observed-over-candidate precedence."""
    merged: list[dict[str, Any]] = []
    for collection in (existing or [], incoming or []):
        if not isinstance(collection, Sequence) or isinstance(collection, (str, bytes, bytearray)):
            continue
        for source in collection:
            if not isinstance(source, Mapping):
                continue
            normalized = normalize_available_finding(source, now_ts=now_ts)
            if normalized is not None:
                merged.append(normalized)

    index_by_key: dict[tuple[str, str, str, str], int] = {}
    result: list[dict[str, Any]] = []
    for finding in merged:
        dedupe_key = _finding_key(finding)
        replacement_key = _replacement_key(finding)
        current_index = index_by_key.get(dedupe_key)
        if current_index is not None:
            result[current_index] = finding
            continue

        if finding["assertion_level"] == "observed":
            candidate_index = index_by_key.get((*replacement_key, "candidate"))
            if candidate_index is not None:
                result.pop(candidate_index)
                index_by_key = {_finding_key(item): idx for idx, item in enumerate(result)}

        if finding["assertion_level"] == "candidate":
            observed_index = index_by_key.get((*replacement_key, "observed"))
            if observed_index is not None:
                continue

        result.append(finding)
        index_by_key[_finding_key(finding)] = len(result) - 1

    result.sort(key=lambda item: int(item.get("seen_at") or 0), reverse=True)
    return result[:cap]


def _build_port_subject(target: str, port: int, protocol: str) -> str:
    return f"{target}:{port}/{protocol}"


def _port_row_to_findings(
    row: Mapping[str, Any],
    *,
    target_hint: str,
    seen_at: int,
) -> list[dict[str, Any]]:
    target = _text(row.get("ip")) or target_hint
    if not target:
        return []

    try:
        port = int(row.get("port"))
    except Exception:
        return []

    protocol = _text(row.get("protocol")).lower() or "tcp"
    status = _text(row.get("status")).lower()
    if status not in _OPEN_PORT_STATES:
        return []

    subject = _build_port_subject(target, port, protocol)
    findings: list[dict[str, Any]] = [
        {
            "kind": "port_open",
            "target": target,
            "subject": subject,
            "details": {
                "port": port,
                "protocol": protocol,
            },
            "assertion_level": "observed",
            "confidence": 1.0,
            "seen_at": seen_at,
            "ttl_seconds": DEFAULT_OBSERVED_TTL_SECONDS,
        }
    ]

    service = _text(row.get("service"))
    product = _text(row.get("product"))
    version = _text(row.get("version"))
    if service or product or version:
        details: dict[str, Any] = {
            "port": port,
            "protocol": protocol,
        }
        if service:
            details["service"] = service
        if product:
            details["product"] = product
        if version:
            details["version"] = version
        findings.append(
            {
                "kind": "service_detected",
                "target": target,
                "subject": subject,
                "details": details,
                "assertion_level": "observed",
                "confidence": 1.0,
                "seen_at": seen_at,
                "ttl_seconds": DEFAULT_OBSERVED_TTL_SECONDS,
            }
        )

    return findings


def extract_observed_findings(
    tool_metadata: Mapping[str, Any] | None,
    *,
    target_hint: str = "",
    seen_at: int | None = None,
) -> list[dict[str, Any]]:
    """Extract deterministic observed findings from current tool metadata."""
    metadata = _as_mapping(tool_metadata)
    if not metadata:
        return []

    ts = _now_ts(seen_at)
    findings: list[dict[str, Any]] = []
    normalized_target_hint = _host_level_target(target_hint)

    for host_row in _as_list(metadata.get("hosts")):
        host = _as_mapping(host_row)
        host_ip = _text(host.get("ip")) or normalized_target_hint
        host_status = _text(host.get("status")).lower()
        if host_ip and host_status in _LIVE_HOST_STATES:
            findings.append(
                {
                    "kind": "host_up",
                    "target": host_ip,
                    "subject": host_ip,
                    "details": {"status": host_status},
                    "assertion_level": "observed",
                    "confidence": 1.0,
                    "seen_at": ts,
                    "ttl_seconds": DEFAULT_OBSERVED_TTL_SECONDS,
                }
            )
        for port_row in _as_list(host.get("ports")):
            findings.extend(_port_row_to_findings(_as_mapping(port_row), target_hint=host_ip, seen_at=ts))

    host_status = _text(metadata.get("host_status")).lower()
    if normalized_target_hint and host_status in _LIVE_HOST_STATES:
        findings.append(
            {
                "kind": "host_up",
                "target": normalized_target_hint,
                "subject": normalized_target_hint,
                "details": {"status": host_status},
                "assertion_level": "observed",
                "confidence": 1.0,
                "seen_at": ts,
                "ttl_seconds": DEFAULT_OBSERVED_TTL_SECONDS,
            }
        )

    for port_row in _as_list(metadata.get("open_ports")):
        findings.extend(
            _port_row_to_findings(
                _as_mapping(port_row),
                target_hint=normalized_target_hint,
                seen_at=ts,
            )
        )

    return merge_available_findings([], findings, cap=CAP_AVAILABLE_FINDINGS, now_ts=ts)


def project_candidate_observations(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    active_target: str = "",
    seen_at: int | None = None,
) -> list[dict[str, Any]]:
    """Project PTR candidate observations into available-findings shape."""
    ts = _now_ts(seen_at)
    normalized_active_target = _text(active_target)
    projected: list[dict[str, Any]] = []
    for row in rows or []:
        payload = _as_mapping(row)
        kind = _text(payload.get("observation_type"))
        subject = _text(payload.get("subject_key_hint"))
        if not kind or not subject:
            continue

        details: dict[str, Any] = {}
        for key in (
            "attributes",
            "rationale",
            "evidence_refs",
            "vulnerability",
            "vulnerability_confidence",
        ):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                details[key] = deepcopy(value)

        projected.append(
            {
                "kind": kind,
                "target": normalized_active_target or subject,
                "subject": subject,
                "details": details,
                "assertion_level": "candidate",
                "confidence": payload.get("confidence"),
                "seen_at": ts,
                "ttl_seconds": DEFAULT_CANDIDATE_TTL_SECONDS,
            }
        )

    return merge_available_findings([], projected, cap=CAP_AVAILABLE_FINDINGS, now_ts=ts)


def compose_subject_hint(*values: Any) -> str:
    """Compose a single normalized subject-hint string from mixed inputs."""
    parts: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            for item in value.values():
                text = _text(item)
                if text:
                    parts.append(text)
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                text = _text(item)
                if text:
                    parts.append(text)
            continue
        text = _text(value)
        if text:
            parts.append(text)
    return " ".join(parts)


def _state_for_finding(item: Mapping[str, Any], *, now_ts: int) -> str:
    assertion_level = _text(item.get("assertion_level")).lower()
    if assertion_level == "candidate":
        return "candidate"
    try:
        seen_at = int(item.get("seen_at") or 0)
    except Exception:
        seen_at = 0
    try:
        ttl_seconds = int(item.get("ttl_seconds") or 0)
    except Exception:
        ttl_seconds = 0
    if ttl_seconds > 0 and now_ts - seen_at <= ttl_seconds:
        return "fresh"
    return "stale"


def _subject_matches_hint(item: Mapping[str, Any], subject_hint: str) -> bool:
    hint = _text(subject_hint).lower()
    if not hint:
        return False

    details = _as_mapping(item.get("details"))
    haystack = " ".join(
        part.lower()
        for part in (
            _text(item.get("subject")),
            _text(item.get("kind")),
            _text(details.get("service")),
            _text(details.get("product")),
            _text(details.get("version")),
            _text(details.get("rationale")),
        )
        if part
    )
    if hint in haystack:
        return True
    for token in hint.split():
        if len(token) >= 2 and token in haystack:
            return True
    return False


def select_relevant_findings(
    available_findings: Sequence[Mapping[str, Any]] | None,
    *,
    target: str,
    subject_hint: str = "",
    limit: int = DEFAULT_RELEVANT_FINDINGS_LIMIT,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Select and rank findings relevant to one target and subject hint."""
    normalized_target = _text(target)
    if not normalized_target:
        return []

    ts = _now_ts(now_ts)
    candidates: list[dict[str, Any]] = []
    for row in available_findings or []:
        finding = normalize_available_finding(row, now_ts=ts)
        if finding is None or not _targets_overlap(finding["target"], normalized_target):
            continue
        state = _state_for_finding(finding, now_ts=ts)
        finding_with_state = dict(finding)
        finding_with_state["state"] = state
        candidates.append(finding_with_state)

    state_order = {"fresh": 0, "candidate": 1, "stale": 2}
    candidates.sort(
        key=lambda item: (
            0 if _subject_matches_hint(item, subject_hint) else 1,
            state_order.get(_text(item.get("state")).lower(), 3),
            -int(item.get("seen_at") or 0),
        )
    )
    return candidates[: max(0, int(limit))]


def select_relevant_findings_for_prompt(
    *,
    available_findings: Sequence[Mapping[str, Any]],
    target: str | None,
    subject_hint_components: Sequence[Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Shared tail helper for planner/PTR/reflect prompt findings selection."""
    subject_hint = compose_subject_hint(*subject_hint_components)
    return select_relevant_findings(
        list(available_findings or []),
        target=str(target or ""),
        subject_hint=subject_hint,
        limit=limit,
    )


def build_relevant_findings_for_prompt(interactive: "InteractiveState") -> list[dict[str, Any]]:
    """Select target-scoped findings for prompt builders from canonical state."""
    metadata = interactive.facts.safe_metadata
    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return []

    resolved_target = resolve_target_from_working_memory(
        dict(working_memory),
        intent_referent_key="intent:target",
        recent_turn_limit=4,
    )
    if not isinstance(resolved_target, str) or not resolved_target.strip():
        return []

    last_tool_result = metadata.get("last_tool_result") or {}
    tool_params: Mapping[str, Any] = {}
    if isinstance(last_tool_result, Mapping) and isinstance(last_tool_result.get("parameters"), Mapping):
        tool_params = dict(last_tool_result.get("parameters") or {})
    else:
        primary_call = primary_tool_call_from_metadata(metadata)
        if primary_call is not None:
            tool_params = dict(primary_call.parameters)

    tool_intent = metadata.get("tool_intent") or {}
    available_findings = working_memory.get("available_findings")
    return select_relevant_findings_for_prompt(
        available_findings=(
            available_findings if isinstance(available_findings, list) else []
        ),
        target=resolved_target,
        subject_hint_components=(
            interactive.facts.current_goal,
            metadata.get("next_tool_hint"),
            tool_params,
            tool_intent.get("focus") if isinstance(tool_intent, Mapping) else "",
        ),
        limit=8,
    )


def count_known_open_port_findings(
    available_findings: Sequence[Mapping[str, Any]] | None,
    *,
    target: str,
    now_ts: int | None = None,
) -> int:
    """Count fresh observed port_open findings for one target."""
    normalized_target = _text(target)
    if not normalized_target:
        return 0

    ts = _now_ts(now_ts)
    count = 0
    for row in available_findings or []:
        finding = normalize_available_finding(row, now_ts=ts)
        if finding is None:
            continue
        if not _targets_overlap(finding["target"], normalized_target):
            continue
        if finding["kind"] != "port_open":
            continue
        if _state_for_finding(finding, now_ts=ts) != "fresh":
            continue
        count += 1
    return count


def _targets_overlap(left: Any, right: Any) -> bool:
    left_aliases = _target_aliases(left)
    right_aliases = _target_aliases(right)
    if not left_aliases or not right_aliases:
        return False
    return bool(left_aliases.intersection(right_aliases))


def format_relevant_findings(findings: Sequence[Mapping[str, Any]] | None) -> str:
    """Render a compact prompt-facing findings list."""
    rendered: list[str] = []
    for row in findings or []:
        item = _as_mapping(row)
        state = _text(item.get("state")).lower() or _state_for_finding(item, now_ts=_now_ts())
        kind = _text(item.get("kind"))
        subject = _text(item.get("subject"))
        details = _as_mapping(item.get("details"))
        detail_bits: list[str] = []
        for key in ("service", "product", "version"):
            value = _text(details.get(key))
            if value:
                detail_bits.append(f"{key}={value}")
        if not detail_bits and item.get("assertion_level") == "candidate":
            confidence = item.get("confidence")
            if confidence not in (None, ""):
                detail_bits.append(f"confidence={confidence}")
        suffix = f" ({', '.join(detail_bits)})" if detail_bits else ""
        rendered.append(f"- [{state}] {kind} {subject}{suffix}")
    return "\n".join(rendered)


def _format_attributes_block(attributes: Mapping[str, Any]) -> list[str]:
    """Render candidate attribute details as an indented prompt block."""
    lines: list[str] = ["  Attributes:"]
    for key in sorted(attributes.keys()):
        value = attributes.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple, dict)):
            serialized = json.dumps(value, ensure_ascii=True, sort_keys=True)
            lines.append(f"    - {key}: {serialized}")
            continue
        lines.append(f"    - {key}: {_text(value)}")
    if len(lines) == 1:
        return []
    return lines


def _format_evidence_block(evidence_refs: Sequence[Any]) -> list[str]:
    """Render bounded evidence-reference lines for finalizer prompts."""
    refs = [_text(value) for value in evidence_refs if _text(value)]
    if not refs:
        return []
    lines = ["  Evidence:"]
    for ref in refs[:5]:
        lines.append(f"    - {ref}")
    if len(refs) > 5:
        lines.append(f"    - ... ({len(refs) - 5} more)")
    return lines


def format_findings_for_finalizer(findings: Sequence[Mapping[str, Any]] | None) -> str:
    """Render rich finding bullets for finalizer prompts."""
    rendered: list[str] = []
    for row in findings or []:
        item = _as_mapping(row)
        details = _as_mapping(item.get("details"))
        assertion_level = _text(item.get("assertion_level")).lower() or "observed"
        confidence = item.get("confidence")
        confidence_suffix = (
            f" confidence={confidence}"
            if confidence not in (None, "")
            else ""
        )
        kind = _text(item.get("kind")) or "finding"
        subject = _text(item.get("subject")) or "(unknown subject)"
        target = _text(item.get("target"))
        target_suffix = f" (target={target})" if target else ""
        rendered.append(f"- [{assertion_level}{confidence_suffix}] {kind} @ {subject}{target_suffix}")

        attributes = _as_mapping(details.get("attributes"))
        rendered.extend(_format_attributes_block(attributes))

        rationale = _text(details.get("rationale"))
        if rationale:
            rendered.append(f"  Rationale: {rationale}")

        evidence_refs = details.get("evidence_refs")
        if isinstance(evidence_refs, Sequence) and not isinstance(
            evidence_refs, (str, bytes, bytearray)
        ):
            rendered.extend(_format_evidence_block(evidence_refs))

        vulnerability = _text(details.get("vulnerability"))
        if vulnerability:
            vulnerability_confidence = details.get("vulnerability_confidence")
            suffix = (
                f" (confidence={vulnerability_confidence})"
                if vulnerability_confidence not in (None, "")
                else ""
            )
            rendered.append(f"  Vulnerability hypothesis: {vulnerability}{suffix}")
    return "\n".join(rendered)


__all__ = [
    "CAP_AVAILABLE_FINDINGS",
    "DEFAULT_CANDIDATE_TTL_SECONDS",
    "DEFAULT_OBSERVED_TTL_SECONDS",
    "DEFAULT_RELEVANT_FINDINGS_LIMIT",
    "build_relevant_findings_for_prompt",
    "compose_subject_hint",
    "count_known_open_port_findings",
    "extract_observed_findings",
    "format_findings_for_finalizer",
    "format_relevant_findings",
    "merge_available_findings",
    "normalize_available_finding",
    "normalize_available_findings",
    "project_candidate_observations",
    "select_relevant_findings",
    "select_relevant_findings_for_prompt",
]
