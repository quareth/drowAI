"""Legacy-compatible TShark security proof shaping helpers.

This module owns the refactored parser package copy of secret proof modes,
auth indicator shaping, secret exposure rows, and durable proof masking. Generic
decoded-PCAP credential correlation remains in agent.tools.pcap_analysis.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, Dict

from agent.tools.pcap_analysis import extract_critical_signals
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    DEFAULT_SENSITIVE_PROOF_MODE,
    SECRET_FINGERPRINT_KEY_ENV,
    _field_row_context,
    _field_rows_to_packet_rows,
    _field_values,
    _flow_key,
    _none_if_empty,
    _normalize_sensitive_proof_mode,
    _safe_int,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.canonical_keys import build_finding_vulnerability_key
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    infer_transport_from_application_protocol,
    normalize_application_protocol,
    normalize_port,
    normalize_transport_protocol,
)

_SECRET_KIND_RE = re.compile(r"[^a-z0-9_]+")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)([^\r\n]+)"
)
_COOKIE_HEADER_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)")
_API_KEY_HEADER_RE = re.compile(
    r"(?i)\b((?:x[-_])?(?:api[-_]?key|auth[-_]?token|access[-_]?token|refresh[-_]?token|id[-_]?token)\s*:\s*)([^\r\n]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=\-]{6,})")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|session[_-]?id|session|token|secret)\b(\s*[:=]\s*[\"']?)([^\"'\s;&]+)"
)
_PROTOCOL_AUTH_ARG_RE = re.compile(
    r"(?i)\b((?:PASS|AUTH(?:ENTICATE)?|LOGIN)\s+|(?:request\.command_parameter|auth\.argument)\s*[:=]\s*)([^\r\n\s]+)"
)
_COMMAND_AUTH_ARG_RE = re.compile(
    r"(?i)\b(--(?:user|password|pass|oauth2-bearer|proxy-user)\s+)([^\s]+)"
)
_SENSITIVE_FIELD_NAME_RE = re.compile(
    r"(?i)(authorization|cookie|passwd|password|pwd|api[_-]?key|token|secret|private[_-]?key|auth(?:entication)?(?:_|\.)?(?:arg|parameter))"
)
_SECRET_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PRIVATE_KEY_BLOCK_RE),
    ("authorization_header", _AUTHORIZATION_HEADER_RE),
    ("cookie", _COOKIE_HEADER_RE),
    ("api_key", _API_KEY_HEADER_RE),
    ("bearer_token", _BEARER_TOKEN_RE),
    ("secret", _SECRET_KV_RE),
    ("protocol_auth_argument", _PROTOCOL_AUTH_ARG_RE),
)
_CREDENTIAL_SECRET_KINDS = frozenset(
    {
        "authorization_header",
        "cookie",
        "password",
        "protocol_auth_argument",
    }
)


def fingerprint_secret(value: str, *, kind: str) -> str | None:
    """Return keyed fingerprint for proof correlation, or None if unavailable."""

    key = (os.getenv(SECRET_FINGERPRINT_KEY_ENV) or "").strip()
    normalized_value = str(value or "")
    if not key or not normalized_value:
        return None

    normalized_kind = _normalize_secret_kind(kind)
    message = f"{normalized_kind}\0{normalized_value}".encode("utf-8")
    digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{normalized_kind}:{digest[:32]}"


def parse_critical_signal_rows(
    rows: list[Mapping[str, Any]],
    *,
    artifact_sha256: str | None,
    sensitive_proof_mode: str,
) -> Dict[str, Any]:
    """Extract generic credential/auth signals and apply TShark proof modes."""

    raw = extract_critical_signals(rows)
    proof_mode = _normalize_sensitive_proof_mode(sensitive_proof_mode)
    credential_events = [
        credential_event_with_proof(
            event,
            artifact_sha256=artifact_sha256,
            proof_mode=proof_mode,
        )
        for event in _list_value(raw.get("credential_events"))
        if isinstance(event, Mapping)
    ]
    auth_sequences = [
        auth_sequence_with_proof(sequence, credential_events, proof_mode=proof_mode)
        for sequence in _list_value(raw.get("auth_sequences"))
        if isinstance(sequence, Mapping)
    ]
    return {
        "credential_events": credential_events,
        "auth_sequences": auth_sequences,
        "auth_success": _list_value(raw.get("auth_success")),
    }


def parse_auth_indicator_rows(
    rows: list[Mapping[str, Any]],
    *,
    critical: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    indicators: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    critical_map = critical if isinstance(critical, Mapping) else parse_critical_signal_rows(
        rows,
        artifact_sha256=None,
        sensitive_proof_mode=DEFAULT_SENSITIVE_PROOF_MODE,
    )
    for event in _list_value(critical_map.get("credential_events")):
        if not isinstance(event, Mapping):
            continue
        value = event.get("proof_excerpt")
        if value in (None, "", []):
            value = event.get("fingerprint") or event.get("kind")
        indicator = {
            "frame": event.get("frame"),
            "time": event.get("time"),
            "protocol": event.get("protocol"),
            "field": event.get("field"),
            "mechanism": _auth_mechanism(
                str(event.get("kind") or ""),
                str(event.get("field") or ""),
                str(value or ""),
            ),
            "value": value,
        }
        dedupe_key = (
            indicator["frame"],
            indicator["field"],
            indicator["mechanism"],
            indicator["value"],
        )
        if dedupe_key not in seen:
            indicators.append(indicator)
            seen.add(dedupe_key)
    for sequence in _list_value(critical_map.get("auth_sequences")):
        if not isinstance(sequence, Mapping) or _safe_int(sequence.get("success_count")) <= 0:
            continue
        indicator = {
            "frame": ",".join(str(item) for item in _list_value(sequence.get("frames"))),
            "time": None,
            "protocol": sequence.get("protocol"),
            "field": "auth_sequence",
            "mechanism": "auth_success",
            "value": ",".join(str(item) for item in _list_value(sequence.get("success_messages"))),
        }
        dedupe_key = (
            indicator["frame"],
            indicator["field"],
            indicator["mechanism"],
            indicator["value"],
        )
        if dedupe_key not in seen:
            indicators.append(indicator)
            seen.add(dedupe_key)
    return {
        "auth_indicators": indicators,
        "warnings": [] if indicators or not rows else ["No authentication indicators found in TShark JSON output."],
    }


def parse_secret_exposure_rows(
    rows: list[Mapping[str, Any]],
    *,
    artifact_sha256: str | None = None,
    sensitive_proof_mode: str = DEFAULT_SENSITIVE_PROOF_MODE,
    critical: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    exposures: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    proof_mode = _normalize_sensitive_proof_mode(sensitive_proof_mode)
    critical_map = critical if isinstance(critical, Mapping) else parse_critical_signal_rows(
        rows,
        artifact_sha256=artifact_sha256,
        sensitive_proof_mode=proof_mode,
    )
    for event in _list_value(critical_map.get("credential_events")):
        if not isinstance(event, Mapping):
            continue
        role = str(event.get("role") or "").strip()
        if role == "username":
            continue
        exposure = dict(event)
        exposure.setdefault("pcap_artifact_sha256", artifact_sha256)
        exposure.setdefault("proof_mode", proof_mode)
        exposure.pop("role", None)
        exposure.pop("value", None)
        exposure.pop("command", None)
        dedupe_key = (
            exposure.get("frame"),
            exposure.get("field"),
            exposure.get("kind"),
            exposure.get("proof_mode"),
            exposure.get("proof_excerpt"),
            exposure.get("fingerprint"),
        )
        if dedupe_key not in seen:
            exposures.append(exposure)
            seen.add(dedupe_key)
    return {
        "secret_exposure": exposures,
        "warnings": [] if exposures or not rows else ["No secret exposure proof found in TShark JSON output."],
    }


def parse_security_field_rows(
    rows: list[Mapping[str, Any]],
    *,
    artifact_sha256: str | None,
    sensitive_proof_mode: str,
) -> dict[str, list[dict[str, Any]]]:
    credential_events: list[dict[str, Any]] = []
    auth_indicators: list[dict[str, Any]] = []
    secret_exposure: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    proof_mode = _normalize_sensitive_proof_mode(sensitive_proof_mode)
    critical = parse_critical_signal_rows(
        _field_rows_to_packet_rows(rows),
        artifact_sha256=artifact_sha256,
        sensitive_proof_mode=proof_mode,
    )
    for event in _list_value(critical.get("credential_events")):
        if not isinstance(event, Mapping):
            continue
        key = (event.get("frame"), event.get("field"), event.get("role"), event.get("proof_excerpt"), event.get("fingerprint"))
        if key in seen:
            continue
        seen.add(key)
        event_dict = dict(event)
        credential_events.append(event_dict)
        indicator_value = (
            event_dict.get("proof_excerpt")
            or event_dict.get("fingerprint")
            or event_dict.get("kind")
        )
        auth_indicators.append(
            {
                "frame": event_dict.get("frame"),
                "time": event_dict.get("time"),
                "protocol": event_dict.get("protocol"),
                "field": event_dict.get("field"),
                "mechanism": _auth_mechanism(
                    str(event_dict.get("kind") or ""),
                    str(event_dict.get("field") or ""),
                    str(indicator_value or ""),
                ),
                "value": indicator_value,
            }
        )
        if str(event_dict.get("role") or "").strip() != "username":
            exposure = dict(event_dict)
            exposure.pop("role", None)
            exposure.pop("value", None)
            exposure.pop("command", None)
            secret_exposure.append(exposure)
    for sequence in _list_value(critical.get("auth_sequences")):
        if not isinstance(sequence, Mapping) or _safe_int(sequence.get("success_count")) <= 0:
            continue
        auth_indicators.append(
            {
                "frame": ",".join(str(item) for item in _list_value(sequence.get("frames"))),
                "time": None,
                "protocol": sequence.get("protocol"),
                "field": "auth_sequence",
                "mechanism": "auth_success",
                "value": ",".join(str(item) for item in _list_value(sequence.get("success_messages"))),
            }
        )
    for fields in rows:
        context = _field_row_context(fields)
        for field_name, raw_value in fields.items():
            value = _none_if_empty(raw_value)
            if not value or not _SENSITIVE_FIELD_NAME_RE.search(str(field_name)):
                continue
            kind = _normalize_secret_kind(_auth_mechanism("", str(field_name), str(value)))
            event = credential_event_with_proof(
                {
                    "frame": context["frame"],
                    "time": context["time"],
                    "stream": context["stream"],
                    "protocol": context["protocol"],
                    "src": context["src"],
                    "dst": context["dst"],
                    "field": field_name,
                    "flow_key": _flow_key(context),
                    "kind": kind,
                    "role": "secret",
                    "value": str(value),
                    "extraction_filter": field_name,
                },
                artifact_sha256=artifact_sha256,
                proof_mode=_normalize_sensitive_proof_mode(sensitive_proof_mode),
            )
            key = (event.get("frame"), event.get("field"), event.get("proof_excerpt"), event.get("fingerprint"))
            if key in seen:
                continue
            seen.add(key)
            credential_events.append(event)
            indicator_value = event.get("proof_excerpt") or event.get("fingerprint") or event.get("kind")
            auth_indicators.append(
                {
                    "frame": event.get("frame"),
                    "time": event.get("time"),
                    "protocol": event.get("protocol"),
                    "field": event.get("field"),
                    "mechanism": _auth_mechanism(str(event.get("kind") or ""), str(field_name), str(value)),
                    "value": indicator_value,
                }
            )
            exposure = dict(event)
            exposure.pop("role", None)
            exposure.pop("value", None)
            secret_exposure.append(exposure)
    return {
        "credential_events": credential_events,
        "auth_indicators": auth_indicators,
        "secret_exposure": secret_exposure,
        "auth_sequences": [
            dict(sequence)
            for sequence in _list_value(critical.get("auth_sequences"))
            if isinstance(sequence, Mapping)
        ],
    }


def credential_event_with_proof(
    event: Mapping[str, Any],
    *,
    artifact_sha256: str | None,
    proof_mode: str,
) -> dict[str, Any]:
    """Return a runtime credential event honoring the requested proof mode."""

    value = str(event.get("value") or "").strip()
    kind = _normalize_secret_kind(str(event.get("kind") or "secret"))
    result = {
        "frame": event.get("frame"),
        "time": event.get("time"),
        "stream": event.get("stream"),
        "protocol": event.get("protocol"),
        "src": event.get("src"),
        "dst": event.get("dst"),
        "field": event.get("field"),
        "flow_key": event.get("flow_key"),
        "pcap_artifact_sha256": artifact_sha256,
        "extraction_filter": event.get("extraction_filter"),
        "kind": kind,
        "role": event.get("role"),
        "command": event.get("command"),
        "proof_mode": proof_mode,
    }
    if proof_mode == "proof_excerpt" and value:
        result["proof_excerpt"] = value
    elif proof_mode == "fingerprint" and value:
        fingerprint = fingerprint_secret(value, kind=kind)
        if fingerprint:
            result["fingerprint"] = fingerprint
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def auth_sequence_with_proof(
    sequence: Mapping[str, Any],
    credential_events: list[Mapping[str, Any]],
    *,
    proof_mode: str,
) -> dict[str, Any]:
    """Return an auth sequence with proofs omitted unless proof mode allows them."""

    result = {
        "stream": sequence.get("stream"),
        "flow_key": sequence.get("flow_key"),
        "protocol": sequence.get("protocol"),
        "src": sequence.get("src"),
        "dst": sequence.get("dst"),
        "frames": _list_value(sequence.get("frames")),
        "event_count": _safe_int(sequence.get("event_count")),
        "username_count": _safe_int(sequence.get("username_count")),
        "secret_count": _safe_int(sequence.get("secret_count")),
        "success_count": _safe_int(sequence.get("success_count")),
        "success_messages": _list_value(sequence.get("success_messages")),
        "proof_mode": proof_mode,
    }
    if proof_mode == "proof_excerpt":
        result["username_proofs"] = _list_value(sequence.get("username_proofs"))
        result["secret_proofs"] = _list_value(sequence.get("secret_proofs"))
    elif proof_mode == "fingerprint":
        stream = str(sequence.get("stream") or "").strip()
        flow_key = str(sequence.get("flow_key") or "").strip()
        fingerprints = [
            event.get("fingerprint")
            for event in credential_events
            if isinstance(event, Mapping)
            and event.get("fingerprint")
            and str(event.get("stream") or "").strip() == stream
            and str(event.get("flow_key") or "").strip() == flow_key
        ]
        result["fingerprints"] = [str(item) for item in fingerprints]
    return {key: value for key, value in result.items() if value not in (None, "", [])}


def http_secret_headers(http: Mapping[str, Any]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for field_name, kind in (
        ("http.authorization", "authorization_header"),
        ("http.proxy_authorization", "authorization_header"),
        ("http.cookie", "cookie"),
        ("http.set_cookie", "cookie"),
    ):
        _ = kind
        values = [value for value in _field_values(http, field_name)]
        if values:
            headers[field_name] = values
    return headers


def iter_sensitive_values(value: Any) -> Iterable[tuple[str, str, str]]:
    for field_name, field_value in _iter_leaf_values(value):
        text = str(field_value).strip()
        if not text:
            continue
        kind = classify_secret(field_name, text)
        if kind:
            yield field_name, text, kind


def classify_secret(field_name: str, value: str) -> str | None:
    field_lower = field_name.lower()
    if "authorization" in field_lower:
        return "authorization_header"
    if "cookie" in field_lower or "session" in field_lower:
        return "cookie"
    if "private" in field_lower and "key" in field_lower:
        return "private_key"
    if "api" in field_lower and "key" in field_lower:
        return "api_key"
    if "token" in field_lower:
        return "bearer_token"
    if "password" in field_lower or "passwd" in field_lower or field_lower.endswith(".pwd"):
        return "password"
    if "auth.argument" in field_lower or "command_parameter" in field_lower:
        return "protocol_auth_argument"
    if _SENSITIVE_FIELD_NAME_RE.search(field_name):
        return "secret"
    for kind, pattern in _SECRET_TEXT_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def semantic_base_payload(
    metadata: Mapping[str, Any],
    pcap: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "tshark",
        "source_tool": "tshark",
    }
    analysis_mode = str(metadata.get("analysis_mode") or "").strip()
    if analysis_mode:
        payload["analysis_mode"] = analysis_mode
    for key in ("input_file", "artifact_sha256"):
        value = pcap.get(key)
        if value not in (None, "", []):
            payload[f"pcap_{key}"] = value
    return payload


def service_subject_from_secret_exposure(
    exposure: Mapping[str, Any],
) -> tuple[str, str, str, int, str | None] | None:
    parsed = _parse_flow_key(exposure.get("flow_key"))
    if parsed is not None:
        protocol, host, port, application_protocol = parsed
        try:
            subject_key = build_service_socket_key(ip=host, protocol=protocol, port=port)
        except ValueError:
            return None
        return (subject_key, host, protocol, port, application_protocol)
    host = str(exposure.get("dst") or "").strip().lower()
    if not host:
        return None
    return None


def build_secret_exposure_finding(
    exposure: Mapping[str, Any],
    base_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if secret_exposure_specificity_gap(exposure):
        return None

    subject = service_subject_from_secret_exposure(exposure)
    affected_subject_key: str | None = None
    affected_subject_type: str | None = None
    if subject is not None:
        affected_subject_key = subject[0]
        affected_subject_type = "service.socket"
    else:
        host_key = _host_subject_key(exposure.get("dst") or exposure.get("src"))
        if host_key:
            affected_subject_key = host_key
            affected_subject_type = "host.ip"

    if not affected_subject_key or not affected_subject_type:
        return None

    protocol = str(exposure.get("protocol") or "").strip().lower()
    field = str(exposure.get("field") or "").strip().lower()
    kind = _normalize_secret_kind(str(exposure.get("kind") or "secret"))
    finding_kind = (
        "credential_exposure_detected"
        if kind in _CREDENTIAL_SECRET_KINDS
        else "secret_exposure_detected"
    )
    detector_id = f"tshark/{finding_kind}/{field or kind}"
    finding_key = build_finding_vulnerability_key(
        subject_key=affected_subject_key,
        detector_id=detector_id,
    )
    payload: dict[str, Any] = {
        **base_payload,
        "detector_id": detector_id,
        "finding_subtype": finding_kind,
        "title": (
            "Credential material exposed in packet capture"
            if finding_kind == "credential_exposure_detected"
            else "Secret material exposed in packet capture"
        ),
        "severity": "medium",
        "subject_key": affected_subject_key,
        "subject_type": affected_subject_type,
        "protocol": protocol,
        "field": field,
        "kind": kind,
    }
    for key in (
        "frame",
        "stream",
        "src",
        "dst",
        "flow_key",
        "extraction_filter",
        "proof_mode",
        "proof_excerpt",
        "fingerprint",
        "pcap_artifact_sha256",
    ):
        value = (
            durable_mask_secret_exposure_proof_excerpt(exposure)
            if key == "proof_excerpt"
            else exposure.get(key)
        )
        if value not in (None, "", []):
            payload[key] = value
    return {
        "observation_type": "finding.vulnerability_detected",
        "subject_type": "finding.vulnerability",
        "subject_key": finding_key,
        "payload": payload,
    }


def secret_exposure_specificity_gap(exposure: Mapping[str, Any]) -> str | None:
    if service_subject_from_secret_exposure(exposure) is None and not _host_subject_key(
        exposure.get("dst") or exposure.get("src")
    ):
        return "missing_subject"
    if not str(exposure.get("protocol") or "").strip():
        return "missing_protocol"
    if not (
        str(exposure.get("frame") or "").strip()
        or str(exposure.get("stream") or "").strip()
    ):
        return "missing_frame_or_stream"
    field = str(exposure.get("field") or "").strip()
    proof_excerpt = str(exposure.get("proof_excerpt") or "").strip()
    proof_mode = str(exposure.get("proof_mode") or DEFAULT_SENSITIVE_PROOF_MODE).strip().lower()
    if proof_mode == "fingerprint" and not str(exposure.get("fingerprint") or "").strip().startswith("hmac-sha256:"):
        return "missing_fingerprint"
    if proof_mode == "proof_excerpt" and not proof_excerpt:
        return "missing_proof_excerpt"
    if not (field or proof_excerpt):
        return "missing_proof_excerpt_or_field"
    return None


def weak_secret_exposure_diagnostic(exposure: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reason": secret_exposure_specificity_gap(exposure) or "unsupported_secret_exposure",
        "field": str(exposure.get("field") or "").strip(),
        "protocol": str(exposure.get("protocol") or "").strip(),
        "frame": str(exposure.get("frame") or "").strip(),
        "stream": str(exposure.get("stream") or "").strip(),
        "source": "tshark",
    }


def compact_packet_proof(metadata: Mapping[str, Any]) -> str:
    for row in _list_value(metadata.get("secret_exposure")):
        exposure = _as_mapping(row)
        if secret_exposure_specificity_gap(exposure):
            continue
        excerpt = safe_proof_excerpt(exposure)
        parts = [
            f"protocol={str(exposure.get('protocol') or '').strip().lower()}",
            f"frame={str(exposure.get('frame') or '').strip()}",
        ]
        stream = str(exposure.get("stream") or "").strip()
        if stream:
            parts.append(f"stream={stream}")
        field = str(exposure.get("field") or "").strip().lower()
        if field:
            parts.append(f"field={field}")
        proof_mode = str(exposure.get("proof_mode") or "").strip().lower()
        if proof_mode:
            parts.append(f"proof_mode={proof_mode}")
        if excerpt:
            parts.append(f"proof={excerpt}")
        fingerprint = str(exposure.get("fingerprint") or "").strip()
        if fingerprint:
            parts.append(f"fingerprint={fingerprint}")
        return _bounded_text(" ".join(parts), 240)
    return ""


def safe_proof_excerpt(exposure: Mapping[str, Any]) -> str:
    excerpt = str(exposure.get("proof_excerpt") or "").strip()
    if not excerpt:
        masked = durable_mask_secret_exposure_proof_excerpt(exposure)
        return masked or "<DURABLE_SECRET_MASK:secret>"
    masked = durable_mask_secret_exposure_proof_excerpt(exposure)
    return _bounded_text(str(masked), 96)


def durable_mask_secret_exposure_proof_excerpt(exposure: Mapping[str, Any]) -> str:
    excerpt = str(exposure.get("proof_excerpt") or "").strip()
    if not excerpt:
        if secret_exposure_has_sensitive_proof_context(exposure):
            return "<DURABLE_SECRET_MASK:secret>"
        return ""
    masked = mask_durable_secrets(excerpt, source="tshark_secret_exposure_proof")
    if str(masked) != excerpt:
        return str(masked)
    if not secret_exposure_has_sensitive_proof_context(exposure):
        return excerpt
    contextual = mask_durable_secrets(
        {"credential": excerpt},
        source="tshark_secret_exposure_proof_context",
    )
    if isinstance(contextual, Mapping):
        value = contextual.get("credential")
        if value not in (None, "", []):
            return str(value)
    return "<DURABLE_SECRET_MASK:secret>"


def secret_exposure_has_sensitive_proof_context(exposure: Mapping[str, Any]) -> bool:
    kind = _normalize_secret_kind(str(exposure.get("kind") or "secret"))
    field = str(exposure.get("field") or "").strip()
    return kind in {
        "authorization_header",
        "bearer_token",
        "cookie",
        "password",
        "protocol_auth_argument",
        "api_key",
        "private_key",
        "secret",
    } or bool(_SENSITIVE_FIELD_NAME_RE.search(field))


def _normalize_secret_kind(kind: str) -> str:
    normalized = _SECRET_KIND_RE.sub("_", str(kind or "secret").lower()).strip("_")
    return normalized or "secret"


def _auth_mechanism(kind: str, field_name: str, value: str) -> str:
    field_lower = field_name.lower()
    value_lower = value.lower()
    if "cookie" in field_lower:
        return "cookie"
    if "authorization" in field_lower or "bearer" in value_lower:
        return "authorization"
    if "ftp" in field_lower or "request.command_parameter" in field_lower:
        return "protocol_auth"
    return _normalize_secret_kind(kind)


def _iter_leaf_values(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _iter_leaf_values(item, path)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_leaf_values(item, prefix)
    else:
        yield prefix, value


def _parse_flow_key(value: Any) -> tuple[str, str, int, str | None] | None:
    flow_key = str(value or "").strip().lower()
    if "->" not in flow_key or ":" not in flow_key:
        return None
    left, right = flow_key.split("->", 1)
    raw_protocol, _source = left.split(":", 1)
    protocol = normalize_transport_protocol(raw_protocol, default=None)
    application_protocol = None
    if protocol is None:
        application_protocol = normalize_application_protocol(raw_protocol)
        protocol = infer_transport_from_application_protocol(application_protocol)
    if protocol is None:
        return None
    try:
        host, raw_port = right.rsplit(":", 1)
    except ValueError:
        return None
    port = normalize_port(raw_port)
    if not host or port is None:
        return None
    return protocol, host, port, application_protocol


def _host_subject_key(value: Any) -> str | None:
    host = str(value or "").strip().lower()
    if not host or " " in host:
        return None
    return f"host.ip:{host}"


def _bounded_text(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = (
    "build_secret_exposure_finding",
    "classify_secret",
    "compact_packet_proof",
    "credential_event_with_proof",
    "durable_mask_secret_exposure_proof_excerpt",
    "fingerprint_secret",
    "http_secret_headers",
    "iter_sensitive_values",
    "parse_auth_indicator_rows",
    "parse_critical_signal_rows",
    "parse_secret_exposure_rows",
    "parse_security_field_rows",
    "safe_proof_excerpt",
    "secret_exposure_has_sensitive_proof_context",
    "secret_exposure_specificity_gap",
    "semantic_base_payload",
    "service_subject_from_secret_exposure",
    "weak_secret_exposure_diagnostic",
)
