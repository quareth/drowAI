"""Orchestrate ingestion runs, archives, and append-only observations.

Scope:
- Durable orchestration and write operations for ingestion lifecycle rows and
 observation rows.

Responsibilities:
- Resolve durable ownership context for one execution ingestion unit.
- Read runtime provenance from existing query services.
- Archive delete-critical evidence through archive policy service.
- Create or reuse ingestion runs idempotently.
- Validate observation payloads through shared contracts.
- Enforce run-local deduplication and lineage consistency.

Boundary:
- This service owns ingestion orchestration and persistence behavior.
- Extractor logic is pluggable and intentionally small in."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any
import hashlib
import re
import time
import uuid as uuid_lib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.feature_flags import get_knowledge_vulnerability_min_confidence
from backend.core.time_utils import to_utc
from backend.models import (
    KnowledgeEvidenceArchive,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    Task,
)
from backend.services.artifact.memory_service import ArtifactMemoryService
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from .archive_service import KnowledgeArchiveService
from .adapter_registry import KnowledgeAdapterRegistryService
from .contracts import (
    IngestionRunCreate,
    IngestionRunStatus,
    ObservationCreate,
    build_semantic_input_snapshot,
    normalize_observation_create,
)
from .candidate_extraction import (
    build_candidate_run_metadata,
    maybe_run_candidate_extraction,
    record_candidate_usage_if_task_present,
)
from .delete_guard_service import KnowledgeDeleteGuardService
from .projection_service import KnowledgeProjectionService
from backend.services.usage_tracking import UsageTrackingService


ExecutionExtractor = Callable[
    [dict[str, Any], str, int, int | None, Mapping[str, Any] | None],
    list[ObservationCreate],
]


class KnowledgeIngestionService:
    """Own ingestion-run orchestration and durable observation persistence."""

    DEFAULT_EXTRACTOR_FAMILY = "runtime.ingestion"
    DEFAULT_EXTRACTOR_VERSION = "1.0"
    ADAPTER_ARTIFACT_READ_MAX_CHARS = 20_000
    CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY = "llm.candidate_extraction"
    CANDIDATE_EXTRACTION_EXTRACTOR_VERSION = "1.0"
    CANDIDATE_EXTRACTION_MODE = "candidate_fallback"
    PROJECTION_IMMEDIATE_RETRY_ATTEMPTS = 1
    MAX_SAFE_ERROR_CHARS = 512
    _SENSITIVE_ERROR_PATTERNS = (
        re.compile(
            r"(?i)\b(authorization|token|api[_-]?key|secret|password|passwd|cookie|session)\b"
            r"\s*[:=]\s*([^\s,;]+)"
        ),
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]+)"),
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    )

    def __init__(
        self,
        db: Session,
        *,
        query_service: ArtifactProvenanceQueryService | None = None,
        artifact_memory_service: ArtifactMemoryService | None = None,
        archive_service: KnowledgeArchiveService | None = None,
        adapter_registry: KnowledgeAdapterRegistryService | None = None,
        projection_service: KnowledgeProjectionService | None = None,
        candidate_extraction_service: Any | None = None,
        extractors: Iterable[ExecutionExtractor] | None = None,
    ):
        self.db = db
        self.query_service = query_service or ArtifactProvenanceQueryService(db)
        self.artifact_memory_service = artifact_memory_service or ArtifactMemoryService(
            db,
            query_service=self.query_service,
        )
        self.archive_service = archive_service or KnowledgeArchiveService(db)
        self.adapter_registry = adapter_registry or KnowledgeAdapterRegistryService()
        self.projection_service = projection_service or KnowledgeProjectionService(db)
        # Deprecated: retained for backward-compatible construction only.
        self.candidate_extraction_service = candidate_extraction_service
        # Keep extractor coverage intentionally small.
        # Empty registry means unsupported tools still archive evidence and succeed with zero observations.
        self.extractors = list(extractors or [])
        self.delete_guard_service = KnowledgeDeleteGuardService(
            db,
            ingest_execution=self.ingest_execution,
        )

    def register_extractor(self, extractor: ExecutionExtractor) -> None:
        """Register one focused extractor callable for ingestion orchestration."""
        self.extractors.append(extractor)

    def create_or_get_ingestion_run(self, run: IngestionRunCreate) -> KnowledgeIngestionRun:
        """Idempotently return one run identity per execution+extractor tuple."""
        existing = self.db.execute(
            select(KnowledgeIngestionRun).where(
                KnowledgeIngestionRun.engagement_id == run.engagement_id,
                KnowledgeIngestionRun.source_execution_id == str(run.source_execution_id),
                KnowledgeIngestionRun.extractor_family == run.extractor_family,
                KnowledgeIngestionRun.extractor_version == run.extractor_version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        created = KnowledgeIngestionRun(
            id=uuid_lib.uuid4(),
            tenant_id=(
                int(run.tenant_id)
                if run.tenant_id is not None
                else self._resolve_tenant_id_from_engagement(engagement_id=int(run.engagement_id))
            ),
            user_id=run.user_id,
            engagement_id=run.engagement_id,
            task_id=run.task_id,
            source_execution_id=run.source_execution_id,
            extractor_family=run.extractor_family,
            extractor_version=run.extractor_version,
            status=run.status.value,
            run_metadata=run.metadata,
        )
        self.db.add(created)
        self.db.flush()
        return created

    def insert_observations(
        self,
        *,
        ingestion_run_id: str,
        observations: Iterable[ObservationCreate],
    ) -> tuple[int, int]:
        """Insert normalized append-only observations with run-local dedupe."""
        run = self.db.execute(
            select(KnowledgeIngestionRun).where(KnowledgeIngestionRun.id == ingestion_run_id)
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"Ingestion run not found: {ingestion_run_id}")

        normalized = [normalize_observation_create(item) for item in observations]
        for item in normalized:
            if int(item.engagement_id) != int(run.engagement_id):
                raise ValueError(
                    "Observation engagement_id does not match ingestion run engagement_id"
                )
            if str(item.ingestion_run_id) != str(run.id):
                raise ValueError(
                    "Observation ingestion_run_id does not match target ingestion run id"
                )
            if str(item.source_execution_id) != str(run.source_execution_id):
                raise ValueError(
                    "Observation source_execution_id does not match ingestion run source_execution_id"
                )
            if item.task_id != run.task_id:
                raise ValueError("Observation task_id does not match ingestion run task_id")
            if item.tenant_id is not None and int(item.tenant_id) != int(run.tenant_id):
                raise ValueError("Observation tenant_id does not match ingestion run tenant_id")
        dedupe_keys = [item.dedupe_key for item in normalized if item.dedupe_key]

        existing_keys = set()
        if dedupe_keys:
            existing_rows = self.db.execute(
                select(KnowledgeObservation.dedupe_key).where(
                    KnowledgeObservation.ingestion_run_id == ingestion_run_id,
                    KnowledgeObservation.dedupe_key.in_(dedupe_keys),
                )
            ).all()
            existing_keys = {row[0] for row in existing_rows}

        inserted = 0
        duplicates = 0
        seen_in_batch: set[str] = set()
        for item in normalized:
            dedupe_key = str(item.dedupe_key)
            if dedupe_key in existing_keys or dedupe_key in seen_in_batch:
                duplicates += 1
                continue

            row = KnowledgeObservation(
                id=uuid_lib.uuid4(),
                tenant_id=int(run.tenant_id),
                user_id=run.user_id,
                ingestion_run_id=run.id,
                engagement_id=run.engagement_id,
                task_id=run.task_id,
                source_execution_id=run.source_execution_id,
                observation_type=item.observation_type,
                subject_type=item.subject_type,
                subject_key=item.subject_key,
                assertion_level=item.assertion_level,
                dedupe_key=dedupe_key,
                payload=item.payload,
                observation_metadata=item.observation_metadata or None,
                observed_at=item.observed_at,
            )
            self.db.add(row)
            seen_in_batch.add(dedupe_key)
            inserted += 1

        self.db.flush()
        return inserted, duplicates

    def set_ingestion_run_status(
        self,
        *,
        ingestion_run_id: str,
        status: IngestionRunStatus,
        error_message: str | None = None,
    ) -> KnowledgeIngestionRun:
        run = self.db.execute(
            select(KnowledgeIngestionRun).where(KnowledgeIngestionRun.id == ingestion_run_id)
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"Ingestion run not found: {ingestion_run_id}")
        run.status = status.value
        run.error_message = error_message
        self.db.flush()
        return run

    def ingest_execution(
        self,
        *,
        task_id: int,
        source_execution_id: str,
        engagement_id: int | None = None,
        extractor_family: str | None = None,
        extractor_version: str | None = None,
        tool_name_hint: str | None = None,
        compact_output_hint: Mapping[str, Any] | None = None,
        post_tool_candidate_payload: Mapping[str, Any] | None = None,
        post_tool_candidate_usage: Mapping[str, Any] | None = None,
        delete_survival_required: bool = False,
        reuse_existing_archive_rows: bool = False,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Ingest one runtime execution into durable knowledge tables."""
        resolved_user_id, resolved_engagement_id, resolved_tenant_id = self._resolve_ownership_for_task(
            task_id=task_id,
            requested_engagement_id=engagement_id,
        )
        execution_payload = self.query_service.get_execution_by_id(
            execution_id=source_execution_id,
            task_id=task_id,
            include_artifacts=True,
        )
        if execution_payload is None:
            raise ValueError(
                f"Cannot ingest missing execution '{source_execution_id}' for task '{task_id}'"
            )
        return self.ingest_execution_payload(
            user_id=resolved_user_id,
            engagement_id=resolved_engagement_id,
            tenant_id=resolved_tenant_id,
            task_id=task_id,
            source_execution_id=source_execution_id,
            execution_payload=execution_payload,
            extractor_family=extractor_family,
            extractor_version=extractor_version,
            tool_name_hint=tool_name_hint,
            compact_output_hint=compact_output_hint,
            post_tool_candidate_payload=post_tool_candidate_payload,
            post_tool_candidate_usage=post_tool_candidate_usage,
            delete_survival_required=delete_survival_required,
            reuse_existing_archive_rows=reuse_existing_archive_rows,
            raise_on_error=raise_on_error,
        )

    def ingest_execution_payload(
        self,
        *,
        user_id: int | None = None,
        engagement_id: int,
        tenant_id: int | None = None,
        task_id: int | None,
        source_execution_id: str,
        execution_payload: Mapping[str, Any],
        extractor_family: str | None = None,
        extractor_version: str | None = None,
        tool_name_hint: str | None = None,
        compact_output_hint: Mapping[str, Any] | None = None,
        post_tool_candidate_payload: Mapping[str, Any] | None = None,
        post_tool_candidate_usage: Mapping[str, Any] | None = None,
        replay_source_type: str | None = None,
        delete_survival_required: bool = False,
        reuse_existing_archive_rows: bool = False,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Ingest one resolved replay/runtime payload into durable knowledge tables."""
        resolved_user_id = self._resolve_user_id(user_id=user_id, engagement_id=engagement_id)
        run = self.create_or_get_ingestion_run(
            IngestionRunCreate(
                tenant_id=(
                    int(tenant_id)
                    if tenant_id is not None
                    else self._resolve_tenant_id_from_engagement(engagement_id=int(engagement_id))
                ),
                user_id=resolved_user_id,
                engagement_id=int(engagement_id),
                task_id=task_id,
                source_execution_id=str(source_execution_id),
                extractor_family=str(extractor_family or self.DEFAULT_EXTRACTOR_FAMILY),
                extractor_version=str(extractor_version or self.DEFAULT_EXTRACTOR_VERSION),
                status=IngestionRunStatus.PENDING,
                metadata=self._build_run_metadata(
                    execution_payload=execution_payload,
                    extractor_family=str(extractor_family or self.DEFAULT_EXTRACTOR_FAMILY),
                    extractor_version=str(extractor_version or self.DEFAULT_EXTRACTOR_VERSION),
                    tool_name_hint=tool_name_hint,
                    compact_output_hint=compact_output_hint,
                    post_tool_candidate_payload=post_tool_candidate_payload,
                    replay_source_type=replay_source_type,
                ),
            )
        )
        self.set_ingestion_run_status(
            ingestion_run_id=str(run.id),
            status=IngestionRunStatus.RUNNING,
        )

        failure_stage = "archive"
        try:
            archived_rows = self._resolve_archived_rows_for_ingestion(
                engagement_id=int(engagement_id),
                task_id=task_id,
                source_execution_id=str(source_execution_id),
                delete_survival_required=delete_survival_required,
                reuse_existing_archive_rows=reuse_existing_archive_rows,
            )
            failure_stage = "adapter_extraction"
            observations, extraction_stats = self._extract_observations(
                execution_payload=dict(execution_payload),
                ingestion_run_id=str(run.id),
                tenant_id=int(run.tenant_id),
                user_id=resolved_user_id,
                engagement_id=int(engagement_id),
                task_id=task_id,
                compact_output_hint=compact_output_hint,
                archived_rows=archived_rows,
            )
            candidate_started = time.perf_counter()
            candidate_result = maybe_run_candidate_extraction(
                run=run,
                execution_payload=dict(execution_payload),
                archived_rows=archived_rows,
                deterministic_observations=observations,
                extraction_stats=extraction_stats,
                post_tool_candidate_payload=post_tool_candidate_payload,
                post_tool_candidate_usage=post_tool_candidate_usage,
                candidate_extractor_family=self.CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY,
                candidate_extractor_version=self.CANDIDATE_EXTRACTION_EXTRACTOR_VERSION,
                candidate_extraction_mode=self.CANDIDATE_EXTRACTION_MODE,
            )
            candidate_duration_seconds = max(0.0, time.perf_counter() - candidate_started)
            observations = [*observations, *list(candidate_result.observations)]
            failure_stage = "observation_insert"
            inserted_count, duplicate_count = self.insert_observations(
                ingestion_run_id=str(run.id),
                observations=observations,
            )
            persisted_observations = self._load_run_observations(ingestion_run_id=str(run.id))
            failure_stage = "projection"
            projection_metadata = self._run_projection(
                tenant_id=int(run.tenant_id),
                user_id=resolved_user_id,
                engagement_id=int(engagement_id),
                observations=persisted_observations,
            )
            semantic_metrics = self._build_semantic_metrics(
                extraction_stats=extraction_stats,
                projection_metadata=projection_metadata,
            )
            existing_run_metadata = dict(run.run_metadata or {})
            candidate_extractor_family = str(
                existing_run_metadata.get("candidate_extractor_family")
                or (
                    run.extractor_family
                    if str(run.extractor_family).strip() == self.CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY
                    else self.CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY
                )
            )
            candidate_extractor_version = str(
                existing_run_metadata.get("candidate_extractor_version")
                or (
                    run.extractor_version
                    if str(run.extractor_family).strip() == self.CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY
                    else self.CANDIDATE_EXTRACTION_EXTRACTOR_VERSION
                )
            )
            candidate_extraction_mode = str(
                existing_run_metadata.get("candidate_extraction_mode")
                or (
                    "candidate_replay"
                    if str(existing_run_metadata.get("replay_source_type") or "").strip().lower()
                    in {"runtime", "durable_archive"}
                    else self.CANDIDATE_EXTRACTION_MODE
                )
            )
            candidate_meta = build_candidate_run_metadata(
                candidate_result=candidate_result,
                existing_run_metadata=existing_run_metadata,
                candidate_duration_seconds=candidate_duration_seconds,
                minimum_confidence=get_knowledge_vulnerability_min_confidence(),
                candidate_extractor_family=candidate_extractor_family,
                candidate_extractor_version=candidate_extractor_version,
                candidate_extraction_mode=candidate_extraction_mode,
            )
            run.run_metadata = {
                **dict(run.run_metadata or {}),
                "archive_count": len(archived_rows),
                "observation_inserted_count": inserted_count,
                "observation_duplicate_count": duplicate_count,
                "adapter_stats": extraction_stats,
                "semantic_status": "succeeded"
                if projection_metadata.get("projection_status") == "succeeded"
                else "failed",
                **candidate_meta,
                "semantic_metrics": semantic_metrics,
                **projection_metadata,
            }
            record_candidate_usage_if_task_present(
                task_id=run.task_id,
                usage_summary=candidate_meta.get("candidate_usage_summary"),
                source_label=(
                    "knowledge_replay"
                    if str(candidate_extraction_mode).strip().lower() == "candidate_replay"
                    else "knowledge_candidate_extractor"
                ),
                source_execution_id=str(run.source_execution_id),
                ingestion_run_id=str(run.id),
                resolve_task_user_id=self._resolve_task_user_id,
                usage_tracking_service_factory=lambda: UsageTrackingService(self.db),
            )
            if projection_metadata.get("projection_status") != "succeeded":
                error_message = str(projection_metadata.get("projection_error") or "projection failed")
                self.set_ingestion_run_status(
                    ingestion_run_id=str(run.id),
                    status=IngestionRunStatus.FAILED,
                    error_message=error_message,
                )
                if raise_on_error:
                    raise RuntimeError(error_message)
                return {
                    "ok": False,
                    "ingestion_run_id": str(run.id),
                    "status": IngestionRunStatus.FAILED.value,
                    "error": error_message,
                }
            self.set_ingestion_run_status(
                ingestion_run_id=str(run.id),
                status=IngestionRunStatus.SUCCEEDED,
            )
            return {
                "ok": True,
                "ingestion_run_id": str(run.id),
                "status": IngestionRunStatus.SUCCEEDED.value,
                "archive_count": len(archived_rows),
                "observation_inserted_count": inserted_count,
                "observation_duplicate_count": duplicate_count,
                "projection_status": projection_metadata.get("projection_status"),
                "replay_source_type": replay_source_type,
                "candidate_extraction_status": candidate_meta["candidate_extraction_status"],
                "candidate_observation_count": candidate_meta["candidate_observation_count"],
                "asset_upsert_count": int(projection_metadata.get("asset_upsert_count") or 0),
                "service_upsert_count": int(projection_metadata.get("service_upsert_count") or 0),
                "finding_upsert_count": int(projection_metadata.get("finding_upsert_count") or 0),
                "relationship_upsert_count": int(projection_metadata.get("relationship_upsert_count") or 0),
                "asset_insert_count": int(projection_metadata.get("asset_insert_count") or 0),
                "service_insert_count": int(projection_metadata.get("service_insert_count") or 0),
                "finding_insert_count": int(projection_metadata.get("finding_insert_count") or 0),
                "relationship_insert_count": int(projection_metadata.get("relationship_insert_count") or 0),
                "web_path_upsert_count": int(projection_metadata.get("web_path_upsert_count") or 0),
                "web_path_insert_count": int(projection_metadata.get("web_path_insert_count") or 0),
                "engagement_id": int(engagement_id),
                "source_execution_id": str(source_execution_id),
            }
        except Exception as exc:
            return self._handle_ingestion_failure(
                run=run,
                failure_stage=failure_stage,
                exc=exc,
                raise_on_error=raise_on_error,
            )

    def _load_run_observations(self, *, ingestion_run_id: str) -> list[ObservationCreate]:
        """Load persisted run observations as the authoritative projection input."""
        rows = self.db.execute(
            select(KnowledgeObservation).where(
                KnowledgeObservation.ingestion_run_id == str(ingestion_run_id)
            ).order_by(KnowledgeObservation.observed_at.asc(), KnowledgeObservation.id.asc())
        ).scalars().all()
        return [
            ObservationCreate(
                user_id=int(row.user_id),
                tenant_id=int(row.tenant_id),
                engagement_id=int(row.engagement_id),
                task_id=row.task_id,
                source_execution_id=str(row.source_execution_id),
                ingestion_run_id=str(row.ingestion_run_id),
                observation_type=str(row.observation_type),
                subject_type=str(row.subject_type),
                subject_key=str(row.subject_key),
                assertion_level=str(row.assertion_level),
                payload=dict(row.payload or {}),
                observation_metadata=dict(row.observation_metadata or {}),
                observed_at=self._normalize_observed_at(row.observed_at),
            )
            for row in rows
        ]

    @staticmethod
    def _normalize_observed_at(value: datetime) -> datetime:
        return to_utc(value).replace(tzinfo=None)

    def _resolve_archived_rows_for_ingestion(
        self,
        *,
        engagement_id: int,
        task_id: int | None,
        source_execution_id: str,
        delete_survival_required: bool,
        reuse_existing_archive_rows: bool,
    ) -> list[KnowledgeEvidenceArchive]:
        if reuse_existing_archive_rows:
            existing = self.db.execute(
                select(KnowledgeEvidenceArchive).where(
                    KnowledgeEvidenceArchive.engagement_id == int(engagement_id),
                    KnowledgeEvidenceArchive.source_execution_id == str(source_execution_id),
                )
            ).scalars().all()
            if existing:
                return existing
            if task_id is None:
                return []

        if task_id is None:
            raise ValueError(
                "Cannot archive runtime execution artifacts without task context"
            )

        return self.archive_service.archive_execution_artifacts(
            engagement_id=engagement_id,
            task_id=task_id,
            execution_id=source_execution_id,
            delete_survival_required=delete_survival_required,
        )

    def ensure_task_delete_safe(
        self,
        *,
        task_id: int,
        engagement_id: int | None,
    ) -> dict[str, object]:
        return self.delete_guard_service.ensure_task_delete_safe(
            task_id=task_id,
            engagement_id=engagement_id,
        )

    def _resolve_ownership_for_task(
        self,
        *,
        task_id: int,
        requested_engagement_id: int | None,
    ) -> tuple[int, int, int]:
        """Return (user_id, engagement_id, tenant_id) for the given task."""
        task = self.db.execute(
            select(Task.id, Task.engagement_id, Task.user_id, Task.tenant_id).where(
                Task.id == int(task_id)
            )
        ).first()
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        task_engagement_id = task[1]
        task_user_id = task[2]
        task_tenant_id = task[3]
        if task_engagement_id is None:
            raise ValueError(f"Task {task_id} has no engagement_id")
        if task_user_id is None:
            raise ValueError(f"Task {task_id} has no user_id")
        if task_tenant_id is None:
            raise ValueError(f"Task {task_id} has no tenant_id")
        if requested_engagement_id is not None and int(requested_engagement_id) != int(task_engagement_id):
            raise ValueError(
                "Requested engagement_id does not match task engagement_id for ingestion orchestration"
            )
        return int(task_user_id), int(task_engagement_id), int(task_tenant_id)

    def _resolve_user_id(self, *, user_id: int | None, engagement_id: int) -> int:
        """Resolve user_id from explicit value or from the engagement row."""
        if user_id is not None:
            return int(user_id)
        from backend.models import Engagement
        eng = self.db.execute(
            select(Engagement.user_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if eng is None:
            raise ValueError(f"Engagement not found: {engagement_id}")
        return int(eng)

    def _resolve_tenant_id_from_engagement(self, *, engagement_id: int) -> int:
        """Resolve tenant_id from durable engagement ownership context."""
        from backend.models import Engagement

        tenant_id = self.db.execute(
            select(Engagement.tenant_id).where(Engagement.id == int(engagement_id))
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(f"Engagement not found: {engagement_id}")
        return int(tenant_id)

    def _build_run_metadata(
        self,
        *,
        execution_payload: dict[str, Any],
        extractor_family: str,
        extractor_version: str,
        tool_name_hint: str | None,
        compact_output_hint: Mapping[str, Any] | None,
        post_tool_candidate_payload: Mapping[str, Any] | None,
        replay_source_type: str | None,
    ) -> dict[str, Any]:
        execution = execution_payload.get("execution") or {}
        metadata: dict[str, Any] = {
            "source_tool_name": str(execution.get("tool_name") or tool_name_hint or ""),
            "artifact_count": len(execution_payload.get("artifacts") or []),
            "run_extractor_family": str(extractor_family),
            "run_extractor_version": str(extractor_version),
        }
        if replay_source_type:
            metadata["replay_source_type"] = str(replay_source_type)
        metadata["semantic_input_snapshot"] = build_semantic_input_snapshot(
            execution=execution,
            artifacts=list(execution_payload.get("artifacts") or []),
        )
        if compact_output_hint:
            metadata["compact_output_hint_keys"] = sorted(str(key) for key in compact_output_hint.keys())
        if isinstance(post_tool_candidate_payload, Mapping):
            raw_rows = post_tool_candidate_payload.get("candidate_observations")
            candidate_row_count = len(raw_rows) if isinstance(raw_rows, list) else 0
            metadata["post_tool_candidate_payload_present"] = True
            metadata["post_tool_candidate_row_count"] = max(0, int(candidate_row_count))
        return metadata

    @staticmethod
    def _build_semantic_metrics(
        *,
        extraction_stats: Mapping[str, Any],
        projection_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "adapter_dispatch_count_total": int(extraction_stats.get("resolved_adapter_count") or 0),
            "adapter_dispatch_count_by_tool": dict(extraction_stats.get("adapter_dispatch_count_by_tool") or {}),
            "adapter_dispatch_count_by_family": dict(
                extraction_stats.get("adapter_dispatch_count_by_family") or {}
            ),
            "zero_observation_run_count": int(extraction_stats.get("zero_observation_run_count") or 0),
            "zero_observation_by_tool": dict(extraction_stats.get("zero_observation_by_tool") or {}),
            "projection_upsert_count_by_model": {
                "asset": int(projection_metadata.get("asset_upsert_count") or 0),
                "service": int(projection_metadata.get("service_upsert_count") or 0),
                "finding": int(projection_metadata.get("finding_upsert_count") or 0),
                "relationship": int(projection_metadata.get("relationship_upsert_count") or 0),
                "web_path": int(projection_metadata.get("web_path_upsert_count") or 0),
            },
            "projection_contradiction_count": int(
                projection_metadata.get("projection_contradiction_count") or 0
            ),
            "projection_contradiction_count_by_domain": dict(
                projection_metadata.get("projection_contradiction_count_by_domain") or {}
            ),
        }

    def _extract_observations(
        self,
        *,
        execution_payload: dict[str, Any],
        ingestion_run_id: str,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int | None,
        compact_output_hint: Mapping[str, Any] | None,
        archived_rows: Iterable[KnowledgeEvidenceArchive] | None = None,
    ) -> tuple[list[ObservationCreate], dict[str, Any]]:
        """Delegate extraction to the adapter registry's extract_with_stats."""
        extract_with_stats = getattr(self.adapter_registry, "extract_with_stats", None)
        if callable(extract_with_stats):
            try:
                return extract_with_stats(
                    execution_payload=execution_payload,
                    ingestion_run_id=ingestion_run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    engagement_id=engagement_id,
                    task_id=task_id,
                    compact_output_hint=compact_output_hint,
                    legacy_extractors=self.extractors,
                    artifact_memory_service=self.artifact_memory_service,
                    max_artifact_chars=self.ADAPTER_ARTIFACT_READ_MAX_CHARS,
                    evidence_archives=archived_rows,
                )
            except TypeError:
                return extract_with_stats(
                    execution_payload=execution_payload,
                    ingestion_run_id=ingestion_run_id,
                    user_id=user_id,
                    engagement_id=engagement_id,
                    task_id=task_id,
                    compact_output_hint=compact_output_hint,
                    legacy_extractors=self.extractors,
                    artifact_memory_service=self.artifact_memory_service,
                    max_artifact_chars=self.ADAPTER_ARTIFACT_READ_MAX_CHARS,
                )
        execution = execution_payload.get("execution")
        execution_dict = dict(execution) if isinstance(execution, Mapping) else {}
        try:
            context = self.adapter_registry.build_context(
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                tenant_id=tenant_id,
                source_execution_id=str(execution_dict.get("execution_id") or ""),
                ingestion_run_id=ingestion_run_id,
                execution_payload=execution_payload,
                compact_output_hint=compact_output_hint,
                evidence_archives=archived_rows,
            )
        except TypeError:
            context = self.adapter_registry.build_context(
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                source_execution_id=str(execution_dict.get("execution_id") or ""),
                ingestion_run_id=ingestion_run_id,
                execution_payload=execution_payload,
                compact_output_hint=compact_output_hint,
            )
        resolved_adapters = self.adapter_registry.resolve_adapters(context)
        adapter_observations: list[ObservationCreate] = []
        for adapter in resolved_adapters:
            adapter_observations.extend(adapter.extract(context))
        legacy_observations: list[ObservationCreate] = []
        for extractor in self.extractors:
            legacy_observations.extend(
                extractor(execution_payload, ingestion_run_id, engagement_id, task_id, compact_output_hint)
            )
        return [*adapter_observations, *legacy_observations], {}

    def _run_projection(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        observations: list[ObservationCreate],
    ) -> dict[str, Any]:
        """Delegate projection with retry to the projection service."""
        svc = self.projection_service
        project_with_retry = getattr(svc, "project_with_retry", None)
        if callable(project_with_retry):
            try:
                return project_with_retry(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    engagement_id=engagement_id,
                    observations=observations,
                    retry_attempts=self.PROJECTION_IMMEDIATE_RETRY_ATTEMPTS,
                    error_sanitizer=self._build_safe_error_details,
                )
            except TypeError:
                return project_with_retry(
                    user_id=user_id,
                    engagement_id=engagement_id,
                    observations=observations,
                    retry_attempts=self.PROJECTION_IMMEDIATE_RETRY_ATTEMPTS,
                    error_sanitizer=self._build_safe_error_details,
                )
        attempt_count = 1 + self.PROJECTION_IMMEDIATE_RETRY_ATTEMPTS
        last_error: Exception | None = None
        last_error_details: dict[str, Any] | None = None
        for attempt in range(1, attempt_count + 1):
            try:
                with self.db.begin_nested():
                    try:
                        projection = svc.project_observations(
                            tenant_id=int(tenant_id),
                            user_id=int(user_id),
                            engagement_id=int(engagement_id),
                            observations=observations,
                        )
                    except TypeError:
                        try:
                            projection = svc.project_observations(
                                user_id=int(user_id),
                                engagement_id=int(engagement_id),
                                observations=observations,
                            )
                        except TypeError:
                            projection = svc.project_observations(
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
                last_error_details = self._build_safe_error_details(stage="projection", error=exc)
                continue
        safe = last_error_details or {
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
            "projection_error": safe["message"],
            "projection_error_class": safe["error_class"],
            "projection_error_fingerprint": safe["fingerprint"],
            "projection_error_redacted": safe["redacted"],
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

    def _resolve_task_user_id(self, task_id: int) -> int | None:
        """Resolve user_id for a task row; used as callable by usage tracking."""
        row = self.db.execute(
            select(Task.id, Task.user_id).where(Task.id == int(task_id))
        ).first()
        if row is None:
            return None
        return int(row[1])

    def _handle_ingestion_failure(
        self,
        *,
        run: KnowledgeIngestionRun,
        failure_stage: str,
        exc: Exception,
        raise_on_error: bool,
    ) -> dict[str, Any]:
        """Record failure metadata and return error envelope."""
        safe_error = self._build_safe_error_details(stage=failure_stage, error=exc)
        existing_metadata = dict(run.run_metadata or {})
        existing_semantic_metrics = dict(existing_metadata.get("semantic_metrics") or {})
        existing_semantic_metrics.setdefault("adapter_dispatch_count_total", 0)
        existing_semantic_metrics.setdefault("zero_observation_run_count", 0)
        existing_semantic_metrics.setdefault("projection_contradiction_count", 0)
        existing_semantic_metrics.setdefault("projection_contradiction_count_by_domain", {})
        run.run_metadata = {
            **existing_metadata,
            "semantic_status": "failed",
            "semantic_failure_stage": failure_stage,
            "semantic_failure_reason": safe_error["message"],
            "semantic_failure_error_class": safe_error["error_class"],
            "semantic_failure_fingerprint": safe_error["fingerprint"],
            "semantic_failure_redacted": safe_error["redacted"],
            "semantic_metrics": existing_semantic_metrics,
        }
        self.db.flush()
        self.set_ingestion_run_status(
            ingestion_run_id=str(run.id),
            status=IngestionRunStatus.FAILED,
            error_message=safe_error["message"],
        )
        if raise_on_error:
            raise exc
        return {
            "ok": False,
            "ingestion_run_id": str(run.id),
            "status": IngestionRunStatus.FAILED.value,
            "error": safe_error["message"],
        }

    @classmethod
    def _sanitize_error_text(cls, raw_message: str) -> tuple[str, bool]:
        message = str(raw_message or "").strip()
        if not message:
            return "operation failed", False

        redacted = False
        for pattern in cls._SENSITIVE_ERROR_PATTERNS:
            if pattern.search(message):
                redacted = True
            if "bearer" in pattern.pattern.lower():
                message = pattern.sub("Bearer <REDACTED>", message)
            elif "eyj" in pattern.pattern.lower():
                message = pattern.sub("<REDACTED_JWT>", message)
            else:
                message = pattern.sub(lambda match: f"{match.group(1)}=<REDACTED>", message)

        if len(message) > cls.MAX_SAFE_ERROR_CHARS:
            message = f"{message[: cls.MAX_SAFE_ERROR_CHARS - 3]}..."
        return message, redacted

    @classmethod
    def _build_safe_error_details(cls, *, stage: str, error: Exception) -> dict[str, Any]:
        raw_message = str(error or "")
        sanitized, redacted = cls._sanitize_error_text(raw_message)
        stage_name = str(stage or "unknown").strip().lower() or "unknown"
        error_class = error.__class__.__name__
        fingerprint = hashlib.sha256(raw_message.encode("utf-8")).hexdigest()[:16]
        return {
            "message": f"{stage_name} failed [{error_class}]: {sanitized}",
            "error_class": error_class,
            "fingerprint": fingerprint,
            "redacted": redacted,
        }
