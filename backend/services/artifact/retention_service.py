"""Artifact-owned retention executors for payload and provenance records.

This module adapts tenant retention policy inputs to data-plane object cleanup
and to explicit protected provenance handling for the MVP retention contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.domain.task_lifecycle import TaskStatus
from backend.models.knowledge import (
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
)
from backend.models.core import Task
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.services.data_plane.retention_service import (
    ArtifactObjectRetentionDecision,
    ArtifactObjectRetentionResult,
    DataPlaneRetentionService,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_FAILED,
    RETENTION_DECISION_PROTECTED,
    RETENTION_DECISION_SKIPPED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    TenantId,
    validate_run_mode,
)


ARTIFACT_PAYLOAD_RETAINED_REASON = "durable_evidence_retained_runtime_artifact_payload_deleted"
ARTIFACT_PAYLOAD_DELETE_FAILED_REASON = "artifact_payload_object_delete_failed"
ARTIFACT_PROVENANCE_PROTECTED_REASON = "execution_provenance_preserved_mvp"


class SupportsArtifactRetentionPolicy(Protocol):
    """Policy fields consumed by the artifact payload retention executor."""

    artifact_payload_retention_days: int
    retention_batch_size_per_tenant: int


class SupportsArtifactProvenanceRetentionPolicy(Protocol):
    """Policy fields consumed by the artifact provenance retention executor."""

    artifact_metadata_retention_days_after_terminal: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class ArtifactRetentionExecutor:
    """Run bounded artifact payload retention through the shared contract."""

    db: Session
    data_plane_retention_service: DataPlaneRetentionService | None = None
    name: str = "artifact.retention"
    retention_class: str = RETENTION_CLASS_ARTIFACT_PAYLOAD

    def run(
        self,
        *,
        policy: SupportsArtifactRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped artifact payload objects."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        plan = _load_artifact_payload_plan(self.db, tenant_id=scoped_tenant_id)
        data_plane = self.data_plane_retention_service or DataPlaneRetentionService(self.db)
        result = data_plane.run_artifact_object_retention(
            tenant_task_scopes=plan.scope_pairs,
            archived_artifact_ids=plan.archived_artifact_ids,
            protected_artifact_ids=plan.protected_artifact_ids,
            created_before=_artifact_payload_cutoff(policy),
            limit_per_tenant=effective_limit,
            dry_run=run_mode == RETENTION_RUN_MODE_DRY_RUN,
        )

        succeeded = int(result.failed_count) == 0
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=_build_counts(result=result, batch_limit=effective_limit),
            reason_counts=_build_reason_counts(result),
            decisions=_build_decisions(result=result, mode=run_mode),
            succeeded=succeeded,
            error_code=(
                None if succeeded else ARTIFACT_PAYLOAD_DELETE_FAILED_REASON
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactProvenanceRetentionExecutor:
    """Report protected artifact provenance rows without mutating them in MVP."""

    db: Session
    name: str = "artifact_provenance.retention"
    retention_class: str = RETENTION_CLASS_EXECUTION_PROVENANCE

    def run(
        self,
        *,
        policy: SupportsArtifactProvenanceRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate terminal-task provenance rows as explicitly protected."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_provenance_limit(policy=policy, limit=limit)
        terminal_before = _artifact_metadata_cutoff(policy)
        decisions = _load_artifact_provenance_protected_decisions(
            self.db,
            tenant_id=scoped_tenant_id,
            terminal_before=terminal_before,
            limit=effective_limit,
        )

        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_EXECUTION_PROVENANCE,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=len(decisions),
                protected_count=len(decisions),
                preserved_count=len(decisions),
                batch_count=len(decisions),
                batch_limit=effective_limit,
            ),
            reason_counts=(
                {ARTIFACT_PROVENANCE_PROTECTED_REASON: len(decisions)}
                if decisions
                else {}
            ),
            decisions=tuple(decisions),
        )


@dataclass(frozen=True, slots=True)
class _ArtifactPayloadPlan:
    scope_pairs: set[tuple[int, int]]
    archived_artifact_ids: set[str]
    protected_artifact_ids: set[str]


def _load_artifact_payload_plan(
    db: Session,
    *,
    tenant_id: int,
) -> _ArtifactPayloadPlan:
    rows = (
        db.query(KnowledgeEvidenceArchive)
        .filter(
            KnowledgeEvidenceArchive.tenant_id == tenant_id,
            KnowledgeEvidenceArchive.source_artifact_id.is_not(None),
        )
        .all()
    )
    active_evidence_ids = _active_finding_evidence_ids(db, tenant_id=tenant_id)
    replay_protected_execution_ids = _replay_protected_execution_ids(db, tenant_id=tenant_id)

    scope_pairs: set[tuple[int, int]] = set()
    archived_artifact_ids: set[str] = set()
    protected_artifact_ids: set[str] = set()
    for row in rows:
        artifact_id = str(row.source_artifact_id)
        archived_artifact_ids.add(artifact_id)
        if row.task_id is not None:
            scope_pairs.add((int(row.tenant_id), int(row.task_id)))

        metadata = dict(row.archive_metadata or {})
        is_policy_protected = (
            bool(metadata.get("delete_survival_required"))
            or str(row.id) in active_evidence_ids
            or str(row.source_execution_id) in replay_protected_execution_ids
        )
        if is_policy_protected:
            protected_artifact_ids.add(artifact_id)

    return _ArtifactPayloadPlan(
        scope_pairs=scope_pairs,
        archived_artifact_ids=archived_artifact_ids,
        protected_artifact_ids=protected_artifact_ids,
    )


def _active_finding_evidence_ids(db: Session, *, tenant_id: int) -> set[str]:
    open_statuses = {
        "open",
        "confirmed",
        "exploited",
        "triaged",
        "in_progress",
        "candidate",
    }
    rows = (
        db.query(KnowledgeFinding)
        .filter(
            KnowledgeFinding.tenant_id == tenant_id,
            KnowledgeFinding.status.in_(tuple(open_statuses)),
        )
        .all()
    )
    collected: set[str] = set()
    for row in rows:
        collected.update(_extract_evidence_archive_ids(row.evidence_summary))
        metadata = dict(row.finding_metadata or {})
        collected.update(_extract_evidence_archive_ids(metadata))
        state = metadata.get("state")
        if isinstance(state, dict):
            collected.update(_extract_evidence_archive_ids(state))
    return collected


def _replay_protected_execution_ids(db: Session, *, tenant_id: int) -> set[str]:
    rows = (
        db.query(KnowledgeIngestionRun)
        .filter(KnowledgeIngestionRun.tenant_id == tenant_id)
        .all()
    )
    protected: set[str] = set()
    for row in rows:
        family = str(row.extractor_family or "").strip().lower()
        metadata = dict(row.run_metadata or {})
        extraction_mode = str(metadata.get("candidate_extraction_mode") or "").strip().lower()
        replay_source_type = str(metadata.get("replay_source_type") or "").strip().lower()
        if family.startswith("llm."):
            protected.add(str(row.source_execution_id))
            continue
        if extraction_mode == "candidate_replay" or replay_source_type in {
            "runtime",
            "durable_archive",
        }:
            protected.add(str(row.source_execution_id))
    return protected


def _extract_evidence_archive_ids(payload: object) -> set[str]:
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


def _build_counts(
    *,
    result: ArtifactObjectRetentionResult,
    batch_limit: int,
) -> RetentionBatchCounts:
    candidate_count = _candidate_count(result.decisions)
    protected_count = _protected_count(result.decisions)
    preserved_count = len(result.decisions) - candidate_count
    return RetentionBatchCounts(
        scanned_count=len(result.decisions),
        candidate_count=candidate_count,
        protected_count=protected_count,
        applied_count=int(result.deleted_count),
        skipped_count=int(result.already_deleted_count),
        failed_count=int(result.failed_count),
        preserved_count=preserved_count,
        already_deleted_count=int(result.already_deleted_count),
        batch_count=len(result.decisions),
        batch_limit=batch_limit,
    )


def _build_reason_counts(result: ArtifactObjectRetentionResult) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    for decision in result.decisions:
        reason = _normalize_decision_reason(decision.reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if result.failed_count:
        reason_counts[ARTIFACT_PAYLOAD_DELETE_FAILED_REASON] = int(result.failed_count)
    return reason_counts


def _build_decisions(
    *,
    result: ArtifactObjectRetentionResult,
    mode: RetentionRunMode,
) -> tuple[RetentionDecision, ...]:
    decisions: list[RetentionDecision] = []
    for decision in result.decisions:
        if decision.action == "eligible_for_delete":
            if mode == RETENTION_RUN_MODE_DRY_RUN:
                decisions.append(
                    _retention_decision(
                        source=decision,
                        outcome=RETENTION_DECISION_CANDIDATE,
                    )
                )
            continue

        outcome = (
            RETENTION_DECISION_PROTECTED
            if decision.reason == "durable_evidence_policy_protected"
            else RETENTION_DECISION_SKIPPED
        )
        decisions.append(_retention_decision(source=decision, outcome=outcome))

    if mode == RETENTION_RUN_MODE_APPLY:
        if result.deleted_count:
            decisions.append(
                _aggregate_decision(
                    outcome=RETENTION_DECISION_APPLIED,
                    reason_code=ARTIFACT_PAYLOAD_RETAINED_REASON,
                    count=result.deleted_count,
                )
            )
        if result.already_deleted_count:
            decisions.append(
                _aggregate_decision(
                    outcome=RETENTION_DECISION_SKIPPED,
                    reason_code=ARTIFACT_PAYLOAD_RETAINED_REASON,
                    count=result.already_deleted_count,
                )
            )
        if result.failed_count:
            decisions.append(
                _aggregate_decision(
                    outcome=RETENTION_DECISION_FAILED,
                    reason_code=ARTIFACT_PAYLOAD_DELETE_FAILED_REASON,
                    count=result.failed_count,
                )
            )
    return tuple(decisions)


def _retention_decision(
    *,
    source: ArtifactObjectRetentionDecision,
    outcome: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
        outcome=outcome,
        reason_code=_normalize_decision_reason(source.reason),
        resource_id=source.artifact_id,
    )


def _aggregate_decision(
    *,
    outcome: str,
    reason_code: str,
    count: int,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
        outcome=outcome,
        reason_code=reason_code,
        count=int(count),
    )


def _normalize_decision_reason(reason: str) -> str:
    if reason == "durable_evidence_retained":
        return ARTIFACT_PAYLOAD_RETAINED_REASON
    return reason


def _candidate_count(decisions: tuple[ArtifactObjectRetentionDecision, ...]) -> int:
    return sum(1 for decision in decisions if decision.action == "eligible_for_delete")


def _protected_count(decisions: tuple[ArtifactObjectRetentionDecision, ...]) -> int:
    return sum(
        1
        for decision in decisions
        if decision.reason == "durable_evidence_policy_protected"
    )


def _effective_limit(
    *,
    policy: SupportsArtifactRetentionPolicy,
    limit: int,
) -> int:
    _normalize_positive_int(
        policy.artifact_payload_retention_days,
        field_name="policy.artifact_payload_retention_days",
    )
    return min(
        _normalize_positive_int(limit, field_name="limit"),
        _normalize_positive_int(
            policy.retention_batch_size_per_tenant,
            field_name="policy.retention_batch_size_per_tenant",
        ),
    )


def _artifact_payload_cutoff(policy: SupportsArtifactRetentionPolicy):
    retention_days = _normalize_positive_int(
        policy.artifact_payload_retention_days,
        field_name="policy.artifact_payload_retention_days",
    )
    return utc_now() - timedelta(days=retention_days)


def _effective_provenance_limit(
    *,
    policy: SupportsArtifactProvenanceRetentionPolicy,
    limit: int,
) -> int:
    _normalize_positive_int(
        policy.artifact_metadata_retention_days_after_terminal,
        field_name="policy.artifact_metadata_retention_days_after_terminal",
    )
    return min(
        _normalize_positive_int(limit, field_name="limit"),
        _normalize_positive_int(
            policy.retention_batch_size_per_tenant,
            field_name="policy.retention_batch_size_per_tenant",
        ),
    )


def _artifact_metadata_cutoff(policy: SupportsArtifactProvenanceRetentionPolicy):
    retention_days = _normalize_positive_int(
        policy.artifact_metadata_retention_days_after_terminal,
        field_name="policy.artifact_metadata_retention_days_after_terminal",
    )
    return utc_now() - timedelta(days=retention_days)


def _load_artifact_provenance_protected_decisions(
    db: Session,
    *,
    tenant_id: int,
    terminal_before: object,
    limit: int,
) -> list[RetentionDecision]:
    terminal_at = func.coalesce(
        Task.completed_at,
        Task.stopped_at,
        Task.updated_at,
        Task.created_at,
    )
    executions = (
        db.query(ToolExecution)
        .join(Task, Task.id == ToolExecution.task_id)
        .filter(
            ToolExecution.tenant_id == tenant_id,
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
            terminal_at < terminal_before,
        )
        .order_by(ToolExecution.created_at.asc(), ToolExecution.id.asc())
        .limit(limit)
        .all()
    )
    decisions = [
        _provenance_decision(resource_id=f"tool_execution:{execution.id}")
        for execution in executions
    ]
    remaining_limit = limit - len(decisions)
    if remaining_limit <= 0:
        return decisions

    artifacts = (
        db.query(ExecutionArtifact)
        .join(Task, Task.id == ExecutionArtifact.task_id)
        .filter(
            ExecutionArtifact.tenant_id == tenant_id,
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.get_terminal_statuses())),
            terminal_at < terminal_before,
        )
        .order_by(ExecutionArtifact.created_at.asc(), ExecutionArtifact.id.asc())
        .limit(remaining_limit)
        .all()
    )
    decisions.extend(
        _provenance_decision(resource_id=f"execution_artifact:{artifact.id}")
        for artifact in artifacts
    )
    return decisions


def _provenance_decision(*, resource_id: str) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_EXECUTION_PROVENANCE,
        outcome=RETENTION_DECISION_PROTECTED,
        reason_code=ARTIFACT_PROVENANCE_PROTECTED_REASON,
        resource_id=resource_id,
    )


def _normalize_positive_int(value: object, *, field_name: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if normalized < 1:
        raise ValueError(f"{field_name} must be positive")
    return normalized


__all__ = [
    "ARTIFACT_PAYLOAD_DELETE_FAILED_REASON",
    "ARTIFACT_PAYLOAD_RETAINED_REASON",
    "ARTIFACT_PROVENANCE_PROTECTED_REASON",
    "ArtifactProvenanceRetentionExecutor",
    "ArtifactRetentionExecutor",
]
