"""Structured-output resolution and mapping for candidate extraction.

Scope:
- Resolve structured payload from LLM response.
- Build usage summary and map candidate rows to ObservationCreate.

Boundary:
- No evidence loading or LLM invocation orchestration.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from agent.providers.llm.core.base import LLMResponse
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.semantic.service_identity import (
    build_service_socket_key,
    default_port_for_application_protocol,
    infer_transport_from_application_protocol,
    normalize_application_protocol,
    normalize_transport_protocol,
    parse_service_socket_key,
)

from ..identity.canonical_keys import (
    build_finding_vulnerability_key,
    build_host_ip_key,
    build_web_url_key,
)
from ..contracts import (
    ObservationCreate,
    build_subject_key,
    normalize_observation_create,
)
from backend.services.usage_tracking.models import UsageData
from backend.services.usage_tracking.pricing import calculate_cost, pricing_status_for_usage

from .contracts import (
    CandidateExtractionPolicyDecision,
    CandidateExtractionRequest,
    CandidateExtractionResult,
    CandidateExtractionUsageSummary,
)
from .vulnerability_rules import (
    is_vulnerability_observation_type,
    normalize_vulnerability_payload,
    parse_vulnerability_confidence,
)

DEFAULT_VULNERABILITY_MIN_CONFIDENCE = 0.80
_IPV4_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SUBJECT_KEY_UNSAFE_PATTERN = re.compile(r"[^a-zA-Z0-9._:/@#-]+")


def build_usage_summary(usage: UsageData | None) -> CandidateExtractionUsageSummary:
    """Build a portable usage summary from provider usage envelope."""
    if usage is None:
        return CandidateExtractionUsageSummary()
    return CandidateExtractionUsageSummary(
        input_tokens=int(usage.prompt_tokens or 0),
        output_tokens=int(usage.completion_tokens or 0),
        total_tokens=int(usage.total_tokens or 0),
        estimated_cost_usd=float(calculate_cost(usage)),
        pricing_status=pricing_status_for_usage(usage),
        provider=str(usage.provider or "openai"),
        model=str(usage.model or "gpt-5-mini"),
        api_surface=str(usage.api_surface or "unknown"),
        cached_tokens=int(usage.cached_tokens or 0),
        reasoning_tokens=int(usage.reasoning_tokens or 0),
        provider_usage_components=(
            usage.provider_usage_components.to_dict()
            if usage.provider_usage_components is not None
            else None
        ),
    )


def resolve_structured_payload(llm_response: LLMResponse) -> dict[str, Any] | None:
    """Resolve structured output dict from model response envelope."""
    structured = getattr(llm_response, "structured_output", None)
    if isinstance(structured, dict):
        return structured
    raw_content = str(getattr(llm_response, "content", "") or "").strip()
    if not raw_content:
        return None
    try:
        parsed = json.loads(raw_content)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def map_structured_payload(
    *,
    request: CandidateExtractionRequest,
    user_id: int,
    payload: Mapping[str, Any],
    bounded_evidence: Sequence[Mapping[str, Any]],
    durable_masking_applied: bool,
    usage_summary: CandidateExtractionUsageSummary,
    minimum_vulnerability_confidence: float = DEFAULT_VULNERABILITY_MIN_CONFIDENCE,
    enable_vulnerability_candidates: bool = True,
) -> CandidateExtractionResult:
    """Map structured candidate payload to normalized contract result."""
    candidate_rows = payload.get("candidate_observations")
    analyst_notes = payload.get("analyst_notes")
    no_signal = bool(payload.get("no_signal"))

    if not isinstance(candidate_rows, list):
        candidate_rows = []
    if not isinstance(analyst_notes, list):
        analyst_notes = []

    available_evidence_ids = {
        str(item.get("evidence_archive_id") or "")
        for item in bounded_evidence
        if str(item.get("evidence_archive_id") or "").strip()
    }
    normalized_observations: list[ObservationCreate] = []
    evidence_ids_used: list[str] = []
    vulnerability_drop_reasons: dict[str, int] = {}
    for row in candidate_rows:
        if not isinstance(row, Mapping):
            continue
        observation_type = str(row.get("observation_type") or "").strip()
        subject_type = str(row.get("subject_type") or "").strip()
        subject_key_hint = str(row.get("subject_key_hint") or "").strip()
        if not observation_type or not subject_type or not subject_key_hint:
            continue
        durable_subject_key_hint = _mask_durable_subject_key_hint(
            subject_key_hint,
            source="knowledge_candidate.subject_key_hint",
        )
        is_vulnerability = is_vulnerability_observation_type(observation_type)

        evidence_refs_raw = row.get("evidence_refs")
        if not isinstance(evidence_refs_raw, list):
            if is_vulnerability:
                vulnerability_drop_reasons["missing_vulnerability_evidence_refs"] = (
                    vulnerability_drop_reasons.get("missing_vulnerability_evidence_refs", 0) + 1
                )
            continue
        normalized_refs: list[dict[str, str]] = []
        for ref in evidence_refs_raw:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("evidence_archive_id") or "").strip()
            excerpt = str(ref.get("excerpt") or "").strip()
            if not evidence_id or not excerpt:
                continue
            if evidence_id not in available_evidence_ids:
                continue
            durable_excerpt = _mask_durable_text(
                excerpt,
                source="knowledge_candidate.evidence_ref_excerpt",
            )
            normalized_refs.append(
                {
                    "evidence_archive_id": evidence_id,
                    "excerpt": durable_excerpt,
                }
            )
            evidence_ids_used.append(evidence_id)
        if not normalized_refs:
            if is_vulnerability:
                vulnerability_drop_reasons["missing_vulnerability_evidence_refs"] = (
                    vulnerability_drop_reasons.get("missing_vulnerability_evidence_refs", 0) + 1
                )
            continue

        attributes = _normalize_attributes(row.get("attributes"))
        durable_attributes = _mask_durable_mapping(
            attributes,
            source="knowledge_candidate.attributes",
        )
        normalized_vulnerability_payload = None
        vulnerability_confidence_source = "none"
        vulnerability_drop_reason: str | None = None

        if is_vulnerability:
            if not enable_vulnerability_candidates:
                vulnerability_drop_reasons["vulnerability_candidates_disabled"] = (
                    vulnerability_drop_reasons.get("vulnerability_candidates_disabled", 0) + 1
                )
                continue
            raw_vulnerability_payload = row.get("vulnerability")
            if raw_vulnerability_payload is not None:
                normalized_vulnerability_payload = normalize_vulnerability_payload(raw_vulnerability_payload)
                if normalized_vulnerability_payload is None:
                    vulnerability_drop_reason = "invalid_vulnerability_payload"
                else:
                    normalized_vulnerability_payload = _mask_durable_mapping(
                        normalized_vulnerability_payload,
                        source="knowledge_candidate.vulnerability",
                    )
            confidence, confidence_source = parse_vulnerability_confidence(row=row, is_vulnerability=True)
            vulnerability_confidence_source = confidence_source
            if confidence is None and vulnerability_drop_reason is None:
                vulnerability_drop_reason = "invalid_vulnerability_confidence"
            if (
                confidence is not None
                and confidence < minimum_vulnerability_confidence
                and vulnerability_drop_reason is None
            ):
                vulnerability_drop_reason = "below_vulnerability_confidence_threshold"
            if vulnerability_drop_reason is not None:
                vulnerability_drop_reasons[vulnerability_drop_reason] = (
                    vulnerability_drop_reasons.get(vulnerability_drop_reason, 0) + 1
                )
                continue
            normalized_confidence = confidence
        else:
            confidence = row.get("confidence")
            try:
                normalized_confidence = float(confidence)
            except (TypeError, ValueError):
                normalized_confidence = 0.0

        rationale = _mask_durable_text(
            str(row.get("rationale") or "").strip(),
            source="knowledge_candidate.rationale",
        )
        mapped_payload = {
            "attributes": durable_attributes,
            "confidence": max(0.0, min(1.0, normalized_confidence)),
            "rationale": rationale,
            "evidence_refs": normalized_refs,
        }
        if normalized_vulnerability_payload is not None:
            mapped_payload["vulnerability"] = normalized_vulnerability_payload
        if is_vulnerability:
            mapped_payload["confidence_metadata"] = {
                "is_vulnerability": True,
                "confidence_source": vulnerability_confidence_source,
                "minimum_threshold": float(minimum_vulnerability_confidence),
                "drop_reason": None,
            }
        mapped_subject_type = subject_type
        mapped_subject_key = build_subject_key(
            subject_type=subject_type,
            raw_key=durable_subject_key_hint,
        )
        if is_vulnerability:
            (
                mapped_subject_type,
                mapped_subject_key,
                canonical_subject_key,
            ) = _canonicalize_vulnerability_identity(
                row=row,
                original_subject_type=subject_type,
                subject_key_hint=durable_subject_key_hint,
                normalized_vulnerability_payload=normalized_vulnerability_payload,
                attributes=durable_attributes,
            )
            mapped_payload["subject_key"] = canonical_subject_key

        observation = ObservationCreate(
            user_id=int(user_id),
            engagement_id=int(request.engagement_id),
            task_id=request.task_id,
            source_execution_id=str(request.source_execution_id),
            ingestion_run_id=str(request.ingestion_run_id),
            observation_type=observation_type,
            subject_type=mapped_subject_type,
            subject_key=mapped_subject_key,
            assertion_level="candidate",
            payload=mapped_payload,
            observation_metadata={
                "source_kind": "llm_candidate",
                "extractor_family": str(request.extractor_family),
                "extractor_version": str(request.extractor_version),
                "extraction_mode": str(request.extraction_mode),
                "durable_masking_applied": bool(durable_masking_applied),
                "audit_summary": {
                    "llm_status": "succeeded",
                    "evidence_item_count": len(bounded_evidence),
                    "candidate_count": len(candidate_rows),
                    "vulnerability_mapping": {
                        "is_vulnerability": True,
                        "confidence_source": vulnerability_confidence_source,
                        "minimum_confidence_threshold": float(minimum_vulnerability_confidence),
                        "drop_reason": None,
                    }
                    if is_vulnerability
                    else None,
                },
            },
        )
        try:
            normalized_observations.append(normalize_observation_create(observation))
        except ValueError:
            continue

    notes: list[str] = []
    for item in analyst_notes:
        if not isinstance(item, Mapping):
            continue
        note = str(item.get("note") or "").strip()
        if note:
            notes.append(
                _mask_durable_text(
                    note,
                    source="knowledge_candidate.analyst_note",
                )
            )

    unique_evidence_ids_used = tuple(dict.fromkeys(evidence_ids_used))
    if normalized_observations:
        policy_decision = None
        if vulnerability_drop_reasons:
            policy_decision = CandidateExtractionPolicyDecision(
                action="run",
                reason="candidates_extracted",
                policy_metadata={
                    "bounded_evidence_count": len(bounded_evidence),
                    "vulnerability_drop_reasons": dict(vulnerability_drop_reasons),
                },
            )
        return CandidateExtractionResult.succeeded(
            observations=normalized_observations,
            analyst_notes=notes,
            evidence_archive_ids_used=unique_evidence_ids_used,
            durable_masking_applied=durable_masking_applied,
            usage_summary=usage_summary,
            policy_decision=policy_decision,
        )

    reason = "model_returned_no_signal" if no_signal else "no_valid_candidate_observations"
    policy_metadata: dict[str, Any] = {"bounded_evidence_count": len(bounded_evidence)}
    if vulnerability_drop_reasons:
        policy_metadata["vulnerability_drop_reasons"] = vulnerability_drop_reasons
    return CandidateExtractionResult.no_signal_result(
        reason=reason,
        policy_decision=CandidateExtractionPolicyDecision(
            action="no_signal",
            reason=reason,
            policy_metadata=policy_metadata,
        ),
        evidence_archive_ids_used=unique_evidence_ids_used,
        durable_masking_applied=durable_masking_applied,
        usage_summary=usage_summary,
    )


def _normalize_attributes(raw_attributes: Any) -> dict[str, Any]:
    """Normalize model attributes into canonical dictionary payload."""
    if isinstance(raw_attributes, Mapping):
        return {str(key): value for key, value in raw_attributes.items() if str(key).strip()}
    if isinstance(raw_attributes, list):
        normalized: dict[str, Any] = {}
        for item in raw_attributes:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            # Keep value shape permissive at ingest time; schema constrains generation.
            normalized[key] = item.get("value")
        return normalized
    return {}


def _mask_durable_text(value: str, *, source: str) -> str:
    """Mask reusable secrets in text before durable candidate persistence."""
    return str(mask_durable_secrets(value, source=source) or "")


def _mask_durable_subject_key_hint(value: str, *, source: str) -> str:
    """Mask reusable secrets in subject-key hints using canonical-key-safe text."""
    masked = _mask_durable_text(value, source=source)
    if masked == str(value or ""):
        return masked
    masked = masked.replace("<DURABLE_SECRET_MASK:", "durable_secret_mask_")
    masked = masked.replace(">", "")
    safe = _SUBJECT_KEY_UNSAFE_PATTERN.sub("-", masked).strip("-")
    return safe or "durable_secret_mask"


def _mask_durable_mapping(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Mask reusable secrets in mapping payloads before durable persistence."""
    masked = mask_durable_secrets(dict(value), source=source)
    return dict(masked) if isinstance(masked, Mapping) else {}


