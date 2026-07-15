"""Tests for report deletion, undo, and retention services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as backend_models
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.data_management import TenantDataManagementSettings
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.tenant import Tenant
from backend.services.reporting.report_deletion_service import ReportDeletionService
from backend.services.reporting.report_retention_service import (
    CURRENT_READY_REPORT_PROTECTED,
    CURRENT_READY_TASK_MEMO_PROTECTED,
    HISTORICAL_REPORT_RETENTION_EXPIRED,
    PENDING_REPORT_DELETION_FINALIZATION_EXPIRED,
    REPORT_JOB_RETENTION_EXPIRED,
    ReportRetentionExecutor,
    ReportRetentionService,
    TASK_MEMO_HISTORY_RETENTION_EXPIRED,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_REPORTING,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)


@dataclass(frozen=True, slots=True)
class _ReportPolicy:
    report_history_retention_days: int = 30
    report_retention_enabled: bool = True
    report_job_retention_days: int = 30
    task_memo_history_retention_days: int = 30
    retention_batch_size_per_tenant: int = 10


def _build_session_factory() -> sessionmaker[Session]:
    assert backend_models.__all__
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _seed_scope(db: Session) -> tuple[int, int, int]:
    tenant = Tenant(slug=f"tenant-{uuid4().hex[:8]}", name="Tenant")
    user = User(username=f"user-{uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Engagement",
    )
    db.add(engagement)
    db.flush()
    return int(tenant.id), int(user.id), int(engagement.id)


def _task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    name: str = "Task",
) -> Task:
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        name=f"{name}-{uuid4().hex[:8]}",
        status="stopped",
    )
    db.add(task)
    db.flush()
    return task


def _report(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    version: int,
    is_current: bool,
    generated_at: datetime,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        version=version,
        status="ready",
        is_current=is_current,
        title=f"Report {version}",
        sections=[{"section_id": "summary"}],
        markdown_snapshot=f"# Report {version}",
        source_task_memo_ids=[str(uuid4())],
        source_knowledge_refs=[{"ref": "knowledge_finding:1", "task_id": 1, "record_type": "finding", "authoritative": True}],
        source_evidence_refs=[{"ref": "evidence_archive:1", "task_id": 1, "evidence_type": "tool_output", "source_tool": "nmap"}],
        generation_metadata={"hash": str(version)},
        generated_at=generated_at,
    )
    db.add(report)
    db.flush()
    return report


def _report_job(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    status: str,
    created_at: datetime,
    finished_at: datetime | None = None,
) -> EngagementReportJob:
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        status=status,
        idempotency_key=f"job-{uuid4()}",
        selected_task_memo_ids=[],
        include_candidate_findings=False,
        source_watermark={"hash": uuid4().hex},
        completed_sections=[],
        total_sections=1,
        created_at=created_at,
        updated_at=finished_at or created_at,
        started_at=created_at,
        finished_at=finished_at,
    )
    db.add(job)
    db.flush()
    return job


def _memo(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    version: int,
    is_current: bool,
    generated_at: datetime,
    status: str = "ready",
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=version,
        is_current=is_current,
        status=status,
        memo_mode="supported",
        source_watermark={"hash": uuid4().hex},
        memo={"summary": f"Memo {version}"},
        created_at=generated_at,
        updated_at=generated_at,
        generated_at=generated_at,
    )
    db.add(memo)
    db.flush()
    return memo


def test_deleting_current_report_promotes_newest_remaining_report() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        old_report = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        current_report = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=2,
            is_current=True,
            generated_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db.commit()

        response = ReportDeletionService(db, undo_seconds=60).schedule_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=user_id,
            report_id=current_report.id,
        )

        assert response is not None
        assert response.deleted_current is True
        assert response.current_report_id == old_report.id
        assert db.get(EngagementReport, old_report.id).is_current is True
        assert db.get(EngagementReport, current_report.id).delete_scheduled_at is not None


def test_undo_delete_restores_current_only_when_no_replacement_exists() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        report = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=True,
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.commit()

        ReportDeletionService(db, undo_seconds=60).schedule_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=user_id,
            report_id=report.id,
        )
        response = ReportDeletionService(db, undo_seconds=60).undo_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report.id,
        )

        assert response is not None
        assert response.restored_current is True
        restored = db.get(EngagementReport, report.id)
        assert restored.is_current is True
        assert restored.delete_scheduled_at is None


def test_retention_erases_only_historical_reports_and_preserves_current() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        db.add(
            TenantDataManagementSettings(
                tenant_id=tenant_id,
                report_retention_enabled=True,
                report_history_retention_days=180,
            )
        )
        old_historical = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=220),
        )
        current_old = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=2,
            is_current=True,
            generated_at=datetime.now(UTC) - timedelta(days=220),
        )
        db.commit()

        result = ReportRetentionService(db).run(dry_run=False)

        assert result.retention_finalized_count == 1
        erased = db.get(EngagementReport, old_historical.id)
        preserved = db.get(EngagementReport, current_old.id)
        assert erased.deletion_finalized_at is not None
        assert erased.sections == []
        assert erased.markdown_snapshot is None
        assert erased.source_task_memo_ids == []
        assert erased.source_knowledge_refs == []
        assert erased.source_evidence_refs == []
        assert preserved.deletion_finalized_at is None
        assert preserved.markdown_snapshot == "# Report 2"


def test_default_retention_erases_historical_report_content() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        old_historical = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=220),
        )
        db.commit()

        result = ReportRetentionService(db).run(dry_run=False, tenant_id=tenant_id)

        assert result.retention_finalized_count == 1
        erased = db.get(EngagementReport, old_historical.id)
        assert erased.deletion_finalized_at is not None
        assert erased.markdown_snapshot is None
        assert erased.sections == []
        assert erased.source_task_memo_ids == []


def test_retention_respects_disabled_report_content_flag() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        db.add(
            TenantDataManagementSettings(
                tenant_id=tenant_id,
                report_retention_enabled=False,
                report_history_retention_days=180,
            )
        )
        old_historical = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=220),
        )
        db.commit()

        result = ReportRetentionService(db).run(dry_run=False, tenant_id=tenant_id)

        assert result.retention_finalized_count == 0
        assert db.get(EngagementReport, old_historical.id).deletion_finalized_at is None
        assert db.get(EngagementReport, old_historical.id).markdown_snapshot == "# Report 1"


def test_retention_finalizes_expired_manual_deletion_and_blocks_undo() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        report = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=True,
            generated_at=datetime.now(UTC) - timedelta(days=1),
        )
        db.commit()

        ReportDeletionService(db, undo_seconds=1).schedule_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=user_id,
            report_id=report.id,
        )
        report.delete_undo_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        result = ReportRetentionService(db).run(dry_run=False, tenant_id=tenant_id)

        assert result.pending_finalized_count == 1
        erased = db.get(EngagementReport, report.id)
        assert erased.deletion_finalized_at is not None
        assert erased.sections == []
        assert erased.markdown_snapshot is None
        assert (
            ReportDeletionService(db).undo_delete(
                tenant_id=tenant_id,
                user_id=user_id,
                report_id=report.id,
            )
            is None
        )


def test_report_retention_executor_returns_canonical_reporting_result_and_uses_policy() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        db.add(
            TenantDataManagementSettings(
                tenant_id=tenant_id,
                report_retention_enabled=True,
                report_history_retention_days=180,
            )
        )
        historical_policy_candidate = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=45),
        )
        current_policy_protected = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=2,
            is_current=True,
            generated_at=datetime.now(UTC) - timedelta(days=45),
        )
        recent_historical = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=3,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=3),
        )
        db.commit()

        executor = ReportRetentionExecutor(db)
        dry_run = executor.run(
            policy=_ReportPolicy(report_history_retention_days=30),
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=50,
        )
        applied = executor.run(
            policy=_ReportPolicy(report_history_retention_days=30),
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert dry_run.retention_class == RETENTION_CLASS_REPORTING
        assert dry_run.counts.batch_limit == 10
        assert dry_run.counts.scanned_count == 2
        assert dry_run.counts.candidate_count == 1
        assert dry_run.counts.protected_count == 1
        assert dry_run.counts.applied_count == 0
        assert dry_run.reason_counts == {
            HISTORICAL_REPORT_RETENTION_EXPIRED: 1,
            CURRENT_READY_REPORT_PROTECTED: 1,
        }
        dry_run_decisions = {
            decision.reason_code: decision for decision in dry_run.decisions
        }
        assert (
            dry_run_decisions[HISTORICAL_REPORT_RETENTION_EXPIRED].outcome
            == RETENTION_DECISION_CANDIDATE
        )
        assert (
            dry_run_decisions[CURRENT_READY_REPORT_PROTECTED].outcome
            == RETENTION_DECISION_PROTECTED
        )
        assert dry_run_decisions[CURRENT_READY_REPORT_PROTECTED].resource_id is None
        assert applied.retention_class == RETENTION_CLASS_REPORTING
        assert applied.counts.scanned_count == 2
        assert applied.counts.candidate_count == 1
        assert applied.counts.protected_count == 1
        assert applied.counts.applied_count == 1
        assert applied.reason_counts[HISTORICAL_REPORT_RETENTION_EXPIRED] == 1
        assert applied.reason_counts[CURRENT_READY_REPORT_PROTECTED] == 1
        applied_decisions = {
            decision.reason_code: decision for decision in applied.decisions
        }
        assert (
            applied_decisions[HISTORICAL_REPORT_RETENTION_EXPIRED].outcome
            == RETENTION_DECISION_APPLIED
        )
        assert (
            applied_decisions[CURRENT_READY_REPORT_PROTECTED].outcome
            == RETENTION_DECISION_PROTECTED
        )
        assert applied_decisions[CURRENT_READY_REPORT_PROTECTED].resource_id is None
        assert applied.to_safe_dict()["retention_class"] == RETENTION_CLASS_REPORTING
        assert (
            db.get(EngagementReport, historical_policy_candidate.id).deletion_finalized_at
            is not None
        )
        assert (
            db.get(EngagementReport, current_policy_protected.id).deletion_finalized_at
            is None
        )
        assert db.get(EngagementReport, current_policy_protected.id).is_current is True
        assert db.get(EngagementReport, recent_historical.id).deletion_finalized_at is None


def test_report_retention_executor_finalizes_only_target_tenant_candidates() -> None:
    factory = _build_session_factory()
    with factory() as db:
        target_tenant_id, target_user_id, target_engagement_id = _seed_scope(db)
        foreign_tenant_id, foreign_user_id, foreign_engagement_id = _seed_scope(db)
        generated_at = datetime.now(UTC) - timedelta(days=45)
        target_historical = _report(
            db,
            tenant_id=target_tenant_id,
            user_id=target_user_id,
            engagement_id=target_engagement_id,
            version=1,
            is_current=False,
            generated_at=generated_at,
        )
        target_current = _report(
            db,
            tenant_id=target_tenant_id,
            user_id=target_user_id,
            engagement_id=target_engagement_id,
            version=2,
            is_current=True,
            generated_at=generated_at,
        )
        foreign_historical = _report(
            db,
            tenant_id=foreign_tenant_id,
            user_id=foreign_user_id,
            engagement_id=foreign_engagement_id,
            version=1,
            is_current=False,
            generated_at=generated_at,
        )
        db.commit()

        result = ReportRetentionExecutor(db).run(
            policy=_ReportPolicy(report_history_retention_days=30),
            tenant_id=target_tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert result.counts.candidate_count == 1
        assert result.counts.protected_count == 1
        assert result.counts.applied_count == 1
        assert result.reason_counts == {
            HISTORICAL_REPORT_RETENTION_EXPIRED: 1,
            CURRENT_READY_REPORT_PROTECTED: 1,
        }
        assert (
            db.get(EngagementReport, target_historical.id).deletion_finalized_at
            is not None
        )
        assert db.get(EngagementReport, target_current.id).deletion_finalized_at is None
        assert db.get(EngagementReport, target_current.id).markdown_snapshot == "# Report 2"
        assert db.get(EngagementReport, foreign_historical.id).deletion_finalized_at is None
        assert (
            db.get(EngagementReport, foreign_historical.id).markdown_snapshot
            == "# Report 1"
        )


def test_report_retention_executor_keeps_pending_deletion_finalization_compatible() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        report = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=True,
            generated_at=datetime.now(UTC) - timedelta(days=1),
        )
        db.commit()

        ReportDeletionService(db, undo_seconds=1).schedule_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=user_id,
            report_id=report.id,
        )
        report.delete_undo_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        result = ReportRetentionExecutor(db).run(
            policy=_ReportPolicy(report_history_retention_days=30),
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert result.counts.candidate_count == 1
        assert result.counts.applied_count == 1
        assert result.reason_counts == {PENDING_REPORT_DELETION_FINALIZATION_EXPIRED: 1}
        assert result.decisions[0].outcome == RETENTION_DECISION_APPLIED
        assert db.get(EngagementReport, report.id).deletion_finalized_at is not None


def test_report_retention_executor_cleans_terminal_jobs_and_memo_history() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        task = _task(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
        )
        old_at = datetime.now(UTC) - timedelta(days=45)
        recent_at = datetime.now(UTC) - timedelta(days=3)
        completed_job = _report_job(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="ready",
            created_at=old_at,
            finished_at=old_at,
        )
        failed_job = _report_job(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="failed",
            created_at=old_at,
            finished_at=old_at,
        )
        active_job = _report_job(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="generating",
            created_at=old_at,
            finished_at=None,
        )
        recent_completed_job = _report_job(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            status="ready",
            created_at=recent_at,
            finished_at=recent_at,
        )
        old_history_memo = _memo(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task.id,
            version=1,
            is_current=False,
            generated_at=old_at,
        )
        current_ready_memo = _memo(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task.id,
            version=2,
            is_current=True,
            generated_at=old_at,
        )
        recent_history_memo = _memo(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task.id,
            version=3,
            is_current=False,
            generated_at=recent_at,
        )
        db.commit()

        executor = ReportRetentionExecutor(db)
        dry_run = executor.run(
            policy=_ReportPolicy(
                report_history_retention_days=30,
                report_job_retention_days=30,
                task_memo_history_retention_days=30,
                retention_batch_size_per_tenant=10,
            ),
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=50,
        )
        assert db.get(EngagementReportJob, completed_job.id) is not None
        assert db.get(TaskClosureMemo, old_history_memo.id) is not None

        applied = executor.run(
            policy=_ReportPolicy(
                report_history_retention_days=30,
                report_job_retention_days=30,
                task_memo_history_retention_days=30,
                retention_batch_size_per_tenant=10,
            ),
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert dry_run.counts.candidate_count == 3
        assert dry_run.counts.protected_count == 1
        assert dry_run.counts.applied_count == 0
        assert dry_run.reason_counts == {
            REPORT_JOB_RETENTION_EXPIRED: 2,
            TASK_MEMO_HISTORY_RETENTION_EXPIRED: 1,
            CURRENT_READY_TASK_MEMO_PROTECTED: 1,
        }
        dry_run_decisions = {
            decision.reason_code: decision for decision in dry_run.decisions
        }
        assert (
            dry_run_decisions[REPORT_JOB_RETENTION_EXPIRED].outcome
            == RETENTION_DECISION_CANDIDATE
        )
        assert (
            dry_run_decisions[TASK_MEMO_HISTORY_RETENTION_EXPIRED].outcome
            == RETENTION_DECISION_CANDIDATE
        )
        assert (
            dry_run_decisions[CURRENT_READY_TASK_MEMO_PROTECTED].outcome
            == RETENTION_DECISION_PROTECTED
        )

        assert applied.counts.candidate_count == 3
        assert applied.counts.protected_count == 1
        assert applied.counts.applied_count == 3
        assert applied.reason_counts == {
            REPORT_JOB_RETENTION_EXPIRED: 2,
            TASK_MEMO_HISTORY_RETENTION_EXPIRED: 1,
            CURRENT_READY_TASK_MEMO_PROTECTED: 1,
        }
        assert db.get(EngagementReportJob, completed_job.id) is None
        assert db.get(EngagementReportJob, failed_job.id) is None
        assert db.get(EngagementReportJob, active_job.id) is not None
        assert db.get(EngagementReportJob, recent_completed_job.id) is not None
        assert db.get(TaskClosureMemo, old_history_memo.id) is None
        assert db.get(TaskClosureMemo, current_ready_memo.id) is not None
        assert db.get(TaskClosureMemo, recent_history_memo.id) is not None


def test_report_retention_executor_keeps_job_and_memo_cleanup_tenant_scoped() -> None:
    factory = _build_session_factory()
    with factory() as db:
        target_tenant_id, target_user_id, target_engagement_id = _seed_scope(db)
        foreign_tenant_id, foreign_user_id, foreign_engagement_id = _seed_scope(db)
        target_task = _task(
            db,
            tenant_id=target_tenant_id,
            user_id=target_user_id,
            engagement_id=target_engagement_id,
        )
        foreign_task = _task(
            db,
            tenant_id=foreign_tenant_id,
            user_id=foreign_user_id,
            engagement_id=foreign_engagement_id,
        )
        old_at = datetime.now(UTC) - timedelta(days=45)
        target_job = _report_job(
            db,
            tenant_id=target_tenant_id,
            user_id=target_user_id,
            engagement_id=target_engagement_id,
            status="ready",
            created_at=old_at,
            finished_at=old_at,
        )
        foreign_job = _report_job(
            db,
            tenant_id=foreign_tenant_id,
            user_id=foreign_user_id,
            engagement_id=foreign_engagement_id,
            status="ready",
            created_at=old_at,
            finished_at=old_at,
        )
        target_memo = _memo(
            db,
            tenant_id=target_tenant_id,
            user_id=target_user_id,
            engagement_id=target_engagement_id,
            task_id=target_task.id,
            version=1,
            is_current=False,
            generated_at=old_at,
        )
        foreign_memo = _memo(
            db,
            tenant_id=foreign_tenant_id,
            user_id=foreign_user_id,
            engagement_id=foreign_engagement_id,
            task_id=foreign_task.id,
            version=1,
            is_current=False,
            generated_at=old_at,
        )
        db.commit()

        result = ReportRetentionExecutor(db).run(
            policy=_ReportPolicy(
                report_job_retention_days=30,
                task_memo_history_retention_days=30,
            ),
            tenant_id=target_tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert result.counts.candidate_count == 2
        assert result.counts.applied_count == 2
        assert result.reason_counts == {
            REPORT_JOB_RETENTION_EXPIRED: 1,
            TASK_MEMO_HISTORY_RETENTION_EXPIRED: 1,
        }
        assert db.get(EngagementReportJob, target_job.id) is None
        assert db.get(TaskClosureMemo, target_memo.id) is None
        assert db.get(EngagementReportJob, foreign_job.id) is not None
        assert db.get(TaskClosureMemo, foreign_memo.id) is not None


def test_report_retention_executor_bounds_historical_and_pending_finalization() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant_id, user_id, engagement_id = _seed_scope(db)
        historical = _report(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            version=1,
            is_current=False,
            generated_at=datetime.now(UTC) - timedelta(days=45),
        )
        pending_reports = [
            _report(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                version=version,
                is_current=False,
                generated_at=datetime.now(UTC) - timedelta(days=1),
            )
            for version in (2, 3, 4)
        ]
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        for report in pending_reports:
            report.delete_scheduled_at = expired_at
            report.delete_undo_until = expired_at
        db.commit()

        executor = ReportRetentionExecutor(db)
        policy = _ReportPolicy(
            report_history_retention_days=30,
            retention_batch_size_per_tenant=2,
        )
        first = executor.run(
            policy=policy,
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )
        second = executor.run(
            policy=policy,
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )
        third = executor.run(
            policy=policy,
            tenant_id=tenant_id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=50,
        )

        assert first.counts.batch_limit == 2
        assert first.counts.candidate_count == 2
        assert first.counts.applied_count == 2
        assert first.reason_counts == {
            HISTORICAL_REPORT_RETENTION_EXPIRED: 1,
            PENDING_REPORT_DELETION_FINALIZATION_EXPIRED: 1,
        }
        assert db.get(EngagementReport, historical.id).deletion_finalized_at is not None
        assert (
            sum(
                1
                for report in pending_reports
                if db.get(EngagementReport, report.id).deletion_finalized_at is not None
            )
            == 3
        )
        assert second.counts.candidate_count == 2
        assert second.counts.applied_count == 2
        assert second.reason_counts == {
            PENDING_REPORT_DELETION_FINALIZATION_EXPIRED: 2
        }
        assert third.counts.candidate_count == 0
        assert third.counts.applied_count == 0
        assert third.reason_counts == {}
