"""Web-discovery deterministic compression helpers.

This module projects parsed ffuf crawler metadata into compact endpoint
summaries. It is pure adapter code: it does not execute ffuf, read workspace
files, or expand coverage beyond the visible crawler tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS

from agent.tools.web_applications._ffuf_common import (
    parse_ffuf_json_text,
    parse_ffuf_text,
)
from agent.tools.web_applications._ffuf_semantics import (
    build_ffuf_semantic_evidence,
    build_ffuf_semantic_observations,
    detect_ffuf_variant,
)
from agent.tools.web_applications.web_discovery_analysis import (
    WebDiscoveryGroup,
    analyze_ffuf_web_discovery,
)

from .common import (
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)
from .contracts import CompressionInput, DeterministicCompressionResult

FFUF_CRAWLER_TOOL_ID = "web_applications.web_crawlers.ffuf"

_REGISTERED_WEB_DISCOVERY_TOOL_IDS: tuple[str, ...] = (FFUF_CRAWLER_TOOL_ID,)
MAX_FFUF_GROUPS = 12
MAX_FFUF_EXAMPLES_PER_GROUP = 5
MAX_FFUF_DECISION_EVIDENCE_GROUPS = 8
MAX_FFUF_INPUT_RANGES_PER_GROUP = 8
_ARTIFACT_REF_LIMIT = 3
_STATUS_DISTRIBUTION_LIMIT = 8
_SEMANTIC_EVIDENCE_LIMIT = 4
_SEMANTIC_OBSERVATION_LIMIT = 4
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "code",
        "credential",
        "key",
        "password",
        "secret",
        "signature",
        "sig",
        "token",
    }
)


def web_discovery_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project parsed ffuf crawler metadata into compact endpoint facts."""

    if input_data.tool_name != FFUF_CRAWLER_TOOL_ID:
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_web_discovery_tool",
        )

    parameters = _mapping_or_empty(input_data.raw_result.get("parameters"))
    metadata = _ffuf_metadata(
        input_data.raw_result,
        target_template=_first_text(parameters.get("target")),
    )
    if not metadata and not parameters:
        return DeterministicCompressionResult.none(fallback_reason="no_ffuf_metadata")

    metadata = dict(metadata)
    metadata.setdefault("ffuf_variant", "crawler")
    args = SimpleNamespace(**dict(parameters))
    target = _target(metadata=metadata, parameters=parameters)

    error = _ffuf_error(metadata=metadata, raw_result=input_data.raw_result)
    if error:
        compact_error = compact_evidence_line(error)
        summary_error = compact_error.rstrip(".")
        summary = _summary(
            _with_target(
                "ffuf crawler discovered 0 endpoints",
                target=target,
            )
            + f"; error: {summary_error}."
        )
        return DeterministicCompressionResult(
            summary=summary,
            errors=(compact_error,),
            structured_signals=(
                {
                    "type": "error_context",
                    "message": f"ffuf crawler failed: {compact_error}",
                },
            ),
            completeness="partial",
            lossiness_risk="low",
        )

    if detect_ffuf_variant(metadata) != "crawler":
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_ffuf_variant",
        )

    analysis = analyze_ffuf_web_discovery(
        metadata.get("results"),
        source_tool=FFUF_CRAWLER_TOOL_ID,
        target_template=target,
        max_examples_per_group=MAX_FFUF_EXAMPLES_PER_GROUP,
        max_input_ranges_per_group=MAX_FFUF_INPUT_RANGES_PER_GROUP,
        safe_url=_safe_url,
    )
    shown_groups = analysis.groups[:MAX_FFUF_GROUPS]
    endpoint_count = len(analysis.records)
    group_count = len(analysis.groups)
    status_distribution = _merged_status_distribution(
        metadata,
        analysis.status_distribution,
    )
    truncation_metadata = _truncation_metadata(
        metadata=metadata,
        raw_result=input_data.raw_result,
    )

    findings: list[str] = []
    findings.extend(_format_ffuf_group(group) for group in shown_groups)
    if endpoint_count == 0:
        findings.append("ffuf crawler returned no discovered endpoints.")
    elif group_count > 0:
        findings.append(
            _grouping_summary_line(
                endpoint_count=endpoint_count,
                group_count=group_count,
                shown_group_count=len(shown_groups),
                has_artifact=bool(
                    _artifact_refs(raw_result=input_data.raw_result, metadata=metadata)
                ),
            )
        )

    distribution_line = _status_distribution_line(status_distribution)
    if distribution_line:
        findings.append(distribution_line)
    truncation_lines = _truncation_lines(truncation_metadata)
    findings.extend(truncation_lines)
    findings.extend(_artifact_findings(input_data.raw_result, metadata=metadata))

    semantic_evidence = _semantic_evidence_lines(metadata, args, input_data.raw_result)
    semantic_observations = _semantic_observation_lines(metadata, args, input_data.raw_result)
    decision_evidence = tuple(
        compact_evidence_line(value)
        for value in (
            [
                _format_ffuf_group(group, prefix="ffuf group:")
                for group in shown_groups[:MAX_FFUF_DECISION_EVIDENCE_GROUPS]
            ]
            + semantic_evidence[:_SEMANTIC_EVIDENCE_LIMIT]
            + semantic_observations[:_SEMANTIC_OBSERVATION_LIMIT]
        )
        if value
    )

    return DeterministicCompressionResult(
        summary=_summary(
            _success_summary(
                endpoint_count=endpoint_count,
                group_count=group_count,
                target=target,
            )
        ),
        key_findings=tuple(dedupe_string_list(findings, limit=None)),
        structured_signals=tuple(
            _structured_signals(
                target=target,
                endpoint_count=endpoint_count,
                group_count=group_count,
                status_distribution=status_distribution,
                truncation_metadata=truncation_metadata,
                raw_result=input_data.raw_result,
                metadata=metadata,
            )
        ),
        decision_evidence=decision_evidence,
        completeness="partial",
        lossiness_risk="low",
    )


