"""Deterministic adapter for TShark rich PCAP analysis output.

This adapter converts TShark's masked semantic observations and structured
metadata into durable knowledge observations. It does not inspect raw PCAP
bytes; artifact fallback is limited to durable-masked JSON metadata.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

from ..contracts import ObservationCreate, validate_subject_key_matches_type
from ..identity.canonical_keys import build_secret_exposure_finding_key
from .base import AdapterContext
from .network_common import (
    build_host_subject_key,
    build_service_subject_key,
    collect_artifact_text_blobs,
    dedupe_observations,
    make_observation,
    normalize_port,
    normalize_transport_protocol,
    resolve_evidence_refs,
)
from .semantic_common import extract_semantic_observations
from runtime_shared.semantic.service_identity import (
    infer_transport_from_application_protocol,
    normalize_application_protocol,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets

_TOOL_NAME = "sniffing_spoofing.network_sniffers.tshark"
_REDACTED_MARKER = "<REDACTED>"
_DURABLE_MASK_MARKER = "<DURABLE_SECRET_MASK"
_ALLOWED_SEMANTIC_SUBJECTS: Mapping[str, set[str]] = {
    "network.host_discovered": {"host.ip"},
    "network.service_observed": {"service.socket"},
    "network.service_detected": {"service.socket"},
    "finding.vulnerability_detected": {"finding.vulnerability"},
}
_SENSITIVE_VALUE_FIELD_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "password",
        "passwd",
        "pwd",
        "api_key",
        "apikey",
        "x_api_key",
        "auth_token",
        "access_token",
        "refresh_token",
        "id_token",
        "session",
        "session_id",
        "secret",
        "private_key",
        "raw_secret",
        "raw_token",
        "raw_cookie",
        "raw_password",
        "credential",
        "credentials",
    }
)
_RAW_VALUE_FIELD_NAMES = frozenset(
    {
        "value",
        "raw_value",
        "payload",
        "raw_payload",
        "full_payload",
        "excerpt",
        "raw_excerpt",
    }
)
_SAFE_PROOF_FIELD_NAMES = frozenset(
    {
        "proof_excerpt",
        "fingerprint",
        "extraction_filter",
        "detector_id",
        "finding_subtype",
        "title",
        "kind",
        "field",
    }
)
_SEMANTIC_PROOF_FIELD_NAMES = frozenset(
    {
        "proof_excerpt",
        "excerpt",
        "value",
        "proof_id",
        "exposure_proof_id",
        "raw_value",
        "payload",
        "raw_payload",
        "full_payload",
        "raw_excerpt",
    }
)
_SENSITIVE_HINT_TOKENS = (
    "authorization",
    "cookie",
    "passwd",
    "password",
    "api_key",
    "apikey",
    "token",
    "secret",
    "private_key",
    "credential",
)


class TsharkKnowledgeAdapter:
    """Normalize TShark execution payloads into safe durable observations."""

    tool_names = (_TOOL_NAME,)
    capability_families: tuple[str, ...] = ()

    def supports(self, context: AdapterContext) -> bool:
        return context.source_tool_name() in self.tool_names

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = self._extract_from_semantic_observations(context)
        if semantic:
            return semantic

        observations = self._extract_from_metadata(context, context.tool_metadata)
        if observations:
            return observations

        for metadata in self._extract_metadata_from_artifact_json(context):
            observations = self._extract_from_metadata(context, metadata)
            if observations:
                return observations
        return []

    def _extract_from_semantic_observations(
        self,
        context: AdapterContext,
    ) -> list[ObservationCreate]:
        filtered_rows = []
        for row in context.semantic_observations:
            sanitized_row = _sanitize_semantic_observation_row(row)
            if sanitized_row is not None:
                filtered_rows.append(sanitized_row)
        if not filtered_rows:
            return []

        semantic_context = replace(context, semantic_observations=filtered_rows)
        observations = extract_semantic_observations(
            semantic_context,
            allowed_subject_types_by_observation=_ALLOWED_SEMANTIC_SUBJECTS,
        )
        evidence_refs = resolve_evidence_refs(context)
        if not evidence_refs:
            return observations

        with_refs: list[ObservationCreate] = []
        for observation in observations:
            payload = dict(observation.payload or {})
            if not isinstance(payload.get("evidence_refs"), list):
                payload["evidence_refs"] = list(evidence_refs)
            with_refs.append(
                make_observation(
                    context=context,
                    observation_type=observation.observation_type,
                    subject_type=observation.subject_type,
                    subject_key=observation.subject_key,
                    payload=payload,
                )
            )
        return dedupe_observations(with_refs)

    def _extract_from_metadata(
        self,
        context: AdapterContext,
        metadata: Mapping[str, Any],
    ) -> list[ObservationCreate]:
        metadata = _mask_mapping(metadata, source="tshark_knowledge_metadata")
        if not _is_tshark_metadata(metadata) or _contains_top_level_unredacted_secret_field(metadata):
            return []

        evidence_refs = resolve_evidence_refs(context)
        base_payload = _base_payload(metadata, evidence_refs)
        observations: list[ObservationCreate] = []

        for host in _list_value(metadata.get("hosts")):
            host_key = _host_subject_key(host)
            if not host_key:
                continue
            observations.append(
                make_observation(
                    context=context,
                    observation_type="network.host_discovered",
                    subject_type="host.ip",
                    subject_key=host_key,
                    payload=base_payload,
                )
            )

        for row in _list_value(metadata.get("conversations")):
            conversation = row if isinstance(row, Mapping) else {}
            if _contains_unredacted_secret_field(conversation):
                continue
            service = _service_from_conversation(conversation)
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
            observations.append(
                make_observation(
                    context=context,
                    observation_type="network.service_observed",
                    subject_type="service.socket",
                    subject_key=subject_key,
                    payload=payload,
                )
            )

        for row in _list_value(metadata.get("secret_exposure")):
            exposure = row if isinstance(row, Mapping) else {}
            finding = self._finding_from_secret_exposure(context, exposure, base_payload)
            if finding is not None:
                observations.append(finding)

        return dedupe_observations(observations)

    def _extract_metadata_from_artifact_json(
        self,
        context: AdapterContext,
    ) -> list[dict[str, Any]]:
        metadata_rows: list[dict[str, Any]] = []
        for _artifact_id, content in collect_artifact_text_blobs(context):
            text = str(content or "").strip()
            if not text or text[0] not in "{[":
                continue
            for payload in _load_json_payloads(text):
                if isinstance(payload, Mapping) and _is_tshark_metadata(payload):
                    row = dict(payload)
                    if not _contains_top_level_unredacted_secret_field(row):
                        metadata_rows.append(row)
        return metadata_rows

    def _finding_from_secret_exposure(
        self,
        context: AdapterContext,
        exposure: Mapping[str, Any],
        base_payload: Mapping[str, Any],
    ) -> ObservationCreate | None:
        if _contains_unredacted_secret_field(exposure):
            return None
        if not _has_safe_secret_exposure_proof(exposure):
            return None

        service = _service_from_secret_exposure(exposure)
        affected_subject_key = service[0] if service is not None else _host_subject_key(
            exposure.get("dst") or exposure.get("src")
        )
        if not affected_subject_key:
            return None

        protocol = str(exposure.get("protocol") or "").strip().lower()
        field = str(exposure.get("field") or "").strip().lower()
        kind = str(exposure.get("kind") or "secret").strip().lower() or "secret"
        detector_id = f"tshark/secret_exposure/{field or kind}"
        fingerprint = str(exposure.get("fingerprint") or "").strip()
        proof_id = fingerprint or _proof_id_from_exposure(exposure)
        try:
            finding_key = build_secret_exposure_finding_key(
                subject_key=affected_subject_key,
                detector_id=detector_id,
                protocol=protocol or "unknown",
                exposure_kind=kind,
                proof_id=proof_id,
                flow_key=str(exposure.get("flow_key") or ""),
            )
        except ValueError:
            return None

        payload: dict[str, Any] = {
            **base_payload,
            "detector_id": detector_id,
            "finding_subtype": "secret_exposure_detected",
            "title": "Secret material exposed in packet capture",
            "subject_key": affected_subject_key,
            "subject_type": "service.socket" if service is not None else "host.ip",
            "protocol": protocol,
            "field": field,
            "kind": kind,
            "exposure_proof_id": proof_id,
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
            value = exposure.get(key)
            if value not in (None, "", []):
                payload[key] = value

        return make_observation(
            context=context,
            observation_type="finding.vulnerability_detected",
            subject_type="finding.vulnerability",
            subject_key=finding_key,
            payload=payload,
        )


def _is_tshark_metadata(metadata: Mapping[str, Any]) -> bool:
    return (
        str(metadata.get("schema_version") or "").strip() == "tshark.v1"
        or str(metadata.get("source_tool") or "").strip() == "tshark"
        or (
            "analysis_mode" in metadata
            and any(key in metadata for key in ("pcap", "protocols", "conversations", "secret_exposure"))
        )
    )


def _base_payload(
    metadata: Mapping[str, Any],
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    pcap = metadata.get("pcap")
    pcap_dict = pcap if isinstance(pcap, Mapping) else {}
    payload: dict[str, Any] = {
        "source": "tshark",
        "source_tool": "tshark",
    }
    analysis_mode = str(metadata.get("analysis_mode") or "").strip()
    if analysis_mode:
        payload["analysis_mode"] = analysis_mode
    for key in ("input_file", "artifact_sha256"):
        value = pcap_dict.get(key)
        if value not in (None, "", []):
            payload[f"pcap_{key}"] = value
    if evidence_refs:
        payload["evidence_refs"] = evidence_refs
    return payload


def _service_from_conversation(row: Mapping[str, Any]) -> tuple[str, str, str, int, str | None] | None:
    host = str(row.get("dst") or "").strip().lower()
    port = normalize_port(row.get("dst_port"))
    if not host or port is None:
        return None
    protocol = _transport_protocol_from_row(row)
    application_protocol = _application_protocol_from_row(row)
    if protocol is None:
        protocol = infer_transport_from_application_protocol(application_protocol)
    if protocol is None:
        return None
    return build_service_subject_key(host, protocol, port), host, protocol, port, application_protocol


def _service_from_secret_exposure(row: Mapping[str, Any]) -> tuple[str, str, str, int, str | None] | None:
    parsed = _parse_flow_key(row.get("flow_key"))
    if parsed is None:
        return None
    protocol, host, port, application_protocol = parsed
    return build_service_subject_key(host, protocol, port), host, protocol, port, application_protocol


def _parse_flow_key(value: Any) -> tuple[str, str, int, str | None] | None:
    flow_key = str(value or "").strip().lower()
    if "->" not in flow_key or ":" not in flow_key:
        return None
    left, right = flow_key.split("->", 1)
    left_protocol = left.split(":", 1)[0]
    protocol = normalize_transport_protocol(left_protocol, default=None)
    application_protocol = None
    if protocol is None:
        application_protocol = normalize_application_protocol(left_protocol)
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


def _transport_protocol_from_row(row: Mapping[str, Any]) -> str | None:
    for key in ("transport_protocol", "transport", "ip_proto", "network_protocol"):
        protocol = normalize_transport_protocol(row.get(key), default=None)
        if protocol is not None:
            return protocol
    return normalize_transport_protocol(row.get("protocol"), default=None)


def _application_protocol_from_row(row: Mapping[str, Any]) -> str | None:
    for key in ("application_protocol", "app_protocol", "service_name", "protocol"):
        protocol = normalize_application_protocol(row.get(key))
        if protocol is not None and normalize_transport_protocol(protocol, default=None) is None:
            return protocol
    return None


def _proof_id_from_exposure(exposure: Mapping[str, Any]) -> str:
    proof_parts = [
        str(exposure.get("pcap_artifact_sha256") or "").strip(),
        str(exposure.get("frame") or "").strip(),
        str(exposure.get("stream") or "").strip(),
        str(exposure.get("extraction_filter") or exposure.get("field") or "").strip(),
        str(exposure.get("proof_excerpt") or "").strip(),
    ]
    return "|".join(part for part in proof_parts if part)


def _has_safe_secret_exposure_proof(exposure: Mapping[str, Any]) -> bool:
    proof_excerpt = str(exposure.get("proof_excerpt") or "")
    fingerprint = str(exposure.get("fingerprint") or "")
    if _value_is_durable_masked(proof_excerpt) or fingerprint.startswith("hmac-sha256:"):
        return True
    proof_mode = str(exposure.get("proof_mode") or "").strip().lower()
    return proof_mode == "metadata_only" and bool(_proof_id_from_exposure(exposure))


def _host_subject_key(value: Any) -> str:
    host = str(value or "").strip().lower()
    if not host or " " in host:
        return ""
    return build_host_subject_key(host)


def _sanitize_semantic_observation_row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    row = dict(value)
    if _is_secret_exposure_semantic_row(row):
        return _sanitize_secret_exposure_semantic_row(row)
    if _contains_unredacted_secret_field(row):
        return None
    return row


def _is_secret_exposure_semantic_row(row: Mapping[str, Any]) -> bool:
    observation_type = str(row.get("observation_type") or "").strip().lower()
    if observation_type != "finding.vulnerability_detected":
        return False
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return False
    finding_subtype = str(payload.get("finding_subtype") or "").strip().lower()
    detector_id = str(payload.get("detector_id") or "").strip().lower()
    return (
        finding_subtype in {"secret_exposure_detected", "credential_exposure_detected"}
        or detector_id.startswith("tshark/secret_exposure/")
        or detector_id.startswith("tshark/credential_exposure")
    )


def _sanitize_secret_exposure_semantic_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    safe_proof_identity = _safe_hmac_proof_identity_fields(payload)
    sanitized_payload = _mask_mapping(payload, source="tshark_knowledge_semantic_row")
    sanitized_payload.update(safe_proof_identity)

    for key in tuple(sanitized_payload):
        field_name = _normalize_field_name(key)
        if field_name not in _SEMANTIC_PROOF_FIELD_NAMES:
            continue
        if not _semantic_proof_value_is_safe(sanitized_payload.get(key)):
            return None

    if not _has_safe_secret_exposure_proof(sanitized_payload):
        return None

    sanitized_row = dict(row)
    sanitized_row["payload"] = sanitized_payload
    sanitized_row = _rewrite_secret_exposure_semantic_identity(sanitized_row)
    if sanitized_row is None:
        return None
    if _contains_unredacted_secret_field(sanitized_row):
        return None
    return sanitized_row


def _rewrite_secret_exposure_semantic_identity(row: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    rewritten_payload = dict(payload)
    affected_subject_key = _affected_subject_key_from_secret_exposure_payload(rewritten_payload)
    if not affected_subject_key:
        return None

    field = str(rewritten_payload.get("field") or "").strip().lower()
    kind = str(rewritten_payload.get("kind") or "secret").strip().lower() or "secret"
    protocol = str(rewritten_payload.get("protocol") or "unknown").strip().lower() or "unknown"
    detector_leaf = field or _detector_leaf_from_secret_exposure_payload(rewritten_payload) or kind
    detector_id = f"tshark/secret_exposure/{detector_leaf}"
    proof_id = _safe_semantic_secret_proof_id(rewritten_payload)
    if not proof_id:
        return None

    try:
        finding_key = build_secret_exposure_finding_key(
            subject_key=affected_subject_key,
            detector_id=detector_id,
            protocol=protocol,
            exposure_kind=kind,
            proof_id=proof_id,
            flow_key=str(rewritten_payload.get("flow_key") or ""),
        )
    except ValueError:
        return None

    rewritten_payload["detector_id"] = detector_id
    rewritten_payload["subject_key"] = affected_subject_key
    rewritten_payload["subject_type"] = (
        "service.socket" if affected_subject_key.startswith("service.socket:") else "host.ip"
    )
    rewritten_payload["exposure_proof_id"] = proof_id
    rewritten = dict(row)
    rewritten["subject_type"] = "finding.vulnerability"
    rewritten["subject_key"] = finding_key
    rewritten["payload"] = rewritten_payload
    return rewritten


def _affected_subject_key_from_secret_exposure_payload(payload: Mapping[str, Any]) -> str:
    subject_key = str(payload.get("subject_key") or "").strip().lower()
    if subject_key.startswith("service.socket:") and _is_canonical_service_socket_key(subject_key):
        return subject_key
    if subject_key.startswith("host.ip:") and " " not in subject_key:
        return subject_key

    service = _service_from_secret_exposure(payload)
    if service is not None:
        return service[0]

    host_key = _host_subject_key(payload.get("dst") or payload.get("src"))
    if host_key:
        return host_key
    return ""


def _is_canonical_service_socket_key(value: str) -> bool:
    try:
        validate_subject_key_matches_type(subject_type="service.socket", subject_key=value)
    except ValueError:
        return False
    return True


def _detector_leaf_from_secret_exposure_payload(payload: Mapping[str, Any]) -> str:
    detector_id = str(payload.get("detector_id") or "").strip().lower()
    prefixes = (
        "tshark/secret_exposure/",
        "tshark/credential_exposure_detected/",
        "tshark/secret_exposure_detected/",
        "tshark/credential_exposure/",
    )
    for prefix in prefixes:
        if detector_id.startswith(prefix):
            return detector_id[len(prefix) :].strip("/")
    return ""


def _safe_semantic_secret_proof_id(payload: Mapping[str, Any]) -> str:
    fingerprint = str(payload.get("fingerprint") or "").strip()
    if fingerprint.startswith("hmac-sha256:"):
        return fingerprint

    for key in ("exposure_proof_id", "proof_id"):
        value = payload.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.startswith("hmac-sha256:"):
                return normalized

    return _proof_id_from_exposure(payload)


def _safe_hmac_proof_identity_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    safe_fields: dict[str, str] = {}
    for key in ("fingerprint", "exposure_proof_id", "proof_id"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized.startswith("hmac-sha256:"):
            safe_fields[key] = normalized
    return safe_fields


def _semantic_proof_value_is_safe(value: Any) -> bool:
    if value in (None, "", []):
        return True
    if isinstance(value, str):
        normalized = value.strip()
        return (
            not normalized
            or _REDACTED_MARKER in normalized
            or _value_is_durable_masked(normalized)
            or normalized.startswith("hmac-sha256:")
        )
    if isinstance(value, list):
        return all(_semantic_proof_value_is_safe(item) for item in value)
    return False


def _contains_unredacted_secret_field(value: Any, *, sensitive_context: bool = False) -> bool:
    if isinstance(value, Mapping):
        local_sensitive = sensitive_context or _mapping_has_sensitive_hint(value)
        for key, child in value.items():
            field_name = _normalize_field_name(key)
            if field_name in _SAFE_PROOF_FIELD_NAMES:
                continue
            field_is_sensitive = field_name in _SENSITIVE_VALUE_FIELD_NAMES
            value_is_sensitive = field_name in _RAW_VALUE_FIELD_NAMES and local_sensitive
            if (field_is_sensitive or value_is_sensitive) and not _value_is_redacted_or_empty(child):
                return True
            if _contains_unredacted_secret_field(
                child,
                sensitive_context=local_sensitive or field_is_sensitive,
            ):
                return True
        return False
    if isinstance(value, list):
        return any(
            _contains_unredacted_secret_field(item, sensitive_context=sensitive_context)
            for item in value
        )
    return False


def _contains_top_level_unredacted_secret_field(value: Mapping[str, Any]) -> bool:
    for key, child in value.items():
        field_name = _normalize_field_name(key)
        if field_name in _SAFE_PROOF_FIELD_NAMES:
            continue
        if field_name in _SENSITIVE_VALUE_FIELD_NAMES and not _value_is_redacted_or_empty(child):
            return True
    return False


def _mapping_has_sensitive_hint(value: Mapping[str, Any]) -> bool:
    for hint_key in ("field", "name", "kind"):
        hint = str(value.get(hint_key) or "").strip().lower()
        if any(token in hint for token in _SENSITIVE_HINT_TOKENS):
            return True
    return False


def _value_is_redacted_or_empty(value: Any) -> bool:
    if value in (None, "", []):
        return True
    if isinstance(value, str):
        normalized = value.strip()
        return (
            not normalized
            or _REDACTED_MARKER in normalized
            or _value_is_durable_masked(normalized)
            or normalized.startswith("hmac-sha256:")
        )
    if isinstance(value, Mapping):
        return not _contains_unredacted_secret_field(value)
    if isinstance(value, list):
        return all(_value_is_redacted_or_empty(item) for item in value)
    return False


def _normalize_field_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_")


def _value_is_durable_masked(value: Any) -> bool:
    return _DURABLE_MASK_MARKER in str(value or "")


def _mask_mapping(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    masked = mask_durable_secrets(dict(value), source=source)
    if not isinstance(masked, dict):
        return {}
    return _mask_tshark_secret_exposure_proofs(masked, source=source)


def _mask_tshark_secret_exposure_proofs(
    value: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    result = dict(value)
    if _looks_like_secret_exposure(result):
        result = _mask_secret_exposure_mapping(result, source=source)

    exposures = result.get("secret_exposure")
    if isinstance(exposures, list):
        result["secret_exposure"] = [
            _mask_secret_exposure_mapping(item, source=source)
            if isinstance(item, Mapping)
            else item
            for item in exposures
        ]
    return result


def _mask_secret_exposure_mapping(
    exposure: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    result = dict(exposure)
    proof = str(result.get("proof_excerpt") or "").strip()
    if not proof or _semantic_proof_value_is_safe(proof):
        return result

    masked = mask_durable_secrets(proof, source=f"{source}_proof_excerpt")
    if str(masked) == proof and _secret_exposure_has_sensitive_proof_context(result):
        contextual = mask_durable_secrets(
            {"credential": proof},
            source=f"{source}_proof_excerpt_context",
        )
        if isinstance(contextual, Mapping):
            masked = contextual.get("credential", "<DURABLE_SECRET_MASK:secret>")
        else:
            masked = "<DURABLE_SECRET_MASK:secret>"
    result["proof_excerpt"] = str(masked)
    return result


def _looks_like_secret_exposure(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in (
            "proof_excerpt",
            "fingerprint",
            "proof_mode",
            "extraction_filter",
        )
    ) and any(key in value for key in ("field", "kind", "detector_id"))


def _secret_exposure_has_sensitive_proof_context(value: Mapping[str, Any]) -> bool:
    kind = _normalize_field_name(value.get("kind"))
    field = _normalize_field_name(value.get("field"))
    detector_id = str(value.get("detector_id") or "").strip().lower()
    return (
        any(token in kind for token in _SENSITIVE_HINT_TOKENS)
        or kind in {"protocol_auth_argument", "authorization_header", "bearer_token"}
        or any(token in field for token in _SENSITIVE_HINT_TOKENS)
        or "command_parameter" in field
        or "auth_argument" in field
        or "secret_exposure" in detector_id
        or "credential_exposure" in detector_id
    )


def _load_json_payloads(text: str) -> list[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payloads: list[Any] = []
        for line in text.splitlines():
            raw_line = line.strip()
            if not raw_line or raw_line[0] not in "{[":
                continue
            try:
                payloads.append(json.loads(raw_line))
            except json.JSONDecodeError:
                continue
        return payloads
    return payload if isinstance(payload, list) else [payload]


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


__all__ = ["TsharkKnowledgeAdapter"]
