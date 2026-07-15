"""Operational historical backfill gate for durable knowledge read models.

Scope:
- Execute engagement-scoped read-model rebuild/backfill for historical observations.
- Capture explicit per-engagement status for completion-gate reporting.
- Verify idempotent rerun behavior by checking read-model row counts after a second rerun.

Boundary:
- Internal service only; no public API surface.
- Uses existing KnowledgeReadModelRebuildService as the canonical rebuild executor."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config.feature_flags import is_knowledge_candidate_extraction_enabled
from ...models import (
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from .read_model_rebuild_service import KnowledgeReadModelRebuildService
from backend.services.metrics.utils import safe_inc


class KnowledgeHistoricalBackfillService:
    """Run and verify historical engagement backfill completion gate."""

    CANDIDATE_REPLAY_EXTRACTOR_FAMILY = "llm.candidate_extraction"

    def __init__(
        self,
        db: Session,
        *,
        rebuild_service: KnowledgeReadModelRebuildService | None = None,
    ) -> None:
        self.db = db
        self.rebuild_service = rebuild_service or KnowledgeReadModelRebuildService(db)

    def run_backfill(
        self,
        *,
        target_engagement_ids: Sequence[int] | None = None,
        verify_idempotent_rerun: bool = True,
    ) -> dict[str, Any]:
        """Execute one historical backfill pass and evaluate completion-gate criteria."""
        engagement_ids = self._resolve_target_engagement_ids(target_engagement_ids)
        engagement_statuses: list[dict[str, Any]] = []
        failed_engagements: list[dict[str, Any]] = []

        for engagement_id in engagement_ids:
            status = self._run_one_engagement(
                engagement_id=engagement_id,
                verify_idempotent_rerun=verify_idempotent_rerun,
            )
            engagement_statuses.append(status)
            if status.get("status") != "succeeded":
                failed_engagements.append(
                    {
                        "engagement_id": int(engagement_id),
                        "error_reason": str(status.get("error_reason") or "unknown"),
                        "rerun_plan": {
                            "operation": "knowledge_read_model_rebuild_service.rebuild_engagement",
                            "params": {"engagement_id": int(engagement_id)},
                        },
                    }
                )

        all_targeted_attempted = len(engagement_statuses) == len(engagement_ids)
        idempotent_checked = bool(verify_idempotent_rerun)
        idempotent_gate_ok = all(
            bool(status.get("idempotent_rerun", {}).get("ok"))
            for status in engagement_statuses
            if status.get("status") == "succeeded"
        )
        completion_gate_passed = (
            all_targeted_attempted
            and not failed_engagements
            and (idempotent_gate_ok if idempotent_checked else False)
        )
        web_path_upsert_total, web_path_insert_total = self._sum_web_path_projection_counters(
            engagement_statuses
        )
        safe_inc("knowledge_backfill_total", max(0, len(engagement_statuses)))

        return {
            "ok": completion_gate_passed,
            "completion_gate_passed": completion_gate_passed,
            "idempotent_rerun_verified": idempotent_checked,
            "targeted_engagement_count": len(engagement_ids),
            "attempted_engagement_count": len(engagement_statuses),
            "succeeded_engagement_count": sum(
                1 for status in engagement_statuses if status.get("status") == "succeeded"
            ),
            "failed_engagement_count": len(failed_engagements),
            "failed_engagements": failed_engagements,
            "engagement_statuses": engagement_statuses,
            "web_path_upsert_count": web_path_upsert_total,
            "web_path_insert_count": web_path_insert_total,
        }

    def verify_after_replay_backfill(
        self,
        *,
        target_engagement_ids: Sequence[int] | None = None,
        verify_idempotent_rerun: bool = True,
        replay_extractor_family: str = "llm.candidate_extraction",
        replay_extractor_version: str | None = None,
        require_replay_runs: bool = True,
    ) -> dict[str, Any]:
        """Run post-replay/backfill verification using existing rebuild/idempotency gate."""
        normalized_family = str(replay_extractor_family).strip().lower()
        if (
            normalized_family == self.CANDIDATE_REPLAY_EXTRACTOR_FAMILY
            and not is_knowledge_candidate_extraction_enabled()
        ):
            return {
                "ok": False,
                "completion_gate_passed": False,
                "verification_scope": "post_replay_backfill",
                "replay_extractor_family": str(replay_extractor_family),
                "replay_extractor_version": replay_extractor_version,
                "idempotent_rerun_verified": bool(verify_idempotent_rerun),
                "targeted_engagement_count": 0,
                "attempted_engagement_count": 0,
                "succeeded_engagement_count": 0,
                "failed_engagement_count": 0,
                "failed_engagements": [],
                "engagement_statuses": [],
                "error_reason": (
                    "Candidate replay verification is disabled because "
                    "ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION is false"
                ),
                "rerun_plan": {
                    "operation": "set ENABLE_KNOWLEDGE_CANDIDATE_EXTRACTION=true",
                    "params": {
                        "extractor_family": str(replay_extractor_family),
                        "target_extractor_version": replay_extractor_version,
                    },
                },
            }
        if target_engagement_ids is not None:
            engagement_ids = self._normalize_engagement_ids(target_engagement_ids)
        else:
            engagement_ids = self._resolve_engagement_ids_from_replay_runs(
                replay_extractor_family=str(replay_extractor_family),
                replay_extractor_version=replay_extractor_version,
            )

        if not engagement_ids:
            if require_replay_runs:
                error_reason = (
                    "No matching replay/backfill ingestion runs found for the requested "
                    "extractor selector"
                )
                rerun_plan: dict[str, Any] | None = {
                    "operation": "knowledge_replay_service.replay_execution",
                    "params": {
                        "extractor_family": str(replay_extractor_family),
                        "target_extractor_version": replay_extractor_version,
                    },
                }
            else:
                error_reason = "No engagements selected for post-replay/backfill verification"
                rerun_plan = None
            return {
                "ok": False,
                "completion_gate_passed": False,
                "verification_scope": "post_replay_backfill",
                "replay_extractor_family": str(replay_extractor_family),
                "replay_extractor_version": replay_extractor_version,
                "idempotent_rerun_verified": bool(verify_idempotent_rerun),
                "targeted_engagement_count": 0,
                "attempted_engagement_count": 0,
                "succeeded_engagement_count": 0,
                "failed_engagement_count": 0,
                "failed_engagements": [],
                "engagement_statuses": [],
                "error_reason": error_reason,
                "rerun_plan": rerun_plan,
            }

        engagement_statuses: list[dict[str, Any]] = []
        failed_engagements: list[dict[str, Any]] = []
        for engagement_id in engagement_ids:
            replay_context = self._build_replay_context(
                engagement_id=int(engagement_id),
                replay_extractor_family=str(replay_extractor_family),
                replay_extractor_version=replay_extractor_version,
            )
            if require_replay_runs and int(replay_context.get("matching_run_count") or 0) <= 0:
                status = {
                    "engagement_id": int(engagement_id),
                    "attempted": False,
                    "status": "failed",
                    "error_reason": "No matching replay/backfill ingestion runs found for engagement",
                    "result": None,
                    "idempotent_rerun": {
                        "checked": False,
                        "ok": None,
                        "before_counts": None,
                        "after_counts": None,
                        "error_reason": None,
                    },
                    "replay_context": replay_context,
                }
            else:
                status = self._run_one_engagement(
                    engagement_id=int(engagement_id),
                    verify_idempotent_rerun=verify_idempotent_rerun,
                )
                status["replay_context"] = replay_context
            engagement_statuses.append(status)
            if status.get("status") != "succeeded":
                failed_engagements.append(
                    {
                        "engagement_id": int(engagement_id),
                        "error_reason": str(status.get("error_reason") or "unknown"),
                        "rerun_plan": {
                            "operation": "knowledge_replay_service.replay_execution",
                            "params": {
                                "extractor_family": str(replay_extractor_family),
                                "target_extractor_version": replay_extractor_version,
                            },
                        },
                    }
                )

        all_targeted_attempted = len(engagement_statuses) == len(engagement_ids)
        idempotent_checked = bool(verify_idempotent_rerun)
        idempotent_gate_ok = all(
            bool(status.get("idempotent_rerun", {}).get("ok"))
            for status in engagement_statuses
            if status.get("status") == "succeeded"
        )
        completion_gate_passed = (
            all_targeted_attempted
            and not failed_engagements
            and (idempotent_gate_ok if idempotent_checked else True)
        )
        web_path_upsert_total, web_path_insert_total = self._sum_web_path_projection_counters(
            engagement_statuses
        )
        return {
            "ok": completion_gate_passed,
            "completion_gate_passed": completion_gate_passed,
            "verification_scope": "post_replay_backfill",
            "replay_extractor_family": str(replay_extractor_family),
            "replay_extractor_version": replay_extractor_version,
            "idempotent_rerun_verified": idempotent_checked,
            "targeted_engagement_count": len(engagement_ids),
            "attempted_engagement_count": len(engagement_statuses),
            "succeeded_engagement_count": sum(
                1 for status in engagement_statuses if status.get("status") == "succeeded"
            ),
            "failed_engagement_count": len(failed_engagements),
            "failed_engagements": failed_engagements,
            "engagement_statuses": engagement_statuses,
            "web_path_upsert_count": web_path_upsert_total,
            "web_path_insert_count": web_path_insert_total,
        }

    def _run_one_engagement(
        self,
        *,
        engagement_id: int,
        verify_idempotent_rerun: bool,
    ) -> dict[str, Any]:
        status: dict[str, Any] = {
            "engagement_id": int(engagement_id),
            "attempted": True,
            "status": "failed",
            "error_reason": None,
            "result": None,
            "idempotent_rerun": {
                "checked": False,
                "ok": None,
                "before_counts": None,
                "after_counts": None,
                "error_reason": None,
            },
        }

        try:
            result = self.rebuild_service.rebuild_engagement(engagement_id=int(engagement_id))
            status["status"] = "succeeded"
            status["result"] = dict(result or {})
        except Exception as exc:
            status["error_reason"] = self._safe_error_reason(exc)
            return status

        if not verify_idempotent_rerun:
            return status

        idempotent = status["idempotent_rerun"]
        idempotent["checked"] = True
        try:
            before_counts = self._read_model_counts_for_engagement(engagement_id=int(engagement_id))
            self.rebuild_service.rebuild_engagement(engagement_id=int(engagement_id))
            after_counts = self._read_model_counts_for_engagement(engagement_id=int(engagement_id))
            idempotent["before_counts"] = before_counts
            idempotent["after_counts"] = after_counts
            idempotent["ok"] = before_counts == after_counts
            if not idempotent["ok"]:
                status["status"] = "failed"
                status["error_reason"] = "Idempotent rerun check failed: read-model counts changed"
        except Exception as exc:
            idempotent["ok"] = False
            idempotent["error_reason"] = self._safe_error_reason(exc)
            status["status"] = "failed"
            status["error_reason"] = f"Idempotent rerun failed: {idempotent['error_reason']}"

        return status

    def _resolve_target_engagement_ids(self, requested: Sequence[int] | None) -> list[int]:
        if requested is not None:
            return self._normalize_engagement_ids(requested)
        rows = self.db.execute(
            select(KnowledgeObservation.engagement_id)
            .distinct()
            .order_by(KnowledgeObservation.engagement_id.asc())
        ).all()
        return [int(row[0]) for row in rows]

    def _resolve_engagement_ids_from_replay_runs(
        self,
        *,
        replay_extractor_family: str,
        replay_extractor_version: str | None,
    ) -> list[int]:
        query = select(KnowledgeIngestionRun.engagement_id).where(
            KnowledgeIngestionRun.extractor_family == str(replay_extractor_family)
        )
        if replay_extractor_version is not None:
            query = query.where(KnowledgeIngestionRun.extractor_version == str(replay_extractor_version))
        rows = self.db.execute(
            query.distinct().order_by(KnowledgeIngestionRun.engagement_id.asc())
        ).all()
        return [int(row[0]) for row in rows]

    def _build_replay_context(
        self,
        *,
        engagement_id: int,
        replay_extractor_family: str,
        replay_extractor_version: str | None,
    ) -> dict[str, Any]:
        run_query = select(KnowledgeIngestionRun).where(
            KnowledgeIngestionRun.engagement_id == int(engagement_id),
            KnowledgeIngestionRun.extractor_family == str(replay_extractor_family),
        )
        if replay_extractor_version is not None:
            run_query = run_query.where(
                KnowledgeIngestionRun.extractor_version == str(replay_extractor_version)
            )
        runs = self.db.execute(run_query).scalars().all()
        source_execution_ids = sorted(
            {
                str(run.source_execution_id)
                for run in runs
                if str(run.source_execution_id).strip()
            }
        )
        return {
            "matching_run_count": len(runs),
            "matched_source_execution_ids": source_execution_ids,
        }

    @staticmethod
    def _normalize_engagement_ids(values: Iterable[int]) -> list[int]:
        normalized = sorted({int(value) for value in values})
        for engagement_id in normalized:
            if engagement_id <= 0:
                raise ValueError("engagement_id values must be positive integers")
        return normalized

    def _read_model_counts_for_engagement(self, *, engagement_id: int) -> dict[str, int]:
        return {
            "assets": self._count_for_model(KnowledgeAsset, engagement_id=engagement_id),
            "services": self._count_for_model(KnowledgeService, engagement_id=engagement_id),
            "findings": self._count_for_model(KnowledgeFinding, engagement_id=engagement_id),
            "relationships": self._count_for_model(KnowledgeRelationship, engagement_id=engagement_id),
            "web_paths": self._count_web_paths_for_engagement(engagement_id=engagement_id),
            "engagement_web_path_links": self._count_for_model(
                EngagementWebPathLink,
                engagement_id=engagement_id,
            ),
        }

    def _count_for_model(self, model: Any, *, engagement_id: int) -> int:
        return int(
            self.db.execute(
                select(func.count(model.id)).where(model.engagement_id == int(engagement_id))
            ).scalar_one()
            or 0
        )

    def _count_web_paths_for_engagement(self, *, engagement_id: int) -> int:
        return int(
            self.db.execute(
                select(func.count(KnowledgeWebPath.id))
                .join(
                    EngagementWebPathLink,
                    EngagementWebPathLink.web_path_id == KnowledgeWebPath.id,
                )
                .where(EngagementWebPathLink.engagement_id == int(engagement_id))
            ).scalar_one()
            or 0
        )

    @staticmethod
    def _sum_web_path_projection_counters(
        engagement_statuses: Sequence[dict[str, Any]],
    ) -> tuple[int, int]:
        upsert_total = 0
        insert_total = 0
        for status in engagement_statuses:
            result = status.get("result")
            if not isinstance(result, dict):
                continue
            upsert_total += int(result.get("web_path_upsert_count") or 0)
            insert_total += int(result.get("web_path_insert_count") or 0)
        return upsert_total, insert_total

    @staticmethod
    def _safe_error_reason(error: Exception) -> str:
        return f"{error.__class__.__name__}: {str(error)}"