def registered_web_discovery_tool_ids() -> tuple[str, ...]:
    """Return web-discovery tool ids registered for deterministic MVP coverage."""

    return _REGISTERED_WEB_DISCOVERY_TOOL_IDS


def register_web_discovery_adapters() -> None:
    """Register deterministic web-discovery adapters for visible crawler tools."""

    from .registry import register_adapter

    register_adapter(FFUF_CRAWLER_TOOL_ID, web_discovery_adapter)


def _ffuf_metadata(
    raw_result: Mapping[str, Any],
    *,
    target_template: Optional[str] = None,
) -> Mapping[str, Any]:
    """Return parsed ffuf metadata from tool metadata or stdout."""

    metadata = raw_result.get("metadata")
    if isinstance(metadata, Mapping):
        nested_ffuf = metadata.get("ffuf")
        if isinstance(nested_ffuf, Mapping):
            return nested_ffuf
        if _looks_like_ffuf_metadata(metadata):
            return metadata

    stdout = str(raw_result.get("stdout") or "")
    stripped = stdout.strip()
    if not stripped:
        return {}
    return (
        parse_ffuf_json_text(stripped)
        if stripped.startswith("{") or stripped.startswith("[")
        else parse_ffuf_text(stdout, target_template=target_template)
    )


def _looks_like_ffuf_metadata(metadata: Mapping[str, Any]) -> bool:
    return any(
        key in metadata
        for key in (
            "results",
            "config",
            "commandline",
            "ffuf_variant",
            "timeout",
            "error",
            "status_distribution",
            "status_counts",
        )
    )


