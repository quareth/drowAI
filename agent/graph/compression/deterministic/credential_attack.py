"""Credential-attack deterministic compression helpers.

This module projects Hydra-authored metadata into compact credential-attack
facts. It is pure adapter code: it does not execute Hydra, read workspace
files, or expose reusable passwords in summaries, findings, evidence, or
structured signals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
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

HYDRA_TOOL_ID = "password_attacks.online_attacks.hydra"

_REGISTERED_CREDENTIAL_ATTACK_TOOL_IDS: tuple[str, ...] = (HYDRA_TOOL_ID,)
_CREDENTIAL_LIMIT = 5
_EVIDENCE_LIMIT = 5
_ERROR_LIMIT = 3
_ARTIFACT_REF_LIMIT = 3
_SEMANTIC_EVIDENCE_LIMIT = 4
_SEMANTIC_OBSERVATION_LIMIT = 4
_REDACTED_PASSWORD = "<redacted>"
_PASSWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>\bpass(?:word|wd)?\s*[:=]\s*)(?P<secret>\S+)"
)
_LOCKOUT_TOKENS = (
    "account locked",
    "account lockout",
    "authentication failure",
    "blocked",
    "lockout",
    "max authentication tries",
    "rate limit",
    "throttle",
    "too many connections",
    "too many failures",
)
_TIMEOUT_TOKENS = ("timeout", "timed out")


def credential_attack_adapter(
    input_data: CompressionInput,
) -> DeterministicCompressionResult:
    """Project parsed Hydra metadata into compact credential-attack facts."""

    if input_data.tool_name != HYDRA_TOOL_ID:
        return DeterministicCompressionResult.none(
            fallback_reason="unsupported_credential_attack_tool",
        )

    metadata = _hydra_metadata(input_data.raw_result)
    if not metadata:
        return DeterministicCompressionResult.none(fallback_reason="no_hydra_metadata")

    context = _context(metadata=metadata, raw_result=input_data.raw_result)
    credentials = _credential_rows(metadata.get("credentials"))
    success_count = _success_count(metadata, credentials=credentials)
    outcome = _outcome(
        metadata=metadata,
        raw_result=input_data.raw_result,
        success_count=success_count,
    )
    errors = _bounded_errors(metadata=metadata, raw_result=input_data.raw_result)
    lockout_lines = _lockout_findings(metadata=metadata, raw_result=input_data.raw_result)

    findings: list[str] = []
    findings.append(_success_count_line(success_count, credentials=credentials))
    findings.extend(_credential_findings(credentials, context=context))

    stats_line = _statistics_line(metadata.get("statistics"))
    if stats_line:
        findings.append(stats_line)
    findings.extend(lockout_lines)
    findings.extend(_artifact_findings(input_data.raw_result, metadata=metadata))

    if success_count == 0 and not errors and not lockout_lines:
        findings.append("Hydra reported no valid credentials.")
    if errors:
        findings.extend(f"error: {error}" for error in errors)

    proof_lines = _credential_proof_lines(credentials, context=context)
    if not proof_lines and outcome in {"timeout", "lockout", "error"}:
        proof_lines = [_outcome_proof_line(outcome=outcome, context=context)]

    semantic_evidence = _semantic_evidence_lines(metadata, input_data.raw_result)
    semantic_observations = _semantic_observation_lines(metadata, input_data.raw_result)
    decision_evidence = tuple(
        compact_evidence_line(value)
        for value in (
            proof_lines[:_EVIDENCE_LIMIT]
            + semantic_evidence[:_SEMANTIC_EVIDENCE_LIMIT]
            + semantic_observations[:_SEMANTIC_OBSERVATION_LIMIT]
        )
        if value
    )

    return DeterministicCompressionResult(
        summary=_summary(
            _summary_text(
                context=context,
                outcome=outcome,
                success_count=success_count,
            )
        ),
        key_findings=tuple(dedupe_string_list(findings, limit=None)),
        errors=tuple(errors),
        structured_signals=tuple(
            _structured_signals(
                context=context,
                outcome=outcome,
                success_count=success_count,
                metadata=metadata,
                raw_result=input_data.raw_result,
                errors=errors,
            )
        ),
        decision_evidence=decision_evidence,
        completeness="complete",
        lossiness_risk="low",
    )


def registered_credential_attack_tool_ids() -> tuple[str, ...]:
    """Return credential-attack tool ids registered for deterministic coverage."""

    return _REGISTERED_CREDENTIAL_ATTACK_TOOL_IDS


def register_credential_attack_adapters() -> None:
    """Register deterministic credential-attack adapters for visible tools."""

    from .registry import register_adapter

    register_adapter(HYDRA_TOOL_ID, credential_attack_adapter)


def _hydra_metadata(raw_result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return parsed Hydra metadata from runtime metadata or stdout/stderr."""

    metadata = raw_result.get("metadata")
    if isinstance(metadata, Mapping):
        nested_hydra = metadata.get("hydra")
        if isinstance(nested_hydra, Mapping):
            return nested_hydra
        tool_metadata = metadata.get("tool_metadata")
        if isinstance(tool_metadata, Mapping) and _looks_like_hydra_metadata(tool_metadata):
            return tool_metadata
        if _looks_like_hydra_metadata(metadata):
            return metadata

    stdout = str(raw_result.get("stdout") or "")
    stderr = str(raw_result.get("stderr") or "")
    if not stdout.strip() and not stderr.strip():
        return {}
    return _parse_hydra_output(stdout, stderr)


