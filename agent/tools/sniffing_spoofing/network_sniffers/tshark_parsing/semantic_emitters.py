"""TShark semantic observation and evidence emitters.

This module preserves observation ordering, dedupe, diagnostics mutation, and
final durable masking behavior for parsed TShark metadata.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    DEFAULT_MAX_ROWS,
    _mapping_value,
    _safe_int,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.security import (
    build_secret_exposure_finding,
    compact_packet_proof,
    semantic_base_payload,
    service_subject_from_secret_exposure,
    weak_secret_exposure_diagnostic,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    infer_transport_from_application_protocol,
    normalize_application_protocol,
    normalize_port,
    normalize_transport_protocol,
)

_SERVICE_PROTOCOL_NAMES = frozenset({"dns", "ftp", "http", "smb", "smtp", "tls", "ssl"})


def build_tshark_semantic_observations(
    metadata: Mapping[str, Any],
    args: Any,
) -> list[dict[str, Any]]:
    """Build safe semantic observations from already-sanitized TShark metadata."""

    _ = args
    if not isinstance(metadata, Mapping):
        return []

    observations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    pcap = _mapping_value(metadata, "pcap")
    base_payload = semantic_base_payload(metadata, pcap)

    for host in _list_value(metadata.get("hosts")):
        host_key = _host_subject_key(host)
        if not host_key:
            continue
        append_observation(
            observations,
            seen,
            {
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": host_key,
                "payload": dict(base_payload),
            },
        )

    for row in _list_value(metadata.get("conversations")):
        conversation = _as_mapping(row)
        service = service_subject_from_conversation(conversation)
        if service is None:
            continue
        subject_key, host, protocol, port, application_protocol = service
        payload = {
            **base_payload,
            "ip": host,
            "protocol": protocol,
            "port": port,
            "evidence_source": "passive_pcap",
            "reachability": "unverified",
        }
        if application_protocol:
            payload["service_name"] = application_protocol
            payload["application_protocol"] = application_protocol
        for key in ("flow_key", "packet_count", "bytes", "src"):
            value = conversation.get(key)
            if value not in (None, "", []):
                payload[key] = value
        append_observation(
            observations,
            seen,
            {
                "observation_type": "network.service_observed",
                "subject_type": "service.socket",
                "subject_key": subject_key,
                "payload": payload,
            },
        )

    for row in _list_value(metadata.get("secret_exposure")):
        exposure = _as_mapping(row)
        finding = build_secret_exposure_finding(exposure, base_payload)
        if finding is None:
            diagnostics.append(weak_secret_exposure_diagnostic(exposure))
            continue
        append_observation(observations, seen, finding)

        service = service_subject_from_secret_exposure(exposure)
        protocol = str(exposure.get("protocol") or "").strip().lower()
        if service is not None and protocol in _SERVICE_PROTOCOL_NAMES:
            subject_key, host, transport, port, _application_protocol = service
            service_name = "tls" if protocol == "ssl" else protocol
            append_observation(
                observations,
                seen,
                {
                    "observation_type": "network.service_detected",
                    "subject_type": "service.socket",
                    "subject_key": subject_key,
                    "payload": {
                        **base_payload,
                        "ip": host,
                        "protocol": transport,
                        "port": port,
                        "service_name": service_name,
                        "source_protocol": protocol,
                    },
                },
            )

    if not _list_value(metadata.get("secret_exposure")):
        for row in _list_value(metadata.get("auth_indicators")):
            indicator = _as_mapping(row)
            diagnostics.append(
                {
                    "reason": "auth_indicator_without_specific_secret_exposure",
                    "field": str(indicator.get("field") or "").strip(),
                    "mechanism": str(indicator.get("mechanism") or "").strip(),
                    "frame": str(indicator.get("frame") or "").strip(),
                    "source": "tshark",
                }
            )
    store_semantic_observation_diagnostics(metadata, diagnostics)
    return mask_durable_secrets(observations, source="tshark_semantic_observations")


def build_tshark_semantic_evidence(
    metadata: Mapping[str, Any],
    args: Any,
) -> list[dict[str, Any]]:
    """Build vocabulary-conformant semantic evidence entries for TShark metadata."""

    if not isinstance(metadata, Mapping):
        return []

    pcap = _mapping_value(metadata, "pcap")
    limits = _mapping_value(metadata, "limits")
    evidence: list[dict[str, Any]] = []

    analysis_mode = args_or_metadata_value(args, metadata, "analysis_mode", "pcap_summary")
    evidence.append(
        {
            "type": SemanticEvidenceType.VARIANT.value,
            "name": "analysis_mode",
            "value": analysis_mode,
            "source": "tshark",
        }
    )

    input_file = args_or_metadata_value(args, pcap, "input_file", None)
    input_mode = "pcap_file" if input_file else "live_capture"
    append_execution_evidence(evidence, name="input_file_mode", value=input_mode)
    append_execution_evidence(
        evidence,
        name="max_rows",
        value=_safe_int(limits.get("max_rows"), default=_safe_int(getattr(args, "max_rows", None), default=DEFAULT_MAX_ROWS)),
        unit="rows",
    )
    append_execution_evidence(
        evidence,
        name="display_filter",
        value=getattr(args, "display_filter", None),
    )
    append_execution_evidence(
        evidence,
        name="capture_filter",
        value=getattr(args, "capture_filter", None),
    )
    field_count = len(_list_value(getattr(args, "fields", None)))
    if field_count:
        append_execution_evidence(
            evidence,
            name="field_extract_count",
            value=field_count,
            unit="fields",
        )

    packet_count = _safe_int(pcap.get("packet_count"), default=0)
    conversation_count = len(_list_value(metadata.get("conversations")))
    secret_exposure_count = len(_list_value(metadata.get("secret_exposure")))
    evidence.extend(
        [
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "packet_count",
                "value": packet_count,
                "detail": {"unit": "packets"},
                "source": "tshark",
            },
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "conversation_count",
                "value": conversation_count,
                "detail": {"unit": "conversations"},
                "source": "tshark",
            },
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "secret_exposure_count",
                "value": secret_exposure_count,
                "detail": {"unit": "exposures"},
                "source": "tshark",
            },
        ]
    )

    if bool(limits.get("truncated")):
        append_diagnostic_evidence(
            evidence,
            name="truncated_output",
            value=True,
            severity="warning",
            note=truncated_lists_note(limits),
        )
    unsupported_note = unsupported_fields_note(metadata)
    if unsupported_note:
        append_diagnostic_evidence(
            evidence,
            name="unsupported_fields",
            value=True,
            severity="warning",
            note=unsupported_note,
        )
    proof = compact_packet_proof(metadata)
    if proof:
        append_diagnostic_evidence(
            evidence,
            name="packet_proof",
            value=proof,
            severity="info",
            note="secret_exposure",
        )

    return mask_durable_secrets(evidence, source="tshark_semantic_evidence")


def append_observation(
    observations: list[dict[str, Any]],
    seen: set[str],
    observation: dict[str, Any],
) -> None:
    marker = json.dumps(observation, sort_keys=True, default=str)
    if marker in seen:
        return
    seen.add(marker)
    observations.append(observation)


def service_subject_from_conversation(
    conversation: Mapping[str, Any],
) -> tuple[str, str, str, int, str | None] | None:
    host = str(conversation.get("dst") or "").strip().lower()
    raw_protocol = str(conversation.get("protocol") or "").strip().lower()
    protocol = normalize_transport_protocol(raw_protocol, default=None)
    application_protocol = None
    if protocol is None:
        application_protocol = normalize_application_protocol(raw_protocol)
        protocol = infer_transport_from_application_protocol(application_protocol)
    port = normalize_port(conversation.get("dst_port"))
    if not host or port is None or protocol is None:
        return None
    try:
        subject_key = build_service_socket_key(ip=host, protocol=protocol, port=port)
    except ValueError:
        return None
    return (subject_key, host, protocol, port, application_protocol)


def store_semantic_observation_diagnostics(
    metadata: Mapping[str, Any],
    diagnostics: list[dict[str, Any]],
) -> None:
    if not diagnostics or not isinstance(metadata, dict):
        return
    existing = metadata.get("semantic_observation_diagnostics")
    rows = [*(_list_value(existing)), *diagnostics]
    metadata["semantic_observation_diagnostics"] = rows[:DEFAULT_MAX_ROWS]


def append_execution_evidence(
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
        "value": evidence_scalar(value),
        "source": "tshark",
    }
    if unit:
        entry["detail"] = {"unit": unit}
    evidence.append(entry)


def append_diagnostic_evidence(
    evidence: list[dict[str, Any]],
    *,
    name: str,
    value: Any,
    severity: str,
    note: str,
) -> None:
    if value in (None, "", []):
        return
    evidence.append(
        {
            "type": SemanticEvidenceType.DIAGNOSTIC.value,
            "name": name,
            "value": evidence_scalar(value),
            "detail": {
                "severity": severity,
                "note": bounded_text(note, 96),
            },
            "source": "tshark",
        }
    )


def args_or_metadata_value(
    args: Any,
    metadata: Mapping[str, Any],
    key: str,
    default: Any,
) -> Any:
    value = getattr(args, key, None)
    if hasattr(value, "value"):
        value = value.value
    if value not in (None, "", []):
        return value
    return metadata.get(key, default)


def evidence_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        enum_value = value.value
        if isinstance(enum_value, (str, int, float, bool)) or enum_value is None:
            return enum_value
    return str(value)


def bounded_text(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def truncated_lists_note(limits: Mapping[str, Any]) -> str:
    lists = _mapping_value(limits, "lists")
    truncated = [
        name
        for name, details in lists.items()
        if isinstance(details, Mapping) and bool(details.get("truncated"))
    ]
    if not truncated:
        return "metadata_limit"
    return ",".join(sorted(str(name) for name in truncated)[:4])


def unsupported_fields_note(metadata: Mapping[str, Any]) -> str:
    matches: list[str] = []
    for warning in _list_value(metadata.get("warnings")):
        text = str(warning or "")
        if "not allowlisted" in text or "unsupported" in text.lower():
            matches.append(text)
    return bounded_text("; ".join(matches[:3]), 160) if matches else ""


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _host_subject_key(value: Any) -> str | None:
    host = str(value or "").strip().lower()
    if not host or " " in host:
        return None
    return f"host.ip:{host}"


__all__ = (
    "append_observation",
    "build_tshark_semantic_evidence",
    "build_tshark_semantic_observations",
    "service_subject_from_conversation",
    "store_semantic_observation_diagnostics",
)