def _ffuf_error(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> Optional[str]:
    error = _text_or_none(metadata.get("error"))
    if error:
        return error

    timeout = metadata.get("timeout")
    if isinstance(timeout, Mapping):
        return _first_text(timeout.get("message"), "timeout")
    if timeout:
        return "timeout"

    success = raw_result.get("success")
    status = _text_or_none(raw_result.get("status"))
    if success is False or status in {"error", "failed", "timeout"}:
        return _first_text(
            raw_result.get("stderr"),
            metadata.get("stderr"),
            status,
            "ffuf command failed",
        )
    return None


def _target(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> Optional[str]:
    config = _mapping_or_empty(metadata.get("config"))
    return _safe_url(
        _first_text(config.get("url"), config.get("target"), parameters.get("target"))
    )


def _with_target(prefix: str, *, target: Optional[str]) -> str:
    if target:
        return f"{prefix} for {target}"
    return prefix


def _format_ffuf_group(group: WebDiscoveryGroup, *, prefix: str = "group") -> str:
    parts = [prefix, f"count={group.count}"]
    for label, value in (
        ("status", group.status_code),
        ("size", group.response_size),
        ("words", group.words),
        ("lines", group.lines),
    ):
        if value is not None:
            parts.append(f"{label}={value}")
    if group.redirect:
        parts.append(f"redirect={group.redirect}")
    if group.content_type:
        parts.append(f"content_type={group.content_type}")

    input_summary = _format_group_input_summary(group)
    if input_summary:
        parts.append(input_summary)
    if group.examples:
        parts.append(f"examples={','.join(group.examples)}")
    return compact_evidence_line(" ".join(parts))


def _format_group_input_summary(group: WebDiscoveryGroup) -> Optional[str]:
    if group.input_ranges:
        return f"inputs={','.join(group.input_ranges)}"
    if group.input_examples:
        return f"inputs={','.join(group.input_examples)}"
    return None


def _success_summary(
    *,
    endpoint_count: int,
    group_count: int,
    target: Optional[str],
) -> str:
    summary = _with_target(
        f"ffuf crawler discovered {endpoint_count} endpoints",
        target=target,
    )
    if endpoint_count > 0:
        summary += f"; grouped into {group_count} response fingerprints"
    return summary + "."


def _grouping_summary_line(
    *,
    endpoint_count: int,
    group_count: int,
    shown_group_count: int,
    has_artifact: bool,
) -> str:
    if shown_group_count < group_count:
        suffix = (
            "full results in artifact"
            if has_artifact
            else "full results omitted from compact output"
        )
        return (
            f"grouped {endpoint_count} results into {group_count} response fingerprints; "
            f"showing {shown_group_count} of {group_count} groups; {suffix}."
        )
    return (
        f"grouped {endpoint_count} results into {group_count} response fingerprints; "
        f"showing {shown_group_count} groups."
    )


def _merged_status_distribution(
    metadata: Mapping[str, Any],
    computed_distribution: Mapping[str, int],
) -> Mapping[str, int]:
    metadata_distribution = _metadata_status_distribution(metadata)
    return metadata_distribution or computed_distribution


def _metadata_status_distribution(metadata: Mapping[str, Any]) -> dict[str, int]:
    value = _first_mapping(
        metadata.get("status_distribution"),
        metadata.get("status_counts"),
        metadata.get("status_code_counts"),
        metadata.get("http_status_counts"),
    )
    if not value:
        return {}

    distribution: dict[str, int] = {}
    for key, raw_count in value.items():
        status = _text_or_none(key)
        count = as_int(raw_count)
        if status and count is not None:
            distribution[status] = count
    return dict(sorted(distribution.items(), key=lambda item: (item[0])))


def _status_distribution_line(distribution: Mapping[str, int]) -> Optional[str]:
    if not distribution:
        return None
    parts = [
        f"{status}={count}"
        for status, count in list(distribution.items())[:_STATUS_DISTRIBUTION_LIMIT]
    ]
    if len(distribution) > _STATUS_DISTRIBUTION_LIMIT:
        parts.append(f"+{len(distribution) - _STATUS_DISTRIBUTION_LIMIT} more")
    return f"status distribution: {', '.join(parts)}"


def _truncation_metadata(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> dict[str, Any]:
    candidates: dict[str, Any] = {}
    for source in (metadata, raw_result):
        for key in (
            "results_truncated",
            "truncated",
            "was_truncated",
            "stdout_truncated",
            "stderr_truncated",
            "chars_truncated",
            "results_total",
            "total_results",
            "omitted_results",
            "original_result_count",
        ):
            value = source.get(key)
            if value in (None, "", []):
                continue
            candidates[key] = value
    return candidates


def _truncation_lines(metadata: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in metadata.items():
        if isinstance(value, bool):
            value_text = "true" if value else "false"
        else:
            value_text = str(value)
        lines.append(compact_evidence_line(f"{key}: {value_text}"))
    return lines


def _semantic_evidence_lines(
    metadata: Mapping[str, Any],
    args: SimpleNamespace,
    raw_result: Mapping[str, Any],
) -> list[str]:
    evidence = metadata.get("semantic_evidence")
    if not isinstance(evidence, list):
        evidence = raw_result.get("semantic_evidence")
    if not isinstance(evidence, list):
        evidence = build_ffuf_semantic_evidence(metadata, args)

    lines: list[str] = []
    for entry in evidence:
        if not isinstance(entry, Mapping):
            continue
        name = _text_or_none(entry.get("name"))
        if not name:
            continue
        value = entry.get("value")
        value_text = _text_or_none(value)
        if value_text is None and value is not None:
            value_text = str(value)
        line = f"semantic evidence: {name}"
        if value_text:
            line += f"={value_text}"
        if line not in lines:
            lines.append(compact_evidence_line(line))
        if len(lines) >= _SEMANTIC_EVIDENCE_LIMIT:
            break
    return lines


def _semantic_observation_lines(
    metadata: Mapping[str, Any],
    args: SimpleNamespace,
    raw_result: Mapping[str, Any],
) -> list[str]:
    observations = metadata.get("semantic_observations")
    if not isinstance(observations, list):
        observations = raw_result.get("semantic_observations")
    if not isinstance(observations, list):
        observations = build_ffuf_semantic_observations(metadata, args)

    lines: list[str] = []
    for entry in observations:
        if not isinstance(entry, Mapping):
            continue
        observation_type = _text_or_none(entry.get("observation_type"))
        if not observation_type:
            continue
        subject_key = _text_or_none(entry.get("subject_key"))
        line = f"semantic observation: {observation_type}"
        if subject_key:
            line += f" {subject_key}"
        if line not in lines:
            lines.append(compact_evidence_line(line))
        if len(lines) >= _SEMANTIC_OBSERVATION_LIMIT:
            break
    return lines


def _structured_signals(
    *,
    target: Optional[str],
    endpoint_count: int,
    group_count: int,
    status_distribution: Mapping[str, int],
    truncation_metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": "ffuf_endpoint_count", "value": endpoint_count},
        {"type": "kv_pair", "key": "ffuf_group_count", "value": group_count},
    ]
    if target:
        signals.append({"type": "kv_pair", "key": "ffuf_target", "value": target})
    if status_distribution:
        signals.append(
            {
                "type": "kv_pair",
                "key": "ffuf_status_distribution",
                "value": ",".join(
                    f"{status}={count}" for status, count in status_distribution.items()
                ),
            }
        )
    for key, value in truncation_metadata.items():
        signals.append(
            {
                "type": "kv_pair",
                "key": f"ffuf_{key}",
                "value": value,
            }
        )
    for ref in _artifact_refs(raw_result=raw_result, metadata=metadata):
        signals.append(
            {
                "type": "kv_pair",
                "key": "ffuf_artifact_ref",
                "value": ref["path"],
            }
        )
    return signals


def _artifact_findings(
    raw_result: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> list[str]:
    return [
        f"artifact: {ref['path']}"
        for ref in _artifact_refs(raw_result=raw_result, metadata=metadata)
    ]


def _artifact_refs(
    *,
    raw_result: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> list[dict[str, str]]:
    candidates: list[Mapping[str, Any]] = []
    for source in (raw_result.get("artifacts"), metadata.get("artifacts")):
        if not isinstance(source, list):
            continue
        for artifact in source:
            if isinstance(artifact, Mapping):
                candidates.append(artifact)
            elif isinstance(artifact, str):
                candidates.append({"path": artifact})
    return sanitize_artifact_refs(candidates)[:_ARTIFACT_REF_LIMIT]


def _safe_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return compact_evidence_line(value)
    netloc = parsed.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[1]
        netloc = f"<REDACTED>@{host}"
    query_pairs = []
    for key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        query_pairs.append(
            (
                key,
                "<REDACTED>"
                if key.lower() in _SENSITIVE_QUERY_KEYS
                else raw_value,
            )
        )
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            urlencode(query_pairs, doseq=True),
            parsed.fragment,
        )
    )


def _first_mapping(*values: Any) -> Mapping[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return value
    return {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _text_or_none(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _summary(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= COMPACT_SUMMARY_MAX_CHARS:
        return text
    return text[: max(COMPACT_SUMMARY_MAX_CHARS - 3, 0)].rstrip() + "..."


register_web_discovery_adapters()