def _parse_hydra_output(stdout: str, stderr: str) -> Mapping[str, Any]:
    """Parse Hydra text output via the tool-owned parser on demand."""

    from agent.tools.password_attacks.online_attacks.hydra import parse_hydra_output

    return parse_hydra_output(stdout, stderr)


def _looks_like_hydra_metadata(metadata: Mapping[str, Any]) -> bool:
    return any(
        key in metadata
        for key in (
            "attack_info",
            "capability_family",
            "credentials",
            "errors",
            "semantic_schema_version",
            "statistics",
            "successful_logins",
            "target_info",
        )
    )


def _context(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> dict[str, Optional[str]]:
    attack_info = _mapping_or_empty(metadata.get("attack_info"))
    target_info = _mapping_or_empty(metadata.get("target_info"))
    parameters = _mapping_or_empty(raw_result.get("parameters"))

    service = _first_text(
        attack_info.get("service"),
        attack_info.get("protocol"),
        metadata.get("service_type"),
        metadata.get("protocol"),
        parameters.get("service_type"),
        parameters.get("protocol"),
    )
    host = _first_text(
        target_info.get("host"),
        parameters.get("target"),
        parameters.get("host"),
    )
    port = _first_text(target_info.get("port"), parameters.get("port"))
    return {"service": service, "host": host, "port": port}


def _credential_rows(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    credentials: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        username = _first_text(row.get("account_identifier"), row.get("username"), row.get("login"))
        host = _first_text(row.get("host"))
        port = _first_text(row.get("port"))
        service = _first_text(row.get("service"), row.get("protocol"))
        credentials.append(
            {
                "username": username,
                "host": host,
                "port": port,
                "service": service,
                "password_present": bool(row.get("password_present") or row.get("password")),
                "source_format": _first_text(row.get("source_format")),
            }
        )
    return credentials


def _success_count(
    metadata: Mapping[str, Any],
    *,
    credentials: list[dict[str, Any]],
) -> int:
    statistics = _mapping_or_empty(metadata.get("statistics"))
    for value in (
        metadata.get("success_count"),
        statistics.get("successful_login_count"),
        statistics.get("valid_passwords"),
        statistics.get("valid_pairs"),
    ):
        count = as_int(value)
        if count is not None:
            return count
    return len(credentials)


def _outcome(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    success_count: int,
) -> str:
    if success_count > 0:
        return "confirmed"

    status = _first_text(raw_result.get("status"), metadata.get("status"))
    exit_code = as_int(_first_value(raw_result.get("exit_code"), metadata.get("exit_code")))
    messages = " ".join(_message_candidates(metadata=metadata, raw_result=raw_result)).lower()
    if status == "timeout" or exit_code == -2 or any(token in messages for token in _TIMEOUT_TOKENS):
        return "timeout"
    if any(token in messages for token in _LOCKOUT_TOKENS):
        return "lockout"
    if raw_result.get("success") is False or status in {"error", "failed", "failure"}:
        return "error"
    if _bounded_errors(metadata=metadata, raw_result=raw_result):
        return "error"
    return "no_valid_credentials"


def _summary_text(
    *,
    context: Mapping[str, Optional[str]],
    outcome: str,
    success_count: int,
) -> str:
    subject = _subject(context)
    if outcome == "confirmed":
        return f"Hydra {subject} confirmed {success_count} successful logins."
    if outcome == "no_valid_credentials":
        return f"Hydra {subject} found no valid credentials."
    if outcome == "timeout":
        return f"Hydra {subject} timed out with {success_count} successful logins."
    if outcome == "lockout":
        return f"Hydra {subject} hit a lockout/rate-limit condition with {success_count} successful logins."
    return f"Hydra {subject} failed with {success_count} successful logins."


def _subject(context: Mapping[str, Optional[str]]) -> str:
    service = context.get("service") or "service"
    host = context.get("host") or "unknown target"
    port = context.get("port")
    target = f"{host}:{port}" if port else host
    return f"{service} against {target}"


def _success_count_line(
    success_count: int,
    *,
    credentials: list[dict[str, Any]],
) -> str:
    accounts = dedupe_string_list(
        credential.get("username") for credential in credentials if credential.get("username")
    )
    if accounts:
        return f"successful logins: {success_count}; accounts={', '.join(accounts)}"
    return f"successful logins: {success_count}"


def _credential_findings(
    credentials: list[dict[str, Any]],
    *,
    context: Mapping[str, Optional[str]],
) -> list[str]:
    findings = [
        "credential: " + _credential_record(credential, context=context)
        for credential in credentials[:_CREDENTIAL_LIMIT]
    ]
    if len(credentials) > _CREDENTIAL_LIMIT:
        findings.append(
            f"Hydra credential findings truncated: showing {_CREDENTIAL_LIMIT} of {len(credentials)}."
        )
    return findings


def _credential_proof_lines(
    credentials: list[dict[str, Any]],
    *,
    context: Mapping[str, Optional[str]],
) -> list[str]:
    return [
        "hydra proof: " + _credential_record(credential, context=context)
        for credential in credentials[:_EVIDENCE_LIMIT]
    ]


def _credential_record(
    credential: Mapping[str, Any],
    *,
    context: Mapping[str, Optional[str]],
) -> str:
    service = _first_text(credential.get("service"), context.get("service"))
    host = _first_text(credential.get("host"), context.get("host"))
    port = _first_text(credential.get("port"), context.get("port"))
    username = _first_text(credential.get("username")) or "<unknown>"
    parts = []
    if service:
        parts.append(f"service={service}")
    if host:
        parts.append(f"host={host}")
    if port:
        parts.append(f"port={port}")
    parts.append(f"login={username}")
    if credential.get("password_present"):
        parts.append(f"password={_REDACTED_PASSWORD}")
    parts.append(f"proof_fingerprint={_proof_fingerprint(credential, context=context)}")
    return compact_evidence_line(_mask_text(" ".join(parts)))


def _proof_fingerprint(
    credential: Mapping[str, Any],
    *,
    context: Mapping[str, Optional[str]],
) -> str:
    material = "|".join(
        str(
            _first_text(
                credential.get(key),
                context.get(key) if key in {"service", "host", "port"} else None,
            )
            or ""
        )
        for key in ("service", "host", "port", "username", "source_format")
    )
    return "sha256:" + sha256(material.encode("utf-8")).hexdigest()[:16]


def _statistics_line(value: Any) -> Optional[str]:
    statistics = _mapping_or_empty(value)
    if not statistics:
        return None
    parts = []
    login_tries = as_int(statistics.get("login_tries_total"))
    if login_tries is not None:
        parts.append(f"login_tries={login_tries}")
    completed = as_int(statistics.get("tries_completed"))
    if completed is not None:
        parts.append(f"tries_completed={completed}")
    remaining = as_int(statistics.get("tries_remaining"))
    if remaining is not None:
        parts.append(f"tries_remaining={remaining}")
    rate = statistics.get("tries_per_minute")
    if rate is not None:
        parts.append(f"tries_per_minute={rate}")
    targets_completed = as_int(statistics.get("targets_completed"))
    targets_total = as_int(statistics.get("targets_total"))
    if targets_completed is not None and targets_total is not None:
        parts.append(f"targets_completed={targets_completed}/{targets_total}")
    if not parts:
        return None
    return "attack statistics: " + ", ".join(parts)


def _lockout_findings(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    for message in _message_candidates(metadata=metadata, raw_result=raw_result):
        lowered = message.lower()
        if any(token in lowered for token in _LOCKOUT_TOKENS):
            findings.append("lockout/rate-limit signal: " + _sanitize_message(message))
        if len(findings) >= _ERROR_LIMIT:
            break
    return findings


def _bounded_errors(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    for source in (
        metadata.get("errors"),
        metadata.get("error"),
        raw_result.get("error"),
        raw_result.get("stderr"),
    ):
        for message in _iter_messages(source):
            sanitized = _sanitize_message(message)
            if sanitized and sanitized not in errors:
                errors.append(sanitized)
            if len(errors) >= _ERROR_LIMIT:
                return errors

    if errors:
        return errors

    status = _first_text(raw_result.get("status"), metadata.get("status"))
    exit_code = as_int(_first_value(raw_result.get("exit_code"), metadata.get("exit_code")))
    if status == "timeout" or exit_code == -2:
        timeout_message = "Hydra command timed out."
        if timeout_message not in errors:
            errors.append(timeout_message)
    elif raw_result.get("success") is False or status in {"error", "failed", "failure"}:
        fallback_message = f"Hydra command ended with status {status or 'error'}."
        if fallback_message not in errors:
            errors.append(fallback_message)
    return errors[:_ERROR_LIMIT]


def _message_candidates(
    *,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    messages: list[str] = []
    for source in (
        metadata.get("errors"),
        metadata.get("warnings"),
        metadata.get("error"),
        raw_result.get("error"),
        raw_result.get("stderr"),
        raw_result.get("stdout"),
    ):
        messages.extend(_iter_messages(source))
    return messages


def _semantic_evidence_lines(
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> list[str]:
    evidence = metadata.get("semantic_evidence")
    if not isinstance(evidence, list):
        evidence = raw_result.get("semantic_evidence")
    if not isinstance(evidence, list):
        return []

    masked = mask_durable_secrets(evidence, source="hydra_deterministic_semantic_evidence")
    lines: list[str] = []
    for entry in masked if isinstance(masked, list) else []:
        if not isinstance(entry, Mapping):
            continue
        name = _first_text(entry.get("name"))
        if not name:
            continue
        value = _first_text(entry.get("value"))
        line = f"semantic evidence: {name}"
        if value:
            line += f"={value}"
        if line not in lines:
            lines.append(compact_evidence_line(_mask_text(line)))
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
        source="hydra_deterministic_semantic_observations",
    )
    lines: list[str] = []
    for entry in masked if isinstance(masked, list) else []:
        if not isinstance(entry, Mapping):
            continue
        observation_type = _first_text(entry.get("observation_type"))
        if not observation_type:
            continue
        subject_key = _first_text(entry.get("subject_key"))
        payload = _mapping_or_empty(entry.get("payload"))
        count = as_int(payload.get("successful_login_count"))
        line = f"semantic observation: {observation_type}"
        if subject_key:
            line += f" {subject_key}"
        if count is not None:
            line += f" successful_login_count={count}"
        if line not in lines:
            lines.append(compact_evidence_line(_mask_text(line)))
        if len(lines) >= _SEMANTIC_OBSERVATION_LIMIT:
            break
    return lines


def _outcome_proof_line(
    *,
    outcome: str,
    context: Mapping[str, Optional[str]],
) -> str:
    material = f"{outcome}|{context.get('service') or ''}|{context.get('host') or ''}|{context.get('port') or ''}"
    fingerprint = "sha256:" + sha256(material.encode("utf-8")).hexdigest()[:16]
    return compact_evidence_line(
        f"hydra proof: outcome={outcome} service={context.get('service') or 'service'} "
        f"host={context.get('host') or 'unknown target'}"
        + (f" port={context.get('port')}" if context.get("port") else "")
        + f" proof_fingerprint={fingerprint}"
    )


def _structured_signals(
    *,
    context: Mapping[str, Optional[str]],
    outcome: str,
    success_count: int,
    metadata: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    errors: Iterable[str],
) -> list[Mapping[str, Any]]:
    signals: list[Mapping[str, Any]] = [
        {"type": "kv_pair", "key": "hydra_outcome", "value": outcome},
        {"type": "kv_pair", "key": "hydra_success_count", "value": success_count},
    ]
    if context.get("service"):
        signals.append({"type": "kv_pair", "key": "hydra_service", "value": context["service"]})
    if context.get("host"):
        signals.append({"type": "kv_pair", "key": "hydra_target_host", "value": context["host"]})
    if context.get("port"):
        signals.append({"type": "kv_pair", "key": "hydra_target_port", "value": context["port"]})
    for error in errors:
        signals.append({"type": "error_context", "message": f"Hydra failed: {error}"})
    for ref in _artifact_refs(raw_result=raw_result, metadata=metadata):
        signals.append({"type": "kv_pair", "key": "hydra_artifact_ref", "value": ref["path"]})
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


def _iter_messages(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        ]
    text = str(value).strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _sanitize_message(value: Any) -> str:
    text = compact_evidence_line(value)
    text = _PASSWORD_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED_PASSWORD}",
        text,
    )
    return _mask_text(text)


def _mask_text(value: str) -> str:
    masked = mask_durable_secrets(value, source="hydra_deterministic_adapter")
    return str(masked)


def _summary(value: str) -> str:
    if len(value) <= COMPACT_SUMMARY_MAX_CHARS:
        return value
    return value[: max(COMPACT_SUMMARY_MAX_CHARS - 3, 0)].rstrip() + "..."


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        text = _text_or_none(value)
        if text:
            return text
    return None


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


register_credential_attack_adapters()
