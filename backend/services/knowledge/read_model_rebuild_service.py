"""Internal rebuild boundary for deterministic read-model recomputation.

Scope:
- Rebuild durable read models from the observation ledger only.
- Support tenant-scoped and source-execution-scoped rebuild operations.

Boundary:
- No runtime tool execution.
- No artifact parsing.
- No public API surface in this module."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.time_utils import to_utc
from ...models import (
    Engagement,
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from .contracts import ObservationCreate, validate_subject_key_matches_type
from .identity_service import KnowledgeIdentityService
from .projection_service import KnowledgeProjectionService, ProjectionResult


class KnowledgeReadModelRebuildService:
    """Recompute read models from durable observations."""

    _CANONICAL_PROVENANCE_ENTITY_TYPES: tuple[str, ...] = (
        "asset",
        "service",
        "finding",
        "relationship",
        "web_path",
    )

    def __init__(
        self,
        db: Session,
        *,
        projection_service: KnowledgeProjectionService | None = None,
        identity_service: KnowledgeIdentityService | None = None,
    ) -> None:
        self.db = db
        self.projection_service = projection_service or KnowledgeProjectionService(db)
        self.identity_service = identity_service or KnowledgeIdentityService()

    def rebuild_engagement(self, *, engagement_id: int) -> dict[str, Any]:
        """Reset and rebuild all read models for one engagement's tenant scope."""
        tenant_id = self._resolve_tenant_id_for_engagement(engagement_id)
        observations = self._load_observations(tenant_id=tenant_id)
        with self.db.begin_nested():
            self._reset_tenant_scope(tenant_id=tenant_id)
            projection, replayed_engagement_ids = self._project_observations_grouped_by_engagement(
                observations=observations,
            )
        return self._build_result(
            scope="engagement",
            engagement_id=int(engagement_id),
            source_execution_id=None,
            observation_count=len(observations),
            projection=projection,
            impacted_identity_count=None,
            replayed_engagement_ids=replayed_engagement_ids,
        )

    def rebuild_source_execution(
        self,
        *,
        source_execution_id: str,
        engagement_id: int | None = None,
    ) -> dict[str, Any]:
        """Reset and rebuild read-model identities impacted by one source execution."""
        resolved_engagement_id = self._resolve_engagement_id_for_source_execution(
            source_execution_id=str(source_execution_id),
            engagement_id=engagement_id,
        )
        tenant_id = self._resolve_tenant_id_for_engagement(resolved_engagement_id)
        source_observations = self._load_observations(
            tenant_id=tenant_id,
            engagement_id=resolved_engagement_id,
            source_execution_id=str(source_execution_id),
        )
        if not source_observations:
            raise ValueError(
                "No observations found for source execution within the resolved scope"
            )

        marker_set = self._resolve_identity_markers(source_observations)
        impacted_web_path_subject_keys = self._resolve_impacted_web_path_subject_keys(
            source_observations
        )
        all_tenant_observations = self._load_observations(tenant_id=tenant_id)
        selected_observations = self._select_observations_for_markers(
            observations=all_tenant_observations,
            marker_set=marker_set,
            impacted_web_path_subject_keys=impacted_web_path_subject_keys,
        )
        marker_keys = self._markers_by_domain(marker_set)

        with self.db.begin_nested():
            self._reset_marker_scope(
                tenant_id=tenant_id,
                marker_keys=marker_keys,
                impacted_web_path_subject_keys=impacted_web_path_subject_keys,
            )
            projection, replayed_engagement_ids = self._project_observations_grouped_by_engagement(
                observations=selected_observations,
            )

        return self._build_result(
            scope="source_execution",
            engagement_id=resolved_engagement_id,
            source_execution_id=str(source_execution_id),
            observation_count=len(selected_observations),
            projection=projection,
            impacted_identity_count=len(marker_set),
            replayed_engagement_ids=replayed_engagement_ids,
        )

    def _project_observations_grouped_by_engagement(
        self,
        *,
        observations: Iterable[ObservationCreate],
    ) -> tuple[ProjectionResult, list[int]]:
        grouped: dict[int, list[ObservationCreate]] = defaultdict(list)
        for observation in observations:
            grouped[int(observation.engagement_id)].append(observation)
        if not grouped:
            return ProjectionResult(), []

        replayed_engagement_ids: list[int] = []
        aggregated_projection = ProjectionResult(
            contradiction_count_by_domain={"asset": 0, "service": 0, "finding": 0, "relationship": 0}
        )
        for engagement_id in sorted(grouped):
            replayed_engagement_ids.append(int(engagement_id))
            current_projection = self.projection_service.project_observations(
                engagement_id=int(engagement_id),
                observations=grouped[engagement_id],
            )
            aggregated_projection = self._accumulate_projection_results(
                base=aggregated_projection,
                increment=current_projection,
            )
        return aggregated_projection, replayed_engagement_ids

    @staticmethod
    def _accumulate_projection_results(
        *,
        base: ProjectionResult,
        increment: ProjectionResult,
    ) -> ProjectionResult:
        base_contradictions = dict(base.contradiction_count_by_domain or {})
        increment_contradictions = dict(increment.contradiction_count_by_domain or {})
        combined_contradictions = {
            domain: int(base_contradictions.get(domain, 0)) + int(increment_contradictions.get(domain, 0))
            for domain in ("asset", "service", "finding", "relationship")
        }
        return ProjectionResult(
            asset_upsert_count=int(base.asset_upsert_count) + int(increment.asset_upsert_count),
            service_upsert_count=int(base.service_upsert_count) + int(increment.service_upsert_count),
            finding_upsert_count=int(base.finding_upsert_count) + int(increment.finding_upsert_count),
            relationship_upsert_count=int(base.relationship_upsert_count)
            + int(increment.relationship_upsert_count),
            asset_insert_count=int(base.asset_insert_count) + int(increment.asset_insert_count),
            service_insert_count=int(base.service_insert_count) + int(increment.service_insert_count),
            finding_insert_count=int(base.finding_insert_count) + int(increment.finding_insert_count),
            relationship_insert_count=int(base.relationship_insert_count)
            + int(increment.relationship_insert_count),
            web_path_upsert_count=int(base.web_path_upsert_count) + int(increment.web_path_upsert_count),
            web_path_insert_count=int(base.web_path_insert_count) + int(increment.web_path_insert_count),
            contradiction_count=int(base.contradiction_count) + int(increment.contradiction_count),
            contradiction_count_by_domain=combined_contradictions,
        )

    def _resolve_tenant_id_for_engagement(self, engagement_id: int) -> int:
        tenant_id = self.db.execute(
            select(Engagement.tenant_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(f"Engagement not found: {engagement_id}")
        return int(tenant_id)

    def _resolve_engagement_id_for_source_execution(
        self,
        *,
        source_execution_id: str,
        engagement_id: int | None,
    ) -> int:
        query = select(KnowledgeIngestionRun.engagement_id).where(
            KnowledgeIngestionRun.source_execution_id == str(source_execution_id)
        )
        if engagement_id is not None:
            query = query.where(KnowledgeIngestionRun.engagement_id == int(engagement_id))
        row = self.db.execute(
            query.order_by(
                KnowledgeIngestionRun.updated_at.desc(),
                KnowledgeIngestionRun.created_at.desc(),
            )
        ).first()
        if row is None:
            raise ValueError("Cannot resolve engagement for source execution rebuild")
        return int(row[0])

    def _load_observations(
        self,
        *,
        tenant_id: int,
        engagement_id: int | None = None,
        source_execution_id: str | None = None,
    ) -> list[ObservationCreate]:
        query = select(KnowledgeObservation).where(
            KnowledgeObservation.tenant_id == int(tenant_id)
        )
        if engagement_id is not None:
            query = query.where(KnowledgeObservation.engagement_id == int(engagement_id))
        if source_execution_id is not None:
            query = query.where(
                KnowledgeObservation.source_execution_id == str(source_execution_id)
            )
        rows = self.db.execute(
            query.order_by(KnowledgeObservation.observed_at.asc(), KnowledgeObservation.id.asc())
        ).scalars().all()
        observations: list[ObservationCreate] = []
        for row in rows:
            observation = self._replay_observation_from_row(row)
            if observation is None:
                continue
            observations.append(observation)
        return observations

    @staticmethod
    def _replay_observation_from_row(row: KnowledgeObservation) -> ObservationCreate | None:
        subject_type = str(row.subject_type or "").strip().lower()
        subject_key = str(row.subject_key or "")
        payload = dict(row.payload or {})
        if subject_type == "service.socket":
            try:
                validate_subject_key_matches_type(
                    subject_type=subject_type,
                    subject_key=subject_key,
                )
            except ValueError:
                return None
        return ObservationCreate(
            user_id=int(row.user_id),
            engagement_id=int(row.engagement_id),
            task_id=row.task_id,
            source_execution_id=str(row.source_execution_id),
            ingestion_run_id=str(row.ingestion_run_id),
            observation_type=str(row.observation_type),
            subject_type=str(row.subject_type),
            subject_key=subject_key,
            assertion_level=str(row.assertion_level),
            payload=payload,
            observed_at=KnowledgeReadModelRebuildService._normalize_observed_at(row.observed_at),
            dedupe_key=str(row.dedupe_key),
        )

    def _resolve_identity_markers(self, observations: Iterable[ObservationCreate]) -> set[str]:
        resolution = self.identity_service.resolve_observations(observations)
        return set(resolution.merge_decisions.keys())

    def _select_observations_for_markers(
        self,
        *,
        observations: Iterable[ObservationCreate],
        marker_set: set[str],
        impacted_web_path_subject_keys: set[str] | None = None,
    ) -> list[ObservationCreate]:
        normalized_impacted_subject_keys = {
            str(value).strip().lower()
            for value in (impacted_web_path_subject_keys or set())
            if str(value).strip()
        }
        if not marker_set and not normalized_impacted_subject_keys:
            return []
        selected: list[ObservationCreate] = []
        for observation in observations:
            if self._observation_matches_impacted_web_path(
                observation=observation,
                impacted_web_path_subject_keys=normalized_impacted_subject_keys,
            ):
                selected.append(observation)
                continue
            resolved = self.identity_service.resolve_observations([observation]).resolved_observations
            if not resolved:
                continue
            marker = f"{resolved[0].identity_domain}:{resolved[0].identity_key}"
            if marker in marker_set:
                selected.append(observation)
        return selected

    @staticmethod
    def _resolve_impacted_web_path_subject_keys(
        observations: Iterable[ObservationCreate],
    ) -> set[str]:
        impacted_subject_keys: set[str] = set()
        for observation in observations:
            subject_key = str(observation.subject_key or "").strip().lower()
            if (
                str(observation.observation_type or "").strip().lower() == "web.path_discovered"
                and str(observation.subject_type or "").strip().lower() == "web.path"
                and subject_key.startswith("web.path:")
            ):
                impacted_subject_keys.add(subject_key)
        return impacted_subject_keys

    @staticmethod
    def _observation_matches_impacted_web_path(
        *,
        observation: ObservationCreate,
        impacted_web_path_subject_keys: set[str],
    ) -> bool:
        if not impacted_web_path_subject_keys:
            return False
        if str(observation.subject_type or "").strip().lower() != "web.path":
            return False
        subject_key = str(observation.subject_key or "").strip().lower()
        return subject_key in impacted_web_path_subject_keys

    @staticmethod
    def _markers_by_domain(marker_set: set[str]) -> dict[str, set[str]]:
        keys: dict[str, set[str]] = {
            "asset": set(), "service": set(), "finding": set(), "relationship": set(),
        }
        for marker in marker_set:
            domain, _, identity_key = str(marker).partition(":")
            if domain in keys and identity_key:
                keys[domain].add(identity_key)
        return keys

    def _reset_tenant_scope(self, *, tenant_id: int) -> None:
        self._delete_tenant_canonical_provenance(tenant_id=tenant_id)
        self.db.query(EngagementWebPathLink).filter(
            EngagementWebPathLink.web_path_id.in_(
                select(KnowledgeWebPath.id).where(KnowledgeWebPath.tenant_id == int(tenant_id))
            )
        ).delete(synchronize_session=False)
        self.db.query(KnowledgeWebPath).filter(
            KnowledgeWebPath.tenant_id == int(tenant_id)
        ).delete(synchronize_session=False)
        self.db.query(EngagementFindingLink).filter(
            EngagementFindingLink.finding_id.in_(
                select(KnowledgeFinding.id).where(KnowledgeFinding.tenant_id == int(tenant_id))
            )
        ).delete(synchronize_session=False)
        self.db.query(EngagementServiceLink).filter(
            EngagementServiceLink.service_id.in_(
                select(KnowledgeService.id).where(KnowledgeService.tenant_id == int(tenant_id))
            )
        ).delete(synchronize_session=False)
        self.db.query(EngagementAssetLink).filter(
            EngagementAssetLink.asset_id.in_(
                select(KnowledgeAsset.id).where(KnowledgeAsset.tenant_id == int(tenant_id))
            )
        ).delete(synchronize_session=False)
        self.db.query(KnowledgeRelationship).filter(
            KnowledgeRelationship.tenant_id == int(tenant_id)
        ).delete(synchronize_session=False)
        self.db.query(KnowledgeFinding).filter(
            KnowledgeFinding.tenant_id == int(tenant_id)
        ).delete(synchronize_session=False)
        self.db.query(KnowledgeService).filter(
            KnowledgeService.tenant_id == int(tenant_id)
        ).delete(synchronize_session=False)
        self.db.query(KnowledgeAsset).filter(
            KnowledgeAsset.tenant_id == int(tenant_id)
        ).delete(synchronize_session=False)
        self.db.flush()

    def _reset_marker_scope(
        self,
        *,
        tenant_id: int,
        marker_keys: dict[str, set[str]],
        impacted_web_path_subject_keys: set[str] | None = None,
    ) -> None:
        relationship_keys = marker_keys.get("relationship") or set()
        finding_keys = marker_keys.get("finding") or set()
        service_keys = marker_keys.get("service") or set()
        asset_keys = marker_keys.get("asset") or set()
        web_path_urls = {
            str(subject_key).removeprefix("web.path:")
            for subject_key in (impacted_web_path_subject_keys or set())
            if str(subject_key).strip().lower().startswith("web.path:")
        }

        if web_path_urls:
            self._delete_canonical_provenance_for_web_paths(
                tenant_id=tenant_id,
                canonical_urls=web_path_urls,
            )
            impacted_web_path_ids = select(KnowledgeWebPath.id).where(
                KnowledgeWebPath.tenant_id == int(tenant_id),
                KnowledgeWebPath.canonical_url.in_(sorted(web_path_urls)),
            )
            self.db.query(EngagementWebPathLink).filter(
                EngagementWebPathLink.web_path_id.in_(impacted_web_path_ids)
            ).delete(synchronize_session=False)
            self.db.query(KnowledgeWebPath).filter(
                KnowledgeWebPath.tenant_id == int(tenant_id),
                KnowledgeWebPath.canonical_url.in_(sorted(web_path_urls)),
            ).delete(synchronize_session=False)

        if relationship_keys:
            self._delete_canonical_provenance_for_domain_keys(
                tenant_id=tenant_id,
                entity_type="relationship",
                model=KnowledgeRelationship,
                key_column=KnowledgeRelationship.relationship_key,
                key_values=relationship_keys,
            )
            self.db.query(KnowledgeRelationship).filter(
                KnowledgeRelationship.tenant_id == int(tenant_id),
                KnowledgeRelationship.relationship_key.in_(relationship_keys),
            ).delete(synchronize_session=False)
        if finding_keys:
            self._delete_canonical_provenance_for_domain_keys(
                tenant_id=tenant_id,
                entity_type="finding",
                model=KnowledgeFinding,
                key_column=KnowledgeFinding.finding_key,
                key_values=finding_keys,
            )
            self.db.query(KnowledgeFinding).filter(
                KnowledgeFinding.tenant_id == int(tenant_id),
                KnowledgeFinding.finding_key.in_(finding_keys),
            ).delete(synchronize_session=False)
        if service_keys:
            self._delete_canonical_provenance_for_domain_keys(
                tenant_id=tenant_id,
                entity_type="service",
                model=KnowledgeService,
                key_column=KnowledgeService.service_key,
                key_values=service_keys,
            )
            self.db.query(KnowledgeService).filter(
                KnowledgeService.tenant_id == int(tenant_id),
                KnowledgeService.service_key.in_(service_keys),
            ).delete(synchronize_session=False)
        if asset_keys:
            self._delete_canonical_provenance_for_domain_keys(
                tenant_id=tenant_id,
                entity_type="asset",
                model=KnowledgeAsset,
                key_column=KnowledgeAsset.asset_key,
                key_values=asset_keys,
            )
            self.db.query(KnowledgeAsset).filter(
                KnowledgeAsset.tenant_id == int(tenant_id),
                KnowledgeAsset.asset_key.in_(asset_keys),
            ).delete(synchronize_session=False)
        self.db.flush()

    def _delete_tenant_canonical_provenance(self, *, tenant_id: int) -> None:
        self.db.query(KnowledgeEntityProvenance).filter(
            KnowledgeEntityProvenance.tenant_id == int(tenant_id),
            KnowledgeEntityProvenance.entity_type.in_(self._CANONICAL_PROVENANCE_ENTITY_TYPES),
        ).delete(synchronize_session=False)

    def _delete_canonical_provenance_for_domain_keys(
        self,
        *,
        tenant_id: int,
        entity_type: str,
        model: type[KnowledgeAsset]
        | type[KnowledgeService]
        | type[KnowledgeFinding]
        | type[KnowledgeRelationship],
        key_column,
        key_values: set[str],
    ) -> None:
        if not key_values:
            return
        impacted_entity_ids = select(model.id).where(
            model.tenant_id == int(tenant_id),
            key_column.in_(sorted(key_values)),
        )
        self.db.query(KnowledgeEntityProvenance).filter(
            KnowledgeEntityProvenance.tenant_id == int(tenant_id),
            KnowledgeEntityProvenance.entity_type == str(entity_type),
            KnowledgeEntityProvenance.entity_id.in_(impacted_entity_ids),
        ).delete(synchronize_session=False)

    def _delete_canonical_provenance_for_web_paths(
        self,
        *,
        tenant_id: int,
        canonical_urls: set[str],
    ) -> None:
        if not canonical_urls:
            return
        impacted_entity_ids = select(KnowledgeWebPath.id).where(
            KnowledgeWebPath.tenant_id == int(tenant_id),
            KnowledgeWebPath.canonical_url.in_(sorted(canonical_urls)),
        )
        self.db.query(KnowledgeEntityProvenance).filter(
            KnowledgeEntityProvenance.tenant_id == int(tenant_id),
            KnowledgeEntityProvenance.entity_type == "web_path",
            KnowledgeEntityProvenance.entity_id.in_(impacted_entity_ids),
        ).delete(synchronize_session=False)

    @staticmethod
    def _build_result(
        *,
        scope: str,
        engagement_id: int,
        source_execution_id: str | None,
        observation_count: int,
        projection: ProjectionResult,
        impacted_identity_count: int | None,
        replayed_engagement_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "scope": scope,
            "engagement_id": int(engagement_id),
            "source_execution_id": source_execution_id,
            "observation_count": int(observation_count),
            "asset_upsert_count": int(projection.asset_upsert_count),
            "service_upsert_count": int(projection.service_upsert_count),
            "finding_upsert_count": int(projection.finding_upsert_count),
            "relationship_upsert_count": int(projection.relationship_upsert_count),
            "web_path_upsert_count": int(projection.web_path_upsert_count),
            "web_path_insert_count": int(projection.web_path_insert_count),
        }
        if impacted_identity_count is not None:
            result["impacted_identity_count"] = int(impacted_identity_count)
        if replayed_engagement_ids is not None:
            normalized_replayed_ids = sorted({int(item) for item in replayed_engagement_ids})
            result["replayed_engagement_ids"] = normalized_replayed_ids
            result["replayed_engagement_count"] = len(normalized_replayed_ids)
        return result

    @staticmethod
    def _normalize_observed_at(value: datetime) -> datetime:
        return to_utc(value)
