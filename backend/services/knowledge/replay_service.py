"""Replay one execution into a new durable ingestion run without task re-execution.

Scope:
- Provide a replay boundary for rerunning ingestion on stored provenance.

Responsibilities:
- Resolve replay extractor identity/version for a new run.
- Trigger ingestion orchestration for the same source execution.
- Preserve previous run history by forcing a new extractor-version tuple.

Boundary:
- This service orchestrates replay intent only.
- Ingestion/archival/observation writes remain owned by KnowledgeIngestionService."""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config.feature_flags import is_knowledge_candidate_extraction_enabled
from backend.core.time_utils import format_iso, utc_now
from ...models import KnowledgeIngestionRun
from backend.services.metrics.utils import safe_gauge, safe_inc
from .ingestion_service import KnowledgeIngestionService
from .replay_source_resolver import KnowledgeReplaySourceResolver


class KnowledgeReplayService:
    """Replay execution ingestion units through KnowledgeIngestionService."""

    DEFAULT_REPLAY_EXTRACTOR_FAMILY = "runtime.ingestion"

    def __init__(
        self,
        db: Session,
        *,
        ingestion_service: KnowledgeIngestionService | None = None,
        replay_source_resolver: KnowledgeReplaySourceResolver | None = None,
    ) -> None:
        self.db = db
        self.ingestion_service = ingestion_service or KnowledgeIngestionService(db)
        self.replay_source_resolver = replay_source_resolver or KnowledgeReplaySourceResolver(
            db,
            query_service=self.ingestion_service.query_service,
        )

    def replay_execution(
        self,
        *,
        task_id: int | None,
        source_execution_id: str,
        extractor_family: str | None = None,
        target_extractor_version: str | None = None,
    ) -> dict[str, object]:
        """
        Replay ingestion for one execution and create a new durable run record.

        If `target_extractor_version` is omitted, a replay-suffixed version is
        generated so replay history cannot overwrite prior runs.
        """
        started = time.perf_counter()
        try:
            family = str(extractor_family or self.DEFAULT_REPLAY_EXTRACTOR_FAMILY)
            normalized_family = family.strip().lower()
            if (
                normalized_family == KnowledgeIngestionService.CANDIDATE_EXTRACTION_EXTRACTOR_FAMILY
                and not is_knowledge_candidate_extraction_enabled()
            ):
                raise ValueError(
                    "Candidate replay is disabled because "
                    "ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION is false"
                )
            if target_extractor_version is not None:
                replay_version = str(target_extractor_version)
                if self.extractor_version_exists(
                    source_execution_id=source_execution_id,
                    extractor_family=family,
                    extractor_version=replay_version,
                ):
                    raise ValueError(
                        "Replay extractor_version already exists for this execution/family; "
                        "choose a newer extractor_version"
                    )
            else:
                replay_version = self._next_replay_version(
                    source_execution_id=source_execution_id,
                    extractor_family=family,
                )
            resolved_source = self.replay_source_resolver.resolve_source(
                source_execution_id=source_execution_id,
                task_id=task_id,
            )
            result = self.ingestion_service.ingest_execution_payload(
                engagement_id=int(resolved_source["engagement_id"]),
                task_id=resolved_source.get("task_id"),
                source_execution_id=source_execution_id,
                execution_payload=resolved_source["execution_payload"],
                extractor_family=family,
                extractor_version=str(replay_version),
                tool_name_hint=None,
                compact_output_hint=resolved_source.get("compact_output_hint"),
                replay_source_type=str(resolved_source.get("source_kind") or ""),
                delete_survival_required=False,
                reuse_existing_archive_rows=True,
                raise_on_error=True,
            )
        except Exception:
            safe_inc("knowledge_replay_failed_total")
            safe_gauge(
                "knowledge_replay_duration_seconds",
                max(0.0, time.perf_counter() - started),
            )
            raise

        replay_duration_seconds = max(0.0, time.perf_counter() - started)
        safe_inc("knowledge_replay_total")
        safe_gauge("knowledge_replay_duration_seconds", replay_duration_seconds)
        run_metadata: dict[str, object] = {}
        run_id = result.get("ingestion_run_id")
        if run_id is not None:
            run = self.db.execute(
                select(KnowledgeIngestionRun).where(KnowledgeIngestionRun.id == run_id)
            ).scalar_one_or_none()
            if run is not None:
                current_metadata = dict(run.run_metadata or {})
                candidate_usage = current_metadata.get("candidate_usage_summary")
                if isinstance(candidate_usage, dict):
                    replay_usage_summary: dict[str, object] = {
                        "input_tokens": int(candidate_usage.get("input_tokens") or 0),
                        "output_tokens": int(candidate_usage.get("output_tokens") or 0),
                        "total_tokens": int(candidate_usage.get("total_tokens") or 0),
                        "estimated_cost_usd": float(candidate_usage.get("estimated_cost_usd") or 0.0),
                    }
                else:
                    replay_usage_summary = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "estimated_cost_usd": 0.0,
                    }
                current_metadata["replay_usage_summary"] = replay_usage_summary
                current_metadata["replay_audit_summary"] = {
                    "status": str(result.get("status") or "unknown"),
                    "outcome_ok": bool(result.get("ok")),
                    "replay_source_type": str(resolved_source.get("source_kind") or ""),
                    "duration_seconds": replay_duration_seconds,
                    "replayed_at": format_iso(utc_now()),
                    "usage_summary": replay_usage_summary,
                }
                run.run_metadata = current_metadata
                self.db.flush()
            if run is not None and isinstance(run.run_metadata, dict):
                run_metadata = dict(run.run_metadata)
        return {
            "ok": bool(result.get("ok")),
            "ingestion_run_id": result.get("ingestion_run_id"),
            "status": result.get("status"),
            "extractor_family": family,
            "extractor_version": str(replay_version),
            "projection_status": result.get("projection_status"),
            "replay_source_type": str(resolved_source.get("source_kind") or ""),
            "candidate_outcome_summary": {
                "status": run_metadata.get("candidate_extraction_status"),
                "reason": run_metadata.get("candidate_extraction_reason"),
                "observation_count": run_metadata.get("candidate_observation_count"),
                "evidence_count": run_metadata.get("candidate_evidence_count"),
                "extractor_family": run_metadata.get("candidate_extractor_family"),
                "extractor_version": run_metadata.get("candidate_extractor_version"),
                "extraction_mode": run_metadata.get("candidate_extraction_mode"),
            },
        }

    def extractor_version_exists(
        self,
        *,
        source_execution_id: str,
        extractor_family: str,
        extractor_version: str,
    ) -> bool:
        """Return whether one execution already has the replay extractor tuple."""
        row = self.db.execute(
            select(KnowledgeIngestionRun.id).where(
                KnowledgeIngestionRun.source_execution_id == str(source_execution_id),
                KnowledgeIngestionRun.extractor_family == str(extractor_family),
                KnowledgeIngestionRun.extractor_version == str(extractor_version),
            )
        ).first()
        return row is not None

    def _next_replay_version(
        self,
        *,
        source_execution_id: str,
        extractor_family: str,
    ) -> str:
        existing_count = self.db.execute(
            select(func.count(KnowledgeIngestionRun.id)).where(
                KnowledgeIngestionRun.source_execution_id == str(source_execution_id),
                KnowledgeIngestionRun.extractor_family == str(extractor_family),
            )
        ).scalar_one()
        next_number = int(existing_count or 0) + 1
        return f"replay.{next_number}"
