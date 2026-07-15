"""Deterministic adapter for Hydra weak-auth confirmation metadata.

This module converts Hydra's semantic weak-auth observations, or its parsed
tool metadata fallback, into durable confirmed finding observations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    default_port_for_application_protocol,
    normalize_application_protocol,
)

from ..contracts import ObservationCreate
from ..identity.canonical_keys import build_finding_vulnerability_key
from .base import AdapterContext
from .web_common import dedupe_observations, resolve_evidence_refs

_TOOL_NAME = "password_attacks.online_attacks.hydra"
_CAPABILITY_FAMILY = "credential_attack"
_SCHEMA_VERSION = "hydra.v1"
_DETECTOR_ID = "hydra/weak-auth"
class HydraKnowledgeAdapter:
    """Normalize Hydra execution metadata into confirmed weak-auth findings."""

    tool_names = (_TOOL_NAME,)
    capability_families: tuple[str, ...] = ()

    def supports(self, context: AdapterContext) -> bool:
        return context.source_tool_name() in self.tool_names

    def extract(self, context: AdapterContext) -> list[ObservationCreate]:
        semantic = self._extract_from_semantic_observations(context)
        if semantic:
            return semantic
        return self._extract_from_metadata(context, context.tool_metadata)

    def _extract_from_semantic_observations(self, context: AdapterContext) -> list[ObservationCreate]:
        observations: list[ObservationCreate] = []
        evidence_refs = resolve_evidence_refs(context)
        for row in context.semantic_observations:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("observation_type") or "") != "finding.vulnerability_confirmed":
                continue
            if str(row.get("subject_type") or "") != "finding.vulnerability":
                continue
            payload_raw = row.get("payload")
            if not isinstance(payload_raw, Mapping):
                continue
            payload = _safe_payload(payload_raw, evidence_refs=evidence_refs)
            if payload.get("detector_id") != _DETECTOR_ID:
                continue
            subject_key = str(row.get("subject_key") or "").strip().lower()
            if not subject_key.startswith("finding.vulnerability:"):
                subject_key = _finding_key_from_payload(payload)
            if not subject_key:
                continue
            observations.append(_make_confirmed_observation(
                context=context,
                subject_key=subject_key,
                payload=payload,
            ))
        return dedupe_observations(observations)

    def _extract_from_metadata(
        self,
        context: AdapterContext,
        metadata: Mapping[str, Any],
    ) -> list[ObservationCreate]:
        if not isinstance(metadata, Mapping):
            return []
        metadata = mask_durable_secrets(dict(metadata), source="hydra_knowledge_metadata")
        credentials = _list_value(metadata.get("credentials"))
        if not credentials:
            return []

        evidence_refs = resolve_evidence_refs(context)
        groups: dict[str, dict[str, Any]] = {}
        for item in credentials:
            credential = item if isinstance(item, Mapping) else {}
            service_name = _service_name(metadata, credential)
            host = credential.get("host") or _mapping_value(metadata, "target_info", "host")
            port = credential.get("port") or _mapping_value(metadata, "target_info", "port")
            port_value = _safe_int(port) or default_port_for_application_protocol(service_name)
            service_key = _service_key(host, port_value)
            if not service_key:
                continue
            group = groups.setdefault(service_key, {"service": service_name, "accounts": [], "count": 0})
            group["count"] += 1
            account = str(
                credential.get("account_identifier") or credential.get("username") or ""
            ).strip()
            if account and account not in group["accounts"]:
                group["accounts"].append(account)

        observations: list[ObservationCreate] = []
        for service_key, group in groups.items():
            payload = _base_payload(
                service_key=service_key,
                service_name=str(group.get("service") or ""),
                successful_login_count=int(group.get("count") or 0),
                account_identifiers=list(group.get("accounts") or []),
                evidence_refs=evidence_refs,
            )
            observations.append(_make_confirmed_observation(
                context=context,
                subject_key=build_finding_vulnerability_key(
                    subject_key=service_key,
                    detector_id=_DETECTOR_ID,
                ),
                payload=payload,
            ))
        return dedupe_observations(observations)


def _make_confirmed_observation(
    *,
    context: AdapterContext,
    subject_key: str,
    payload: Mapping[str, Any],
) -> ObservationCreate:
    return ObservationCreate(
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        engagement_id=context.engagement_id,
        task_id=context.task_id,
        source_execution_id=context.source_execution_id,
        ingestion_run_id=context.ingestion_run_id,
        observation_type="finding.vulnerability_confirmed",
        subject_type="finding.vulnerability",
        subject_key=subject_key,
        assertion_level="confirmed",
        payload=dict(payload),
        observation_metadata={
            "source_kind": "deterministic",
            "extractor_family": _CAPABILITY_FAMILY,
            "extractor_version": _SCHEMA_VERSION,
            "durable_masking_applied": True,
        },
    )


def _safe_payload(
    payload: Mapping[str, Any],
    *,
    evidence_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    masked = mask_durable_secrets(dict(payload), source="hydra_knowledge_semantic")
    if not isinstance(masked, Mapping):
        return {}
    result = dict(masked)
    result.setdefault("source", "hydra")
    result.setdefault("detector_id", _DETECTOR_ID)
    result.setdefault("finding_subtype", "credential_compromise_confirmed")
    result.setdefault("confidence", "confirmed")
    result["durable_masking_applied"] = True
    if evidence_refs and not isinstance(result.get("evidence_refs"), list):
        result["evidence_refs"] = list(evidence_refs)
    return result


def _base_payload(
    *,
    service_key: str,
    service_name: str,
    successful_login_count: int,
    account_identifiers: list[str],
    evidence_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    title_service = service_name.upper() if service_name else "service"
    payload: dict[str, Any] = {
        "source": "hydra",
        "detector_id": _DETECTOR_ID,
        "title": f"Weak authentication confirmed on {title_service}",
        "finding_subtype": "credential_compromise_confirmed",
        "confidence": "confirmed",
        "subject_type": "service.socket",
        "subject_key": service_key,
        "service": service_name,
        "auth_protocol": service_name,
        "successful_login_count": successful_login_count,
        "durable_masking_applied": True,
    }
    if account_identifiers:
        payload["account_identifier"] = account_identifiers[0]
        payload["account_identifiers"] = account_identifiers[:20]
    if evidence_refs:
        payload["evidence_refs"] = list(evidence_refs)
    masked = mask_durable_secrets(payload, source="hydra_knowledge_metadata_payload")
    return dict(masked) if isinstance(masked, Mapping) else payload


def _finding_key_from_payload(payload: Mapping[str, Any]) -> str:
    subject_key = str(payload.get("subject_key") or "").strip().lower()
    if not subject_key.startswith("service.socket:"):
        return ""
    try:
        return build_finding_vulnerability_key(
            subject_key=subject_key,
            detector_id=str(payload.get("detector_id") or _DETECTOR_ID),
        )
    except ValueError:
        return ""


def _service_key(host: Any, port: Any) -> str:
    host_text = str(host or "").strip()
    port_value = _safe_int(port)
    if not host_text or not port_value:
        return ""
    try:
        return build_service_socket_key(ip=host_text, protocol="tcp", port=port_value)
    except ValueError:
        return ""


def _service_name(metadata: Mapping[str, Any], credential: Mapping[str, Any]) -> str:
    for value in (
        credential.get("service"),
        credential.get("protocol"),
        _mapping_value(metadata, "attack_info", "service"),
        _mapping_value(metadata, "attack_info", "protocol"),
        metadata.get("protocol"),
    ):
        normalized = normalize_application_protocol(value)
        if normalized:
            return normalized
    return ""


def _mapping_value(metadata: Mapping[str, Any], parent: str, key: str) -> Any:
    value = metadata.get(parent)
    if not isinstance(value, Mapping):
        return None
    return value.get(key)


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _safe_int(value: Any) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > 65535:
        return None
    return parsed


__all__ = ["HydraKnowledgeAdapter"]
