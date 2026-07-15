"""Utility deterministic compression helpers.

This module projects bounded network utility metadata into compact facts.
It is pure adapter code: it does not execute
commands, inspect artifacts, call runtime providers, or expose reusable
credentials, bearer tokens, cookies, or raw secrets in compact output.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any, Optional

from core.prompts.constants import COMPACT_SUMMARY_MAX_CHARS
from runtime_shared.durable_secret_masking import mask_durable_secrets

from .common import (
    as_int,
    compact_evidence_line,
    dedupe_string_list,
    sanitize_artifact_refs,
)
from .contracts import CompressionInput, DeterministicCompressionResult

NETWORK_UTILITY_TOOL_ID = "networking_utilities.network"
MAX_UTILITY_RESULT_STDIO_CHARS = 128 * 1024

_REGISTERED_UTILITY_TOOL_IDS: tuple[str, ...] = (
    NETWORK_UTILITY_TOOL_ID,
)
_ARTIFACT_REF_LIMIT = 3
_EVIDENCE_LIMIT = 5
_SEMANTIC_EVIDENCE_LIMIT = 4
_SEMANTIC_OBSERVATION_LIMIT = 5
_SENSITIVE_FIELD_NAME_PATTERN = (
    r"(?:authorization|bearer|cookies?|password|passwd|pwd|secret|token|api[_-]?key)"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>\b"
    + _SENSITIVE_FIELD_NAME_PATTERN
    + r"\b\s*[:=]\s*)(?P<secret>\S+)"
)
_SENSITIVE_OBJECT_FIELD_RE = re.compile(
    r"(?i)(?P<prefix>(?P<key_quote>['\"])"
    + _SENSITIVE_FIELD_NAME_PATTERN
    + r"(?P=key_quote)\s*:\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)"
)
_BARE_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_REDACTED = "<redacted>"


def utility_adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
    """Project visible utility tool metadata into compact deterministic facts."""

    if input_data.tool_name == NETWORK_UTILITY_TOOL_ID:
        return _adapt_network_utility(input_data)
    return DeterministicCompressionResult.none(fallback_reason="unsupported_utility_tool")


def registered_utility_tool_ids() -> tuple[str, ...]:
    """Return utility tool ids registered for deterministic MVP coverage."""

    return _REGISTERED_UTILITY_TOOL_IDS


def register_utility_adapters() -> None:
    """Register deterministic adapters for visible utility tools."""

    from .registry import register_adapter

    for tool_id in _REGISTERED_UTILITY_TOOL_IDS:
        register_adapter(tool_id, utility_adapter)


def _adapt_network_utility(input_data: CompressionInput) -> DeterministicCompressionResult:
    metadata = _mapping_or_empty(input_data.raw_result.get("metadata"))
    parameters = _mapping_or_empty(input_data.raw_result.get("parameters"))
    operation = _first_text(metadata.get("operation"), parameters.get("operation"))
    target = _network_target(metadata=metadata, parameters=parameters)

    if not operation and not metadata:
        return DeterministicCompressionResult.none(fallback_reason="no_network_utility_metadata")

    outcome = _network_outcome(metadata=metadata, raw_result=input_data.raw_result)
    findings = _network_findings(metadata=metadata, target=target, outcome=outcome)
    findings.extend(_semantic_observation_findings(metadata, input_data.raw_result))
    findings.extend(_artifact_findings(input_data.raw_result, metadata=metadata))
    errors = _bounded_errors(metadata=metadata, raw_result=input_data.raw_result)
    evidence = _utility_evidence(metadata=metadata, raw_result=input_data.raw_result)

    return DeterministicCompressionResult(
        summary=_summary(
            _summary_text(
                subject="Network utility",
                operation=operation or "utility",
                target=target or "local host",
                outcome=outcome,
            )
        ),
        key_findings=tuple(dedupe_string_list(findings, limit=None)),
        errors=tuple(errors),
        structured_signals=tuple(
            _structured_signals(
                tool=input_data.tool_name,
                operation=operation,
                target=target,
                outcome=outcome,
                metadata=metadata,
                raw_result=input_data.raw_result,
                errors=errors,
            )
        ),
        decision_evidence=tuple(evidence),
        completeness="partial",
        lossiness_risk="low",
    )


def _network_findings(
    *,
    metadata: Mapping[str, Any],
    target: Optional[str],
    outcome: str,
) -> list[str]:
    findings: list[str] = [f"outcome: {outcome}"]
    record_type = _first_text(metadata.get("record_type"))
    if record_type:
        findings.append(f"record type: {record_type}")
    line_count = as_int(metadata.get("stdout_line_count"))
    if line_count is not None:
        findings.append(f"stdout lines: {line_count}")
    answer_count = as_int(metadata.get("answer_count"))
    if answer_count is not None:
        findings.append(f"answers: {answer_count}")
    entry_count = as_int(metadata.get("entry_count"))
    if entry_count is not None:
        findings.append(f"entries: {entry_count}")
    if "reachable" in metadata:
        endpoint = target or "target"
        findings.append(f"reachability: {endpoint} {'reachable' if metadata.get('reachable') else 'not reachable'}")
    if metadata.get("timed_out"):
        findings.append("timed out: true")
    return findings


def _summary_text(
    *,
    subject: str,
    operation: str,
    target: str,
    outcome: str,
) -> str:
    return f"{subject} {operation} against {target}: {outcome}."


def _network_outcome(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> str:
    if metadata.get("timed_out") or raw_result.get("status") == "timeout":
        return "timeout"
    if "reachable" in metadata:
        return "reachable" if metadata.get("reachable") else "not_reachable"
    if metadata.get("success") is True or raw_result.get("success") is True:
        return "success"
    if metadata.get("success") is False or raw_result.get("success") is False:
        return "failed"
    return "completed"


def _network_target(
    *,
    metadata: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> Optional[str]:
    target = _first_text(metadata.get("target"), parameters.get("target"))
    port = _first_text(metadata.get("port"), parameters.get("port"))
    if target and port:
        return _mask_text(f"{target}:{port}")
    if target:
        return _mask_text(target)
    operation = _first_text(metadata.get("operation"), parameters.get("operation"))
    if operation in {"local_interfaces", "local_routes", "local_neighbors"}:
        return "local host"
    return None


def _utility_evidence(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    lines: list[str] = []
    for label, value in (
        ("stdout preview", metadata.get("stdout_preview")),
        ("stderr preview", metadata.get("stderr_preview")),
    ):
        preview = _bounded_runner_preview(value)
        if preview:
            lines.append(_masked_evidence_line(label, preview))

    for label, value in (("stdout preview", raw_result.get("stdout")), ("stderr preview", raw_result.get("stderr"))):
        if len(lines) >= _EVIDENCE_LIMIT:
            break
        preview = _bounded_runner_preview(value)
        if preview:
            lines.append(_masked_evidence_line(label, preview))

    lines.extend(_semantic_evidence_lines(metadata, raw_result))
    lines.extend(_semantic_observation_lines(metadata, raw_result))
    return dedupe_string_list(lines, limit=_EVIDENCE_LIMIT)


def _semantic_evidence_lines(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    evidence = metadata.get("semantic_evidence")
    if not isinstance(evidence, list):
        evidence = raw_result.get("semantic_evidence")
    if not isinstance(evidence, list):
        return []

    masked = mask_durable_secrets(evidence, source="utility_deterministic_semantic_evidence")
    lines: list[str] = []
    for entry in masked if isinstance(masked, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = _first_text(entry.get("name"), entry.get("type"))
        if not name:
            continue
        value = _first_text(entry.get("value"))
        line = f"semantic evidence: {name}"
        if value:
            line += f"={value}"
        line = compact_evidence_line(_mask_text(line))
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= _SEMANTIC_EVIDENCE_LIMIT:
            break
    return lines


def _semantic_observation_lines(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    observations = metadata.get("semantic_observations")
    if not isinstance(observations, list):
        observations = raw_result.get("semantic_observations")
    if not isinstance(observations, list):
        return []

    masked = mask_durable_secrets(
        observations,
        source="utility_deterministic_semantic_observations",
    )
    lines: list[str] = []
    for entry in masked if isinstance(masked, list) else []:
        if not isinstance(entry, Mapping):
            continue
        observation_type = _first_text(entry.get("observation_type"))
        if not observation_type:
            continue
        payload = _mapping_or_empty(entry.get("payload"))
        subject_key = _first_text(entry.get("subject_key"))
        line = f"semantic observation: {observation_type}"
        if subject_key:
            line += f" {subject_key}"
        detail = _observation_payload_detail(payload)
        if detail:
            line += f" {detail}"
        line = compact_evidence_line(_mask_text(line))
        if line and line not in lines:
            lines.append(line)
        if len(lines) >= _SEMANTIC_OBSERVATION_LIMIT:
            break
    return lines


def _semantic_observation_findings(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    observations = metadata.get("semantic_observations")
    if not isinstance(observations, list):
        observations = raw_result.get("semantic_observations")
    if not isinstance(observations, list):
        return []

    masked = mask_durable_secrets(
        observations,
        source="utility_deterministic_semantic_observation_findings",
    )
    findings: list[str] = []
    for entry in masked if isinstance(masked, list) else []:
        if not isinstance(entry, Mapping):
            continue
        observation_type = _first_text(entry.get("observation_type"))
        if not observation_type:
            continue
        payload = _mapping_or_empty(entry.get("payload"))
        detail = _observation_payload_detail(payload)
        line = f"service state: {observation_type}"
        if detail:
            line += f" {detail}"
        line = compact_evidence_line(_mask_text(line))
        if line not in findings:
            findings.append(line)
        if len(findings) >= _SEMANTIC_OBSERVATION_LIMIT:
            break
    return findings


def _observation_payload_detail(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    ip = _first_text(payload.get("ip"), payload.get("host"))
    port = _first_text(payload.get("port"))
    protocol = _first_text(payload.get("protocol"))
    service = _first_text(payload.get("service_name"), payload.get("service"))
    outcome = _first_text(payload.get("utility_outcome"), payload.get("state"))
    if ip:
        endpoint = ip
        if protocol and port:
            endpoint = f"{endpoint}/{protocol}/{port}"
        elif port:
            endpoint = f"{endpoint}:{port}"
        parts.append(f"endpoint={endpoint}")
    if service:
        parts.append(f"service={service}")
    if outcome:
        parts.append(f"outcome={outcome}")
    return " ".join(parts)


def _structured_signals(
    *,
    tool: str,
    operation: Optional[str],
    target: Optional[str],
    outcome: str,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    errors: Iterable[str],
) -> list[Mapping[str, Any]]:
    prefix = "network_utility"
    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": f"{prefix}_outcome", "value": outcome},
    ]
    if operation:
        signals.append({"type": "kv_pair", "key": f"{prefix}_operation", "value": operation})
    if target:
        signals.append({"type": "kv_pair", "key": f"{prefix}_target", "value": target})
    if "reachable" in metadata:
        signals.append(
            {
                "type": "service",
                "target": target,
                "state": "reachable" if metadata.get("reachable") else "not_reachable",
            }
        )
    for error in errors:
        signals.append({"type": "error_context", "message": f"Utility failed: {error}"})
    for ref in _artifact_refs(raw_result=raw_result, metadata=metadata):
        signals.append({"type": "kv_pair", "key": f"{prefix}_artifact_ref", "value": ref["path"]})
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


def _bounded_errors(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    candidates: list[Any] = []
    for key in ("error", "error_code", "runner_parse_error"):
        value = metadata.get(key)
        if value and value != "none":
            candidates.append(value)
    if raw_result.get("success") is False:
        candidates.extend(
            value
            for value in (raw_result.get("status"), raw_result.get("stderr"))
            if value is not None
        )
    return [
        _mask_text(compact_evidence_line(value))
        for value in dedupe_string_list(candidates, limit=3)
        if _mask_text(compact_evidence_line(value))
    ]


def _bounded_runner_preview(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:MAX_UTILITY_RESULT_STDIO_CHARS]


def _masked_evidence_line(label: str, preview: str) -> str:
    return compact_evidence_line(_mask_text(compact_evidence_line(f"{label}: {preview}")))


def _summary(value: str) -> str:
    if len(value) <= COMPACT_SUMMARY_MAX_CHARS:
        return value
    return value[: max(COMPACT_SUMMARY_MAX_CHARS - 3, 0)].rstrip() + "..."


def _mask_text(value: str) -> str:
    text = str(mask_durable_secrets(str(value), source="utility_deterministic_adapter"))
    text = _BARE_TOKEN_RE.sub("Bearer " + _REDACTED, text)
    text = _SENSITIVE_OBJECT_FIELD_RE.sub(_redacted_object_field, text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}",
        text,
    )
    return text


def _redacted_object_field(match: re.Match[str]) -> str:
    value = match.group("value")
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return f"{match.group('prefix')}{value[0]}{_REDACTED}{value[0]}"
    return f"{match.group('prefix')}{_REDACTED}"


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = _text_or_none(value)
        if text:
            return _mask_text(text)
    return None


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


register_utility_adapters()
