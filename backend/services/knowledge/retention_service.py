""" retention orchestration for operational logs and durable evidence policy.

Scope:
- Apply explicit retention classes to operational and durable knowledge surfaces.
- Purge expired operational logs with dry-run support.
- Evaluate and apply evidence archive compaction eligibility without unsafe deletion.

Boundary:
- Delete-guard wiring is handled separately in task cleanup orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import time
from typing import Any, Literal

from sqlalchemy.orm import Session

from backend.config import REASONING_RETENTION_DAYS
from backend.core.time_utils import utc_now
from backend.models.chat import AgentLog
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeFinding, KnowledgeIngestionRun
from backend.services.data_plane.retention_service import (
    ArtifactObjectRetentionResult,
    DataPlaneRetentionService,
)
from backend.models.streaming import StreamEvent, SystemLog
from .archive_service import KnowledgeArchiveService
from backend.services.metrics.utils import safe_gauge, safe_inc

RETENTION_CLASS_OPERATIONAL_EPHEMERAL = "operational_ephemeral"
RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE = "engagement_knowledge"

RetentionClass = Literal[
    "artifact_payload",
    "engagement_knowledge",
    "operational_ephemeral",
]
EvidenceRetentionAction = Literal[
    "preserve_active_finding",
    "preserve_replay_policy",
    "preserve_non_archived_mode",
    "eligible_for_compaction",
]


@dataclass(frozen=True, slots=True)
class OperationalLogRetentionRule:
    """One explicit operational-log retention rule."""

    name: str
    retention_class: RetentionClass
    max_age_days: int
    model: Any
    timestamp_field: str


@dataclass(frozen=True, slots=True)
class OperationalLogCleanupResult:
    """One rule execution result for operational-log cleanup."""

    name: str
    retention_class: RetentionClass
    max_age_days: int
    cutoff_iso: str
    candidate_count: int
    deleted_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "retention_class": self.retention_class,
            "max_age_days": self.max_age_days,
            "cutoff": self.cutoff_iso,
            "candidate_count": self.candidate_count,
            "deleted_count": self.deleted_count,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRetentionDecision:
    """Per-evidence policy decision for retention/compaction eligibility."""

    evidence_id: str
    source_execution_id: str
    storage_mode: str
    retention_class: RetentionClass
    action: EvidenceRetentionAction
    reason: str
    replay_policy_status: str = "not_required"

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source_execution_id": self.source_execution_id,
            "storage_mode": self.storage_mode,
            "retention_class": self.retention_class,
            "action": self.action,
            "reason": self.reason,
            "replay_policy_status": self.replay_policy_status,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeRetentionRunResult:
    """Top-level retention run summary with dry-run-safe counters."""

    dry_run: bool
    executed_at: str
    retention_classes: tuple[RetentionClass, ...]
    operational_log_results: tuple[OperationalLogCleanupResult, ...]
    evidence_decisions: tuple[EvidenceRetentionDecision, ...]
    artifact_object_retention: ArtifactObjectRetentionResult
    evidence_compacted_count: int = 0
    evidence_compacted_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        operational_deleted_total = sum(item.deleted_count for item in self.operational_log_results)
        operational_candidate_total = sum(item.candidate_count for item in self.operational_log_results)
        compaction_eligible = [
            item.to_dict()
            for item in self.evidence_decisions
            if item.action == "eligible_for_compaction"
        ]
        preserved = [
            item.to_dict()
            for item in self.evidence_decisions
            if item.action != "eligible_for_compaction"
        ]
        return {
            "dry_run": self.dry_run,
            "executed_at": self.executed_at,
            "retention_classes": list(self.retention_classes),
            "operational_logs": {
                "rule_count": len(self.operational_log_results),
                "candidate_total": int(operational_candidate_total),
                "deleted_total": int(operational_deleted_total),
                "rules": [item.to_dict() for item in self.operational_log_results],
            },
            "evidence_compaction": {
                "decision_count": len(self.evidence_decisions),
                "eligible_count": len(compaction_eligible),
                "preserved_count": len(preserved),
                "compacted_count": int(self.evidence_compacted_count),
                "compacted_bytes": int(self.evidence_compacted_bytes),
                "eligible": compaction_eligible,
                "preserved": preserved,
            },
            "artifact_object_retention": self.artifact_object_retention.to_dict(),
        }


class KnowledgeRetentionService:
    """Evaluate and execute explicit retention policy decisions."""

    def __init__(
        self,
        db: Session,
        *,
        operational_retention_days: int = REASONING_RETENTION_DAYS,
        tenant_id: int | None = None,
        operational_batch_limit: int | None = None,
        manage_transaction: bool = True,
        include_operational_log_retention: bool = True,
        include_durable_evidence_retention: bool = True,
        include_artifact_object_retention: bool = True,
        archive_service: KnowledgeArchiveService | None = None,
        data_plane_retention_service: DataPlaneRetentionService | None = None,
    ) -> None:
        self.db = db
        self.operational_retention_days = max(1, int(operational_retention_days))
        self.tenant_id = _normalize_optional_positive_int(tenant_id, field_name="tenant_id")
        self.operational_batch_limit = _normalize_optional_positive_int(
            operational_batch_limit,
            field_name="operational_batch_limit",
        )
        self.manage_transaction = bool(manage_transaction)
        self.include_operational_log_retention = bool(include_operational_log_retention)
        self.include_durable_evidence_retention = bool(include_durable_evidence_retention)
        self.include_artifact_object_retention = bool(include_artifact_object_retention)
        self.archive_service = archive_service or KnowledgeArchiveService(db)
        self.data_plane_retention_service = data_plane_retention_service or DataPlaneRetentionService(db)

    def run(self, *, dry_run: bool = True) -> KnowledgeRetentionRunResult:
        """Run retention policy and return a deterministic summary."""
        started = time.perf_counter()
        now_utc = utc_now()
        rules = (
            self._operational_log_rules()
            if self.include_operational_log_retention
            else ()
        )
        operational_results_list: list[OperationalLogCleanupResult] = []
        remaining_operational_limit = self.operational_batch_limit
        for rule in rules:
            rule_result = self._execute_operational_rule(
                rule=rule,
                now_utc=now_utc,
                dry_run=dry_run,
                limit=remaining_operational_limit,
            )
            operational_results_list.append(rule_result)
            if remaining_operational_limit is not None:
                remaining_operational_limit = max(
                    0,
                    remaining_operational_limit - rule_result.candidate_count,
                )
        operational_results = tuple(operational_results_list)
        evidence_decisions = (
            tuple(self._evaluate_evidence_retention())
            if self.include_durable_evidence_retention
            else ()
        )
        compacted_count = 0
        compacted_bytes = 0
        if self.include_durable_evidence_retention and self.include_artifact_object_retention:
            retention_scope_pairs = self._retention_scope_pairs_from_archived_artifacts()
            artifact_object_retention = self.data_plane_retention_service.run_artifact_object_retention(
                tenant_task_scopes=retention_scope_pairs,
                archived_artifact_ids=self._archived_artifact_ids(),
                protected_artifact_ids=self._protected_artifact_ids(evidence_decisions),
                dry_run=dry_run,
            )
        else:
            artifact_object_retention = _empty_artifact_object_retention_result(
                dry_run=dry_run,
                executed_at=now_utc,
            )
        if not dry_run and self.include_durable_evidence_retention:
            compacted_count, compacted_bytes = self._apply_evidence_compaction(decisions=evidence_decisions)

        operational_deleted_total = sum(item.deleted_count for item in operational_results)
        retention_deleted_total = (
            int(operational_deleted_total)
            + int(compacted_count)
            + int(artifact_object_retention.deleted_count)
        )
        deleted_bytes_total = int(compacted_bytes) + int(artifact_object_retention.deleted_bytes)
        if not dry_run:
            safe_inc("knowledge_retention_deleted_total", max(0, retention_deleted_total))
            safe_gauge("knowledge_retention_deleted_bytes", max(0, deleted_bytes_total))
            safe_inc(
                "knowledge_retention_artifact_object_deleted_total",
                max(0, int(artifact_object_retention.deleted_count)),
            )
        else:
            safe_gauge("knowledge_retention_deleted_bytes", 0)
        safe_gauge(
            "knowledge_retention_duration_seconds",
            max(0.0, time.perf_counter() - started),
        )

        if self.manage_transaction:
            if dry_run:
                self.db.rollback()
            else:
                self.db.commit()

        return KnowledgeRetentionRunResult(
            dry_run=bool(dry_run),
            executed_at=now_utc.isoformat(),
            retention_classes=_retention_classes(
                include_durable_evidence_retention=self.include_durable_evidence_retention,
                include_operational_log_retention=self.include_operational_log_retention,
            ),
            operational_log_results=operational_results,
            evidence_decisions=evidence_decisions,
            artifact_object_retention=artifact_object_retention,
            evidence_compacted_count=compacted_count,
            evidence_compacted_bytes=compacted_bytes,
        )

    def _operational_log_rules(self) -> tuple[OperationalLogRetentionRule, ...]:
        days = self.operational_retention_days
        return (
            OperationalLogRetentionRule(
                name="agent_logs",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                max_age_days=days,
                model=AgentLog,
                timestamp_field="timestamp",
            ),
            OperationalLogRetentionRule(
                name="system_logs",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                max_age_days=days,
                model=SystemLog,
                timestamp_field="timestamp",
            ),
            OperationalLogRetentionRule(
                name="stream_events",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                max_age_days=days,
                model=StreamEvent,
                timestamp_field="created_at",
            ),
        )

    def _execute_operational_rule(
        self,
        *,
        rule: OperationalLogRetentionRule,
        now_utc: datetime,
        dry_run: bool,
        limit: int | None,
    ) -> OperationalLogCleanupResult:
        cutoff = now_utc - timedelta(days=int(rule.max_age_days))
        timestamp_column = getattr(rule.model, rule.timestamp_field)
        scoped_query = self.db.query(rule.model.id).filter(timestamp_column < cutoff)
        if self.tenant_id is not None:
            scoped_query = scoped_query.filter(rule.model.tenant_id == self.tenant_id)
        scoped_query = scoped_query.order_by(timestamp_column.asc(), rule.model.id.asc())
        if limit is not None:
            scoped_query = scoped_query.limit(max(0, int(limit)))
        candidate_ids = [row_id for (row_id,) in scoped_query.all()]
        candidates = len(candidate_ids)
        deleted = 0
        if not dry_run and candidate_ids:
            delete_query = self.db.query(rule.model).filter(rule.model.id.in_(tuple(candidate_ids)))
            if self.tenant_id is not None:
                delete_query = delete_query.filter(rule.model.tenant_id == self.tenant_id)
            deleted = int(delete_query.delete(synchronize_session=False) or 0)
            self.db.flush()
        return OperationalLogCleanupResult(
            name=rule.name,
            retention_class=rule.retention_class,
            max_age_days=int(rule.max_age_days),
            cutoff_iso=cutoff.isoformat(),
            candidate_count=candidates,
            deleted_count=deleted,
        )

    def _evaluate_evidence_retention(self) -> list[EvidenceRetentionDecision]:
        active_finding_refs = self._active_finding_evidence_ids()
        replay_protected_execution_ids = self._replay_protected_execution_ids()
        query = self.db.query(KnowledgeEvidenceArchive)
        if self.tenant_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.tenant_id == self.tenant_id)
        rows = query.all()
        decisions: list[EvidenceRetentionDecision] = []
        for row in rows:
            evidence_id = str(row.id)
            source_execution_id = str(row.source_execution_id)
            storage_mode = self.archive_service.normalize_storage_mode(str(row.storage_mode or ""))
            metadata = dict(row.archive_metadata or {})

            if evidence_id in active_finding_refs:
                decisions.append(
                    EvidenceRetentionDecision(
                        evidence_id=evidence_id,
                        source_execution_id=source_execution_id,
                        storage_mode=storage_mode,
                        retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                        action="preserve_active_finding",
                        reason="referenced_by_active_finding",
                        replay_policy_status="required_for_active_finding",
                    )
                )
                continue

            if bool(metadata.get("delete_survival_required")):
                decisions.append(
                    EvidenceRetentionDecision(
                        evidence_id=evidence_id,
                        source_execution_id=source_execution_id,
                        storage_mode=storage_mode,
                        retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                        action="preserve_replay_policy",
                        reason="delete_survival_required",
                        replay_policy_status="protected_requires_archived_file",
                    )
                )
                continue

            if source_execution_id in replay_protected_execution_ids:
                decisions.append(
                    EvidenceRetentionDecision(
                        evidence_id=evidence_id,
                        source_execution_id=source_execution_id,
                        storage_mode=storage_mode,
                        retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                        action="preserve_replay_policy",
                        reason="replay_policy_protected_execution",
                        replay_policy_status="protected_requires_archived_file",
                    )
                )
                continue

            if storage_mode != "archived_file":
                decisions.append(
                    EvidenceRetentionDecision(
                        evidence_id=evidence_id,
                        source_execution_id=source_execution_id,
                        storage_mode=storage_mode or "metadata_only",
                        retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                        action="preserve_non_archived_mode",
                        reason="storage_mode_not_archived_file",
                        replay_policy_status="not_applicable",
                    )
                )
                continue

            decisions.append(
                EvidenceRetentionDecision(
                    evidence_id=evidence_id,
                    source_execution_id=source_execution_id,
                    storage_mode=storage_mode,
                    retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                    action="eligible_for_compaction",
                    reason="cold_archived_file_without_active_or_replay_dependency",
                    replay_policy_status="not_required",
                )
            )
        return decisions

    def _apply_evidence_compaction(
        self,
        *,
        decisions: tuple[EvidenceRetentionDecision, ...],
    ) -> tuple[int, int]:
        compacted_count = 0
        compacted_bytes = 0
        if not decisions:
            return compacted_count, compacted_bytes

        rows_by_id = {
            str(row.id): row
            for row in self._tenant_scoped_query(KnowledgeEvidenceArchive).all()
        }
        for decision in decisions:
            if decision.action != "eligible_for_compaction":
                continue
            row = rows_by_id.get(str(decision.evidence_id))
            if row is None:
                continue
            did_compact, deleted_bytes = self.archive_service.compact_archive_to_metadata_only(
                evidence_row=row,
                reason=decision.reason,
                replay_policy_status=decision.replay_policy_status,
            )
            if did_compact:
                compacted_count += 1
                compacted_bytes += int(deleted_bytes)
        return compacted_count, compacted_bytes

    def _active_finding_evidence_ids(self) -> set[str]:
        # Candidate findings are still active triage state and must keep evidence available.
        open_statuses = {"open", "confirmed", "exploited", "triaged", "in_progress", "candidate"}
        rows = (
            self._tenant_scoped_query(KnowledgeFinding)
            .filter(KnowledgeFinding.status.in_(tuple(open_statuses)))
            .all()
        )
        collected: set[str] = set()
        for row in rows:
            collected.update(self._extract_evidence_archive_ids(row.evidence_summary))
            metadata = dict(row.finding_metadata or {})
            collected.update(self._extract_evidence_archive_ids(metadata))
            state = metadata.get("state")
            if isinstance(state, dict):
                collected.update(self._extract_evidence_archive_ids(state))
        return collected

    def _replay_protected_execution_ids(self) -> set[str]:
        runs = self._tenant_scoped_query(KnowledgeIngestionRun).all()
        protected: set[str] = set()
        for run in runs:
            family = str(run.extractor_family or "").strip().lower()
            metadata = dict(run.run_metadata or {})
            extraction_mode = str(metadata.get("candidate_extraction_mode") or "").strip().lower()
            replay_source_type = str(metadata.get("replay_source_type") or "").strip().lower()
            if family.startswith("llm."):
                protected.add(str(run.source_execution_id))
                continue
            if extraction_mode == "candidate_replay" or replay_source_type in {"runtime", "durable_archive"}:
                protected.add(str(run.source_execution_id))
        return protected

    @staticmethod
    def _extract_evidence_archive_ids(payload: Any) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        refs = payload.get("evidence_refs")
        if not isinstance(refs, list):
            return set()
        ids: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            evidence_id = str(ref.get("evidence_archive_id") or "").strip()
            if evidence_id:
                ids.add(evidence_id)
        return ids

    def _archived_artifact_ids(self) -> set[str]:
        query = self.db.query(KnowledgeEvidenceArchive.source_artifact_id)
        if self.tenant_id is not None:
            query = query.filter(KnowledgeEvidenceArchive.tenant_id == self.tenant_id)
        rows = query.all()
        return {
            str(source_artifact_id)
            for (source_artifact_id,) in rows
            if source_artifact_id is not None
        }

    def _protected_artifact_ids(
        self,
        decisions: tuple[EvidenceRetentionDecision, ...],
    ) -> set[str]:
        protected_evidence_ids = {
            item.evidence_id
            for item in decisions
            if item.action in {"preserve_active_finding", "preserve_replay_policy"}
        }
        if not protected_evidence_ids:
            return set()
        rows = (
            self.db.query(KnowledgeEvidenceArchive.id, KnowledgeEvidenceArchive.source_artifact_id)
            .filter(KnowledgeEvidenceArchive.id.in_(tuple(protected_evidence_ids)))
        )
        if self.tenant_id is not None:
            rows = rows.filter(KnowledgeEvidenceArchive.tenant_id == self.tenant_id)
        rows = rows.all()
        protected: set[str] = set()
        for _evidence_id, source_artifact_id in rows:
            if source_artifact_id is None:
                continue
            protected.add(str(source_artifact_id))
        return protected

    def _retention_scope_pairs_from_archived_artifacts(self) -> set[tuple[int, int]]:
        rows = (
            self.db.query(
                KnowledgeEvidenceArchive.tenant_id,
                KnowledgeEvidenceArchive.task_id,
            )
            .filter(
                KnowledgeEvidenceArchive.source_artifact_id.is_not(None),
                KnowledgeEvidenceArchive.tenant_id.is_not(None),
                KnowledgeEvidenceArchive.task_id.is_not(None),
            )
        )
        if self.tenant_id is not None:
            rows = rows.filter(KnowledgeEvidenceArchive.tenant_id == self.tenant_id)
        rows = rows.all()
        scopes: set[tuple[int, int]] = set()
        for tenant_id, task_id in rows:
            if tenant_id is None or task_id is None:
                continue
            try:
                parsed_tenant_id = int(tenant_id)
                parsed_task_id = int(task_id)
            except (TypeError, ValueError):
                continue
            if parsed_tenant_id <= 0 or parsed_task_id <= 0:
                continue
            scopes.add((parsed_tenant_id, parsed_task_id))
        return scopes

    def _tenant_scoped_query(self, model: Any) -> Any:
        query = self.db.query(model)
        if self.tenant_id is not None:
            query = query.filter(model.tenant_id == self.tenant_id)
        return query


def _normalize_optional_positive_int(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    normalized = int(value)
    if normalized < 1:
        raise ValueError(f"{field_name} must be positive")
    return normalized


def _empty_artifact_object_retention_result(
    *,
    dry_run: bool,
    executed_at: datetime,
) -> ArtifactObjectRetentionResult:
    return ArtifactObjectRetentionResult(
        dry_run=bool(dry_run),
        executed_at=executed_at.isoformat(),
        retention_class="artifact_payload",
        decisions=(),
        deleted_count=0,
        already_deleted_count=0,
        failed_count=0,
        deleted_bytes=0,
    )


def _retention_classes(
    *,
    include_durable_evidence_retention: bool,
    include_operational_log_retention: bool = True,
) -> tuple[RetentionClass, ...]:
    classes: list[RetentionClass] = []
    if include_operational_log_retention:
        classes.append(RETENTION_CLASS_OPERATIONAL_EPHEMERAL)
    if include_durable_evidence_retention:
        classes.append(RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE)
    return tuple(classes)
