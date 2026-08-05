"""Deterministic projection service for durable read-model upserts.

Scope:
- Consume canonical observations only.
- Resolve identities through KnowledgeIdentityService.
- Route per-domain upserts to dedicated projectors.
- Provide retry-aware projection with rollback-safe attempts.

Boundary:
- No raw-artifact parsing.
- No adapter/extractor logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.core.time_utils import to_utc
from ...models import Engagement, KnowledgeAsset, KnowledgeService, KnowledgeWebPath
from .identity.merge_rules import (
    merge_confidence_with_corroboration,
    merge_evidence_refs,
    merge_rich_details,
    merge_state_with_contradictions,
)
from .projection import (
    AssetProjector,
    EngagementLinkProjector,
    FindingProjector,
    RelationshipProjector,
    ServiceProjector,
    WebPathProjector,
)
from .contracts import ObservationCreate
from .identity_service import (
    IdentityMergeDecision,
    KnowledgeIdentityService,
    ResolvedIdentityObservation,
)


@dataclass(frozen=True)
class ProjectionResult:
    """Stable projection counters for one deterministic projection pass."""

    asset_upsert_count: int = 0
    service_upsert_count: int = 0
    finding_upsert_count: int = 0
    relationship_upsert_count: int = 0
    asset_insert_count: int = 0
    service_insert_count: int = 0
    finding_insert_count: int = 0
    relationship_insert_count: int = 0
    web_path_upsert_count: int = 0
    web_path_insert_count: int = 0
    contradiction_count: int = 0
    contradiction_count_by_domain: dict[str, int] | None = None


class KnowledgeProjectionService:
    """Project canonical observations into durable read models."""

    def __init__(
        self,
        db: Session,
        *,
        identity_service: KnowledgeIdentityService | None = None,
        asset_projector: AssetProjector | None = None,
        service_projector: ServiceProjector | None = None,
        finding_projector: FindingProjector | None = None,
        relationship_projector: RelationshipProjector | None = None,
        engagement_link_projector: EngagementLinkProjector | None = None,
        web_path_projector: WebPathProjector | None = None,
    ) -> None:
        self.db = db
        self.identity_service = identity_service or KnowledgeIdentityService()
        self.asset_projector = asset_projector or AssetProjector()
        self.service_projector = service_projector or ServiceProjector()
        self.finding_projector = finding_projector or FindingProjector()
        self.relationship_projector = relationship_projector or RelationshipProjector()
        self.engagement_link_projector = engagement_link_projector or EngagementLinkProjector()
        self.web_path_projector = web_path_projector or WebPathProjector()

    def project_observations(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int | None = None,
        observations: Iterable[ObservationCreate],
        engagement_id: int | None = None,
    ) -> ProjectionResult:
        """Resolve identity and upsert read-model rows deterministically."""
        observations_list = list(observations)
        if not observations_list:
            return ProjectionResult()

        if engagement_id is None:
            raise ValueError("engagement_id is required for projection writes")
        engagement_scope_id = int(engagement_id)
        self._validate_observation_engagement_scope(
            engagement_id=engagement_scope_id,
            observations=observations_list,
        )

        if user_id is None:
            user_id = int(observations_list[0].user_id)
        resolved_tenant_id = self._resolve_tenant_id(
            engagement_id=engagement_scope_id,
            tenant_id=tenant_id,
        )
        resolution = self.identity_service.resolve_observations(observations_list)
        grouped = self._group_resolved_observations(resolution.resolved_observations)

        asset_key_to_id = self._load_asset_key_map(
            tenant_id=resolved_tenant_id,
            user_id=int(user_id),
        )
        service_key_to_id = self._load_service_key_map(
            tenant_id=resolved_tenant_id,
            user_id=int(user_id),
        )

        asset_upserts = 0
        service_upserts = 0
        finding_upserts = 0
        relationship_upserts = 0
        asset_inserts = 0
        service_inserts = 0
        finding_inserts = 0
        relationship_inserts = 0
        web_path_upserts = 0
        web_path_inserts = 0
        contradiction_by_domain: dict[str, int] = {
            "asset": 0, "service": 0, "finding": 0, "relationship": 0,
        }

        for marker in sorted(resolution.merge_decisions.keys()):
            decision = resolution.merge_decisions[marker]
            if decision.identity_domain != "asset":
                continue
            existing = self._asset_existing_state(
                tenant_id=resolved_tenant_id,
                user_id=int(user_id),
                asset_key=decision.identity_key,
            )
            merged = self.apply_merge_decision(existing_state=existing, decision=decision)
            contradiction_by_domain["asset"] += int(merged.get("_new_contradiction_count") or 0)
            result = self.asset_projector.upsert(
                db=self.db, user_id=int(user_id), tenant_id=resolved_tenant_id, decision=decision,
                merged_state=merged, engagement_id=engagement_scope_id,
            )
            asset_upserts += 1
            if result.inserted:
                asset_inserts += 1
            asset_key_to_id[result.row.asset_key] = str(result.row.id)
            self.engagement_link_projector.upsert_asset_link(
                db=self.db, tenant_id=resolved_tenant_id, engagement_id=engagement_scope_id,
                asset_id=str(result.row.id), observed_at=decision.last_seen_at,
            )

        for marker in sorted(resolution.merge_decisions.keys()):
            decision = resolution.merge_decisions[marker]
            if decision.identity_domain != "service":
                continue
            existing = self._service_existing_state(
                tenant_id=resolved_tenant_id,
                user_id=int(user_id),
                service_key=decision.identity_key,
            )
            merged = self.apply_merge_decision(existing_state=existing, decision=decision)
            contradiction_by_domain["service"] += int(merged.get("_new_contradiction_count") or 0)
            result = self.service_projector.upsert(
                db=self.db, user_id=int(user_id), tenant_id=resolved_tenant_id, decision=decision,
                merged_state=merged, asset_key_to_id=asset_key_to_id,
                engagement_id=engagement_scope_id,
            )
            service_upserts += 1
            if result.inserted:
                service_inserts += 1
            service_key_to_id[result.row.service_key] = str(result.row.id)
            self.engagement_link_projector.upsert_service_link(
                db=self.db, tenant_id=resolved_tenant_id, engagement_id=engagement_scope_id,
                service_id=str(result.row.id), observed_at=decision.last_seen_at,
            )

        for marker in sorted(resolution.merge_decisions.keys()):
            decision = resolution.merge_decisions[marker]
            if decision.identity_domain != "finding":
                continue
            grouped_rows = grouped.get(marker, [])
            existing = self._finding_existing_state(
                tenant_id=resolved_tenant_id,
                user_id=int(user_id),
                finding_key=decision.identity_key,
            )
            merged = self.apply_merge_decision(existing_state=existing, decision=decision)
            contradiction_by_domain["finding"] += int(merged.get("_new_contradiction_count") or 0)
            result = self.finding_projector.upsert(
                db=self.db, user_id=int(user_id), tenant_id=resolved_tenant_id, decision=decision,
                merged_state=merged, resolved_observations=grouped_rows,
                asset_key_to_id=asset_key_to_id, service_key_to_id=service_key_to_id,
                engagement_id=engagement_scope_id,
            )
            finding_upserts += 1
            if result.inserted:
                finding_inserts += 1
            self.engagement_link_projector.upsert_finding_link(
                db=self.db, tenant_id=resolved_tenant_id, engagement_id=engagement_scope_id,
                finding_id=str(result.row.id), observed_at=decision.last_seen_at,
            )

        for marker in sorted(resolution.merge_decisions.keys()):
            decision = resolution.merge_decisions[marker]
            if decision.identity_domain != "relationship":
                continue
            grouped_rows = grouped.get(marker, [])
            existing = self._relationship_existing_state(
                tenant_id=resolved_tenant_id,
                user_id=int(user_id),
                relationship_key=decision.identity_key,
            )
            merged = self.apply_merge_decision(existing_state=existing, decision=decision)
            contradiction_by_domain["relationship"] += int(merged.get("_new_contradiction_count") or 0)
            result = self.relationship_projector.upsert(
                db=self.db, user_id=int(user_id), tenant_id=resolved_tenant_id, decision=decision,
                merged_state=merged, resolved_observations=grouped_rows,
                engagement_id=engagement_scope_id,
            )
            relationship_upserts += 1
            if result.inserted:
                relationship_inserts += 1

        web_path_projection = self.web_path_projector.upsert_from_observations(
            db=self.db,
            user_id=int(user_id),
            tenant_id=resolved_tenant_id,
            engagement_id=engagement_scope_id,
            observations=observations_list,
            asset_key_to_id=asset_key_to_id,
            service_key_to_id=service_key_to_id,
        )
        web_path_upserts = int(web_path_projection.upsert_count)
        web_path_inserts = int(web_path_projection.insert_count)
        if web_path_upserts > 0:
            self._upsert_engagement_web_path_links(
                engagement_id=engagement_scope_id,
                tenant_id=resolved_tenant_id,
                user_id=int(user_id),
                observations=observations_list,
            )

        self.db.flush()
        return ProjectionResult(
            asset_upsert_count=asset_upserts,
            service_upsert_count=service_upserts,
            finding_upsert_count=finding_upserts,
            relationship_upsert_count=relationship_upserts,
            asset_insert_count=asset_inserts,
            service_insert_count=service_inserts,
            finding_insert_count=finding_inserts,
            relationship_insert_count=relationship_inserts,
            web_path_upsert_count=web_path_upserts,
            web_path_insert_count=web_path_inserts,
            contradiction_count=sum(int(value) for value in contradiction_by_domain.values()),
            contradiction_count_by_domain=contradiction_by_domain,
        )

    def _upsert_engagement_web_path_links(
        self,
        *,
        engagement_id: int,
        tenant_id: int,
        user_id: int,
        observations: Sequence[ObservationCreate],
    ) -> None:
        canonical_last_seen: dict[str, datetime] = {}
        for observation in observations:
            if str(observation.observation_type or "").strip().lower() != "web.path_discovered":
                continue
            if str(observation.subject_type or "").strip().lower() != "web.path":
                continue
            subject_key = str(observation.subject_key or "").strip().lower()
            if not subject_key.startswith("web.path:"):
                continue
            canonical_url = subject_key.removeprefix("web.path:")
            observed_at = self._coerce_datetime(observation.observed_at)
            if observed_at is None:
                continue
            previous = canonical_last_seen.get(canonical_url)
            if previous is None or observed_at > previous:
                canonical_last_seen[canonical_url] = observed_at
        if not canonical_last_seen:
            return

        rows = self.db.query(KnowledgeWebPath.id, KnowledgeWebPath.canonical_url).filter(
            KnowledgeWebPath.tenant_id == int(tenant_id),
            KnowledgeWebPath.user_id == int(user_id),
            KnowledgeWebPath.canonical_url.in_(list(canonical_last_seen.keys())),
        ).all()
        for web_path_id, canonical_url in rows:
            observed_at = canonical_last_seen.get(str(canonical_url))
            if observed_at is None:
                continue
            self.engagement_link_projector.upsert_web_path_link(
                db=self.db,
                tenant_id=tenant_id,
                engagement_id=int(engagement_id),
                web_path_id=str(web_path_id),
                observed_at=observed_at,
            )

    def _resolve_tenant_id(self, *, engagement_id: int, tenant_id: int | None = None) -> int:
        """Resolve tenant ownership for projection writes."""
        resolved_tenant_id = self.db.execute(
            select(Engagement.tenant_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if resolved_tenant_id is None:
            raise ValueError(f"Engagement not found: {engagement_id}")
        if tenant_id is not None and int(tenant_id) != int(resolved_tenant_id):
            raise ValueError("Projection tenant_id does not match engagement tenant_id")
        return int(resolved_tenant_id)

    @staticmethod
    def _validate_observation_engagement_scope(
        *,
        engagement_id: int,
        observations: Sequence[ObservationCreate],
    ) -> None:
        mismatched = sorted(
            {
                int(observation.engagement_id)
                for observation in observations
                if int(observation.engagement_id) != int(engagement_id)
            }
        )
        if mismatched:
            joined = ", ".join(str(value) for value in mismatched)
            raise ValueError(
                "Observation engagement scope mismatch. "
                f"Expected engagement_id={engagement_id}, got={joined}"
            )

    def apply_merge_decision(
        self,
        *,
        existing_state: Mapping[str, Any] | None,
        decision: IdentityMergeDecision,
    ) -> dict[str, Any]:
        """Return one merged projector state record for upsert boundaries."""
        current = dict(existing_state or {})
        current_metadata = dict(current.get("metadata") or {})
        incoming_metadata = dict(decision.metadata or {})
        first_seen = self._min_datetime(
            self._coerce_datetime(current.get("first_seen_at")),
            decision.first_seen_at,
        )
        last_seen = self._max_datetime(
            self._coerce_datetime(current.get("last_seen_at")),
            decision.last_seen_at,
        )
        previous_count = int(current_metadata.get("observation_count") or current.get("observation_count") or 0)
        merged_count = previous_count + int(decision.observation_count)
        merged_confidence = merge_confidence_with_corroboration(
            current=str(current.get("confidence") or ""),
            incoming=decision.confidence,
            is_corroborated=merged_count > 1,
        )
        merged_evidence_refs = merge_evidence_refs(
            existing=current_metadata.get("evidence_refs") if isinstance(current_metadata.get("evidence_refs"), list) else None,
            incoming=decision.evidence_refs,
        )
        merged_state, state_contradictions = merge_state_with_contradictions(
            existing_state=current_metadata.get("state"),
            incoming_state=incoming_metadata.get("state"),
            observed_at=decision.last_seen_at,
        )
        merged_rich_details = merge_rich_details(
            existing=current_metadata.get("rich_details")
            if isinstance(current_metadata.get("rich_details"), Mapping)
            else None,
            incoming=incoming_metadata.get("rich_details")
            if isinstance(incoming_metadata.get("rich_details"), Mapping)
            else None,
        )
        contradictions: list[dict[str, Any]] = []
        previous_contradictions = current_metadata.get("contradictions")
        incoming_contradictions = incoming_metadata.get("contradictions")
        if isinstance(previous_contradictions, list):
            contradictions.extend(item for item in previous_contradictions if isinstance(item, Mapping))
        if isinstance(incoming_contradictions, list):
            contradictions.extend(item for item in incoming_contradictions if isinstance(item, Mapping))
        contradictions.extend(state_contradictions)
        incoming_contradiction_count = (
            len([item for item in incoming_contradictions if isinstance(item, Mapping)])
            if isinstance(incoming_contradictions, list)
            else 0
        )

        source_subject_types = sorted(
            {
                *self._as_string_set(current_metadata.get("source_subject_types")),
                *{str(item) for item in decision.source_subject_types},
            }
        )
        source_observation_types = sorted(
            {
                *self._as_string_set(current_metadata.get("source_observation_types")),
                *{str(item) for item in decision.source_observation_types},
            }
        )

        merged_metadata = {
            **current_metadata,
            **incoming_metadata,
            "state": merged_state,
            "evidence_refs": merged_evidence_refs,
            "observation_count": merged_count,
            "source_subject_types": source_subject_types,
            "source_observation_types": source_observation_types,
        }
        if merged_rich_details:
            merged_metadata["rich_details"] = merged_rich_details
        if contradictions:
            merged_metadata["contradictions"] = contradictions

        return {
            "identity_domain": decision.identity_domain,
            "identity_key": decision.identity_key,
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "observation_count": merged_count,
            "confidence": merged_confidence,
            "evidence_refs": merged_evidence_refs,
            "metadata": merged_metadata,
            "_new_contradiction_count": len(state_contradictions) + incoming_contradiction_count,
        }

    @staticmethod
    def _as_string_set(value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(item) for item in value if str(item).strip()}

    @staticmethod
    def _group_resolved_observations(
        resolved_observations: Iterable[ResolvedIdentityObservation],
    ) -> dict[str, list[ResolvedIdentityObservation]]:
        grouped: dict[str, list[ResolvedIdentityObservation]] = {}
        for row in resolved_observations:
            marker = f"{row.identity_domain}:{row.identity_key}"
            grouped.setdefault(marker, []).append(row)
        for key in grouped:
            grouped[key] = sorted(grouped[key], key=lambda item: item.observed_at)
        return grouped

    def _load_asset_key_map(self, *, tenant_id: int, user_id: int) -> dict[str, str]:
        rows = self.db.query(KnowledgeAsset.asset_key, KnowledgeAsset.id).filter(
            KnowledgeAsset.tenant_id == int(tenant_id),
            KnowledgeAsset.user_id == int(user_id),
        ).all()
        return {str(asset_key): str(asset_id) for asset_key, asset_id in rows}

    def _load_service_key_map(self, *, tenant_id: int, user_id: int) -> dict[str, str]:
        rows = self.db.query(KnowledgeService.service_key, KnowledgeService.id).filter(
            KnowledgeService.tenant_id == int(tenant_id),
            KnowledgeService.user_id == int(user_id),
        ).all()
        return {str(service_key): str(service_id) for service_key, service_id in rows}

    def _asset_existing_state(
        self,
        *,
        tenant_id: int,
        user_id: int,
        asset_key: str,
    ) -> dict[str, Any] | None:
        row = self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.tenant_id == int(tenant_id),
            KnowledgeAsset.user_id == int(user_id),
            KnowledgeAsset.asset_key == str(asset_key),
        ).one_or_none()
        if row is None:
            return None
        return {
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "confidence": row.max_confidence,
            "metadata": dict(row.asset_metadata or {}),
        }

    def _service_existing_state(
        self,
        *,
        tenant_id: int,
        user_id: int,
        service_key: str,
    ) -> dict[str, Any] | None:
        row = self.db.query(KnowledgeService).filter(
            KnowledgeService.tenant_id == int(tenant_id),
            KnowledgeService.user_id == int(user_id),
            KnowledgeService.service_key == str(service_key),
        ).one_or_none()
        if row is None:
            return None
        return {
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "metadata": dict(row.service_metadata or {}),
        }

    def _finding_existing_state(
        self,
        *,
        tenant_id: int,
        user_id: int,
        finding_key: str,
    ) -> dict[str, Any] | None:
        from ...models import KnowledgeFinding

        row = self.db.query(KnowledgeFinding).filter(
            KnowledgeFinding.tenant_id == int(tenant_id),
            KnowledgeFinding.user_id == int(user_id),
            KnowledgeFinding.finding_key == str(finding_key),
        ).one_or_none()
        if row is None:
            return None
        return {
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "confidence": row.confidence,
            "metadata": dict(row.finding_metadata or {}),
        }

    def _relationship_existing_state(
        self,
        *,
        tenant_id: int,
        user_id: int,
        relationship_key: str,
    ) -> dict[str, Any] | None:
        from ...models import KnowledgeRelationship

        row = self.db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.tenant_id == int(tenant_id),
            KnowledgeRelationship.user_id == int(user_id),
            KnowledgeRelationship.relationship_key == str(relationship_key),
        ).one_or_none()
        if row is None:
            return None
        return {
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "confidence": row.confidence,
            "metadata": dict(row.relationship_metadata or {}),
        }

    @staticmethod
    def _coerce_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return to_utc(value)
        return None

    @staticmethod
    def _min_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
        left = KnowledgeProjectionService._coerce_datetime(left)
        right = KnowledgeProjectionService._coerce_datetime(right)
        if left is None:
            return right
        if right is None:
            return left
        return right if right < left else left

    @staticmethod
    def _max_datetime(left: datetime | None, right: datetime | None) -> datetime | None:
        left = KnowledgeProjectionService._coerce_datetime(left)
        right = KnowledgeProjectionService._coerce_datetime(right)
        if left is None:
            return right
        if right is None:
            return left
        return right if right > left else left

    def project_with_retry(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int,
        engagement_id: int,
        observations: list,
        retry_attempts: int = 1,
        error_sanitizer: Callable[..., dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run projection with immediate retry and rollback-safe attempts."""
        attempt_count = 1 + int(retry_attempts)
        last_error: Exception | None = None
        last_error_details: dict[str, Any] | None = None

        for attempt in range(1, attempt_count + 1):
            try:
                with self.db.begin_nested():
                    projection = self.project_observations(
                        tenant_id=tenant_id,
                        user_id=int(user_id),
                        engagement_id=int(engagement_id),
                        observations=observations,
                    )
                return {
                    "projection_status": "succeeded",
                    "projection_attempt_count": attempt,
                    "repair_required": False,
                    "repair_owner": "none",
                    "projection_error": None,
                    "projection_error_class": None,
                    "projection_error_fingerprint": None,
                    "projection_error_redacted": False,
                    "asset_upsert_count": projection.asset_upsert_count,
                    "service_upsert_count": projection.service_upsert_count,
                    "finding_upsert_count": projection.finding_upsert_count,
                    "relationship_upsert_count": projection.relationship_upsert_count,
                    "asset_insert_count": projection.asset_insert_count,
                    "service_insert_count": projection.service_insert_count,
                    "finding_insert_count": projection.finding_insert_count,
                    "relationship_insert_count": projection.relationship_insert_count,
                    "web_path_upsert_count": projection.web_path_upsert_count,
                    "web_path_insert_count": projection.web_path_insert_count,
                    "projection_contradiction_count": projection.contradiction_count,
                    "projection_contradiction_count_by_domain": dict(
                        projection.contradiction_count_by_domain or {}
                    ),
                }
            except Exception as exc:
                last_error = exc
                if error_sanitizer is not None:
                    last_error_details = error_sanitizer(stage="projection", error=exc)
                else:
                    last_error_details = {
                        "message": f"projection failed [{exc.__class__.__name__}]: {exc}",
                        "error_class": exc.__class__.__name__,
                        "fingerprint": "",
                        "redacted": False,
                    }
                continue

        safe_projection_error = last_error_details or {
            "message": f"projection failed: {last_error}",
            "error_class": (last_error.__class__.__name__ if last_error else "RuntimeError"),
            "fingerprint": "",
            "redacted": False,
        }
        return {
            "projection_status": "failed",
            "projection_attempt_count": attempt_count,
            "repair_required": True,
            "repair_owner": "knowledge_read_model_rebuild_service",
            "projection_error": safe_projection_error["message"],
            "projection_error_class": safe_projection_error["error_class"],
            "projection_error_fingerprint": safe_projection_error["fingerprint"],
            "projection_error_redacted": safe_projection_error["redacted"],
            "asset_upsert_count": 0,
            "service_upsert_count": 0,
            "finding_upsert_count": 0,
            "relationship_upsert_count": 0,
            "asset_insert_count": 0,
            "service_insert_count": 0,
            "finding_insert_count": 0,
            "relationship_insert_count": 0,
            "web_path_upsert_count": 0,
            "web_path_insert_count": 0,
            "projection_contradiction_count": 0,
            "projection_contradiction_count_by_domain": {},
        }