def _canonicalize_vulnerability_identity(
    *,
    row: Mapping[str, Any],
    original_subject_type: str,
    subject_key_hint: str,
    normalized_vulnerability_payload: Mapping[str, Any] | None,
    attributes: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Normalize vulnerability candidates to finding.vulnerability identity keys."""
    canonical_subject_key = _infer_linkable_subject_key(
        subject_key_hint=subject_key_hint,
        attributes=attributes,
    ) or build_subject_key(subject_type=original_subject_type, raw_key=subject_key_hint)
    detector_id = _infer_vulnerability_detector_id(
        row=row,
        subject_key_hint=subject_key_hint,
        normalized_vulnerability_payload=normalized_vulnerability_payload,
        attributes=attributes,
    )
    finding_key = build_finding_vulnerability_key(
        subject_key=canonical_subject_key,
        detector_id=detector_id,
    )
    return ("finding.vulnerability", finding_key, canonical_subject_key)


def _infer_linkable_subject_key(
    *,
    subject_key_hint: str,
    attributes: Mapping[str, Any],
) -> str | None:
    normalized_hint = str(subject_key_hint or "").strip().lower()
    if not normalized_hint:
        return None

    if normalized_hint.startswith("finding.vulnerability:"):
        tail = normalized_hint[len("finding.vulnerability:") :]
        if ":" in tail:
            candidate = tail.rsplit(":", 1)[0]
            parsed = _parse_subject_key_candidate(candidate)
            if parsed:
                return parsed

    if ":" in normalized_hint:
        first, remainder = normalized_hint.split(":", 1)
        if first and remainder:
            parsed_remainder = _parse_subject_key_candidate(remainder)
            if parsed_remainder:
                return parsed_remainder

    parsed_hint = _parse_subject_key_candidate(normalized_hint)
    if parsed_hint:
        return parsed_hint

    target_ip = str(attributes.get("target_ip") or "").strip()
    raw_protocol = str(attributes.get("protocol") or "tcp").strip().lower() or "tcp"
    protocol = normalize_transport_protocol(raw_protocol, default=None)
    if protocol is None:
        app_protocol = normalize_application_protocol(raw_protocol)
        protocol = infer_transport_from_application_protocol(app_protocol)
    target_port = attributes.get("target_port")
    if target_port is None:
        target_port = default_port_for_application_protocol(raw_protocol)
    if target_ip and target_port is not None and protocol is not None:
        try:
            return build_service_socket_key(ip=target_ip, protocol=protocol, port=target_port)
        except ValueError:
            pass

    return None


def _parse_subject_key_candidate(candidate: str) -> str | None:
    value = str(candidate or "").strip().lower()
    if not value:
        return None

    if value.startswith("service.socket:"):
        parsed = parse_service_socket_key(value)
        if parsed is not None:
            return parsed.subject_key
        tail = value[len("service.socket:") :]
        if ":" in tail:
            possible_tail, _ = tail.rsplit(":", 1)
            parsed = parse_service_socket_key(f"service.socket:{possible_tail}")
            if parsed is not None:
                return parsed.subject_key
        return None

    if value.startswith("host.ip:"):
        host_ip = value[len("host.ip:") :]
        if ":" in host_ip:
            maybe_ipv4, _ = host_ip.rsplit(":", 1)
            if _IPV4_PATTERN.fullmatch(maybe_ipv4):
                host_ip = maybe_ipv4
        try:
            return build_subject_key(subject_type="host.ip", raw_key=host_ip)
        except ValueError:
            return None

    if value.startswith("host.dns:"):
        host_dns = value[len("host.dns:") :]
        try:
            return build_subject_key(subject_type="host.dns", raw_key=host_dns)
        except ValueError:
            return None

    if value.startswith("web.url:"):
        web_url = value[len("web.url:") :]
        try:
            return build_web_url_key(web_url)
        except ValueError:
            return None

    if value.startswith(("http://", "https://")):
        try:
            return build_web_url_key(value)
        except ValueError:
            return None

    if _IPV4_PATTERN.fullmatch(value):
        try:
            return build_host_ip_key(value)
        except ValueError:
            return None

    return None


def _infer_vulnerability_detector_id(
    *,
    row: Mapping[str, Any],
    subject_key_hint: str,
    normalized_vulnerability_payload: Mapping[str, Any] | None,
    attributes: Mapping[str, Any],
) -> str:
    vulnerability_id = str((normalized_vulnerability_payload or {}).get("id") or "").strip()
    if vulnerability_id:
        return vulnerability_id

    detector_from_attributes = str(attributes.get("detector_id") or "").strip()
    if detector_from_attributes:
        return detector_from_attributes

    normalized_hint = str(subject_key_hint or "").strip().lower()
    if normalized_hint.startswith("finding.vulnerability:"):
        tail = normalized_hint[len("finding.vulnerability:") :]
        if ":" in tail:
            detector = tail.rsplit(":", 1)[1].strip()
            if detector:
                return detector

    if ":" in normalized_hint:
        candidate_detector, candidate_subject = normalized_hint.split(":", 1)
        if candidate_detector and _parse_subject_key_candidate(candidate_subject):
            return candidate_detector

    detector_from_row = str(row.get("detector_id") or "").strip()
    if detector_from_row:
        return detector_from_row

    vulnerability_title = str((normalized_vulnerability_payload or {}).get("title") or "").strip()
    if vulnerability_title:
        return vulnerability_title

    attribute_title = str(attributes.get("title") or "").strip()
    if attribute_title:
        return attribute_title

    return "llm-candidate"


__all__ = ["build_usage_summary", "map_structured_payload", "resolve_structured_payload"]
