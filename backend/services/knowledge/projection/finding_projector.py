"""Finding projector for deterministic tenant/user-scoped knowledge_findings upserts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import KnowledgeAsset, KnowledgeFinding, KnowledgeService
from ..evidence_refs import normalize_canonical_evidence_refs
from ..identity.merge_rules import merge_confidence, normalize_confidence
from ..identity_service import IdentityMergeDecision, ResolvedIdentityObservation
from ..severity_policy import normalize_severity


@dataclass(frozen=True)
class FindingUpsertResult:
    """Result envelope for one finding upsert call."""

    row: KnowledgeFinding
    inserted: bool


class FindingProjector:
    """Upsert durable finding rows from identity merge decisions."""

    def upsert(
        self,
        *,
        db: Session,
        user_id: int,
        decision: IdentityMergeDecision,
        merged_state: Mapping[str, Any],
        resolved_observations: Iterable[ResolvedIdentityObservation],
        asset_key_to_id: Mapping[str, str],
        service_key_to_id: Mapping[str, str],
        engagement_id: int | None = None,
        tenant_id: int,
    ) -> FindingUpsertResult:
        existing = self._resolve_existing_row(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            finding_key=decision.identity_key,
        )
        inserted = existing is None
        row = existing or KnowledgeFinding(
            tenant_id=tenant_id,
            user_id=int(user_id),
            engagement_id=engagement_id,
            finding_key=decision.identity_key,
            finding_type=self._finding_type_from_key(decision.identity_key),
            subject_type=self._subject_type_from_key(decision.identity_key),
            subject_key=decision.identity_key,
            first_seen_at=decision.first_seen_at,
            last_seen_at=decision.last_seen_at,
        )

        observations = list(resolved_observations)
        representative = observations[-1] if observations else None
        metadata = dict(merged_state.get("metadata") or {})
        authority = dict(metadata.get("authority") or {})
        authority_state = self._resolve_authority_state(observations)
        authority.update(authority_state)
        metadata["authority"] = authority
        state = dict(metadata.get("state") or {})
        inferred_subject_key = self._infer_subject_key(decision.identity_key, representative)
        inferred_subject_type = self._subject_type_from_subject_key(inferred_subject_key) or row.subject_type
        asset_id, service_id = self._resolve_subject_links(
            db=db,
            tenant_id=tenant_id,
            user_id=user_id,
            subject_key=inferred_subject_key,
            payload=representative.payload if representative is not None else {},
            asset_key_to_id=asset_key_to_id,
            service_key_to_id=service_key_to_id,
        )

        row.finding_type = self._finding_type_from_key(decision.identity_key)
        row.tenant_id = int(tenant_id)
        row.subject_type = inferred_subject_type
        row.subject_key = inferred_subject_key
        row.asset_id = asset_id
        row.service_id = service_id
        row.title = self._resolve_title(existing=row if not inserted else None, representative=representative)
        row.severity = normalize_severity(state.get("severity")) or normalize_severity(row.severity)
        row.status = self._resolve_status(
            existing_status=row.status,
            source_observation_types=decision.source_observation_types,
            authority=authority_state,
        )
        row.assertion_level = self._resolve_assertion_level(
            existing_assertion_level=row.assertion_level,
            source_observation_types=decision.source_observation_types,
            authority=authority_state,
        )
        row.confidence = self._resolve_projected_confidence(
            existing_confidence=existing.confidence if existing is not None else None,
            merged_confidence=merged_state.get("confidence"),
            representative=representative,
            authority=authority_state,
        )
        row.first_seen_at = merged_state.get("first_seen_at")
        row.last_seen_at = merged_state.get("last_seen_at")
        evidence_refs = normalize_canonical_evidence_refs(metadata.get("evidence_refs"), strict=False)
        metadata["evidence_refs"] = evidence_refs
        durable_masking_applied = self._resolve_durable_masking_applied(
            existing_value=metadata.get("durable_masking_applied"),
            observations=observations,
        )
        if durable_masking_applied is not None:
            metadata["durable_masking_applied"] = durable_masking_applied
        row.evidence_summary = {"evidence_refs": evidence_refs}
        row.finding_metadata = metadata

        if inserted:
            db.add(row)
        db.flush()
        return FindingUpsertResult(row=row, inserted=inserted)

    @staticmethod
    def _resolve_existing_row(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        finding_key: str,
    ) -> KnowledgeFinding | None:
        return db.execute(
            select(KnowledgeFinding).where(
                KnowledgeFinding.tenant_id == int(tenant_id),
                KnowledgeFinding.user_id == int(user_id),
                KnowledgeFinding.finding_key == str(finding_key),
            )
        ).scalar_one_or_none()

    @staticmethod
    def _finding_type_from_key(finding_key: str) -> str:
        prefix = str(finding_key or "").split(":", 1)[0].strip().lower()
        return prefix or "finding.unknown"

    @staticmethod
    def _subject_type_from_key(finding_key: str) -> str:
        prefix = str(finding_key or "").split(":", 1)[0].strip().lower()
        return prefix or "finding.instance"

    @staticmethod
    def _subject_type_from_subject_key(subject_key: str) -> str:
        return str(subject_key or "").split(":", 1)[0].strip().lower()

    @staticmethod
    def _infer_subject_key(
        finding_key: str,
        representative: ResolvedIdentityObservation | None,
    ) -> str:
        if representative is not None:
            payload_subject = str(representative.payload.get("subject_key") or "").strip().lower()
            if payload_subject:
                return payload_subject
        key = str(finding_key or "").strip().lower()
        if key.startswith("finding.vulnerability:"):
            tail = key[len("finding.vulnerability:"):]
            if ":" in tail:
                return tail.rsplit(":", 1)[0]
        return key

    @staticmethod
    def _resolve_subject_links(
        *,
        db: Session,
        tenant_id: int,
        user_id: int,
        subject_key: str,
        payload: Mapping[str, Any],
        asset_key_to_id: Mapping[str, str],
        service_key_to_id: Mapping[str, str],
    ) -> tuple[str | None, str | None]:
        subject = str(subject_key or "").strip().lower()
        if subject.startswith("service.socket:"):
            service_id = service_key_to_id.get(subject)
            if not service_id:
                service_id = db.execute(
                    select(KnowledgeService.id).where(
                        KnowledgeService.tenant_id == int(tenant_id),
                        KnowledgeService.user_id == int(user_id),
                        KnowledgeService.service_key == subject,
                    )
                ).scalar_one_or_none()
            service_id_str = str(service_id) if service_id is not None else None
            if service_id_str:
                asset_id = db.execute(
                    select(KnowledgeService.asset_id).where(
                        KnowledgeService.id == service_id,
                        KnowledgeService.tenant_id == int(tenant_id),
                        KnowledgeService.user_id == int(user_id),
                    )
                ).scalar_one_or_none()
                return (str(asset_id) if asset_id is not None else None, service_id_str)
            return None, None

        if subject.startswith("host.ip:") or subject.startswith("host.dns:"):
            asset_id = asset_key_to_id.get(subject)
            if not asset_id:
                asset_id = db.execute(
                    select(KnowledgeAsset.id).where(
                        KnowledgeAsset.tenant_id == int(tenant_id),
                        KnowledgeAsset.user_id == int(user_id),
                        KnowledgeAsset.asset_key == subject,
                    )
                ).scalar_one_or_none()
            return (str(asset_id) if asset_id is not None else None, None)

        target_ip = str(payload.get("target_ip") or "").strip().lower()
        if target_ip:
            host_key = f"host.ip:{target_ip}"
            asset_id = asset_key_to_id.get(host_key)
            if not asset_id:
                asset_id = db.execute(
                    select(KnowledgeAsset.id).where(
                        KnowledgeAsset.tenant_id == int(tenant_id),
                        KnowledgeAsset.user_id == int(user_id),
                        KnowledgeAsset.asset_key == host_key,
                    )
                ).scalar_one_or_none()
            return (str(asset_id) if asset_id is not None else None, None)
        return None, None

    @staticmethod
    def _resolve_title(
        *,
        existing: KnowledgeFinding | None,
        representative: ResolvedIdentityObservation | None,
    ) -> str | None:
        if representative is None:
            return existing.title if existing is not None else None
        payload = dict(representative.payload or {})
        title = str(payload.get("title") or "").strip()
        if title:
            return title
        # Preserve a previously-set title before falling back to detector_id
        if existing is not None and existing.title:
            return existing.title
        detector = str(payload.get("detector_id") or "").strip()
        if detector:
            return detector
        return None

    @staticmethod
    def _resolve_status(
        *,
        existing_status: str | None,
        source_observation_types: set[str],
        authority: Mapping[str, Any] | None = None,
    ) -> str | None:
        if bool((authority or {}).get("candidate_only")):
            return "candidate"
        types = {str(item or "").strip().lower() for item in source_observation_types}
        if "finding.exploit_succeeded" in types:
            return "exploited"
        if "finding.vulnerability_confirmed" in types:
            return "confirmed"
        if "finding.vulnerability_detected" in types:
            return "open"
        return existing_status

    @staticmethod
    def _resolve_assertion_level(
        *,
        existing_assertion_level: str | None,
        source_observation_types: set[str],
        authority: Mapping[str, Any] | None = None,
    ) -> str | None:
        if bool((authority or {}).get("candidate_only")):
            return "candidate"
        types = {str(item or "").strip().lower() for item in source_observation_types}
        if "finding.exploit_succeeded" in types:
            return "exploited"
        if "finding.vulnerability_confirmed" in types:
            return "confirmed"
        if "finding.vulnerability_detected" in types:
            return "observed"
        return existing_assertion_level

    @staticmethod
    def _resolve_authority_state(
        observations: list[ResolvedIdentityObservation],
    ) -> dict[str, Any]:
        source_kinds = {
            str((item.observation_metadata or {}).get("source_kind") or "").strip().lower()
            for item in observations
            if str((item.observation_metadata or {}).get("source_kind") or "").strip()
        }
        assertion_levels = {
            str(item.assertion_level or "").strip().lower()
            for item in observations
            if str(item.assertion_level or "").strip()
        }
        has_llm_candidate = "llm_candidate" in source_kinds or "candidate" in assertion_levels
        has_authoritative = bool(source_kinds.intersection({"deterministic", "native_emitter"})) or bool(
            assertion_levels.intersection({"observed", "confirmed", "exploited"})
        )
        candidate_only = bool(has_llm_candidate and not has_authoritative)
        source_kind = "mixed" if has_llm_candidate and has_authoritative else (
            "llm_candidate" if has_llm_candidate else "deterministic"
        )
        return {
            "source_kind": source_kind,
            "candidate_only": candidate_only,
        }

    @staticmethod
    def _resolve_durable_masking_applied(
        *,
        existing_value: Any,
        observations: list[ResolvedIdentityObservation],
    ) -> bool | None:
        if any(
            item.payload.get("durable_masking_applied") is True
            or (item.observation_metadata or {}).get("durable_masking_applied") is True
            for item in observations
        ):
            return True
        if isinstance(existing_value, bool):
            return existing_value
        if any(
            item.payload.get("durable_masking_applied") is False
            or (item.observation_metadata or {}).get("durable_masking_applied") is False
            for item in observations
        ):
            return False
        return None

    @staticmethod
    def _resolve_projected_confidence(
        *,
        existing_confidence: Any,
        merged_confidence: Any,
        representative: ResolvedIdentityObservation | None,
        authority: Mapping[str, Any] | None = None,
    ) -> str | None:
        if bool((authority or {}).get("candidate_only")):
            projected = merge_confidence(existing_confidence, merged_confidence)
            if representative is not None:
                projected = merge_confidence(projected, (representative.payload or {}).get("confidence"))
            return projected
        return normalize_confidence(merged_confidence)
