"""Test canonical reporting-retention persistence and protection boundaries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.models.tenant import Tenant
from backend.repositories.reporting.reporting_retention_repository import (
    ReportingRetentionRepository,
)


REPORTING_REPOSITORY_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORTING_REPOSITORY_TABLES)
    return engine, sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def _seed_scope(session, *, tenant_label: str):
    tenant = Tenant(
        slug=f"tenant-{tenant_label}-{uuid.uuid4().hex}", name=f"Tenant {tenant_label}"
    )
    user = User(
        username=f"user-{tenant_label}-{uuid.uuid4().hex}", password="hashed-password"
    )
    session.add_all([tenant, user])
    session.flush()
    engagement = Engagement(
        tenant_id=tenant.id, user_id=user.id, name=f"Engagement {tenant_label}"
    )
    session.add(engagement)
    session.flush()
    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {tenant_label}",
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _add_report(session, *, tenant, user, engagement, version: int, is_current: bool):
    report = EngagementReport(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        version=version,
        status="ready",
        is_current=is_current,
        title=f"Report {version}",
        sections=[{"id": "summary"}],
        source_task_memo_ids=[],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={},
    )
    session.add(report)
    session.flush()
    return report


def _add_job(session, *, tenant, user, engagement):
    job = EngagementReportJob(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        status="ready",
        idempotency_key=f"retention-job-{uuid.uuid4()}",
        selected_task_memo_ids=[],
        include_candidate_findings=False,
        source_watermark={},
        completed_sections=[],
        total_sections=0,
        attempt_count=1,
        max_attempts=3,
    )
    session.add(job)
    session.flush()
    return job


def _add_memo(
    session, *, tenant, user, engagement, task, version: int, is_current: bool
):
    memo = TaskClosureMemo(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        version=version,
        is_current=is_current,
        status="ready",
        memo_mode="supported",
        source_watermark={},
        memo={"summary": "retention"},
    )
    session.add(memo)
    session.flush()
    return memo


def test_report_retention_candidates_preserve_current_and_scope_pending_deletion() -> (
    None
):
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="retention")
        other_tenant, other_user, other_engagement, _ = _seed_scope(
            session, tenant_label="retention-other"
        )
        historical = _add_report(
            session,
            tenant=tenant,
            user=user,
            engagement=engagement,
            version=1,
            is_current=False,
        )
        current = _add_report(
            session,
            tenant=tenant,
            user=user,
            engagement=engagement,
            version=2,
            is_current=True,
        )
        eligible = _add_report(
            session,
            tenant=tenant,
            user=user,
            engagement=engagement,
            version=3,
            is_current=False,
        )
        foreign = _add_report(
            session,
            tenant=other_tenant,
            user=other_user,
            engagement=other_engagement,
            version=1,
            is_current=False,
        )
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        historical.generated_at = old
        current.generated_at = old
        eligible.generated_at = old
        foreign.generated_at = old
        historical.delete_scheduled_at = old
        historical.delete_undo_until = old
        session.commit()

        repo = ReportingRetentionRepository(session)
        pending = repo.list_reports_pending_deletion(now=cutoff, tenant_id=tenant.id)
        candidates = repo.list_retention_candidate_reports(
            tenant_id=tenant.id, generated_before=cutoff
        )
        protected = repo.count_retention_protected_current_reports(
            tenant_id=tenant.id, generated_before=cutoff
        )

        assert [report.id for report in pending] == [historical.id]
        assert [report.id for report in candidates] == [eligible.id]
        assert protected == 1

    engine.dispose()


def test_job_and_memo_retention_candidates_are_tenant_scoped_and_deletable() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(session, tenant_label="cleanup")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, tenant_label="cleanup-other"
        )
        job = _add_job(session, tenant=tenant, user=user, engagement=engagement)
        foreign_job = _add_job(
            session, tenant=other_tenant, user=other_user, engagement=other_engagement
        )
        memo = _add_memo(
            session,
            tenant=tenant,
            user=user,
            engagement=engagement,
            task=task,
            version=1,
            is_current=False,
        )
        current_memo = _add_memo(
            session,
            tenant=tenant,
            user=user,
            engagement=engagement,
            task=task,
            version=2,
            is_current=True,
        )
        foreign_memo = _add_memo(
            session,
            tenant=other_tenant,
            user=other_user,
            engagement=other_engagement,
            task=other_task,
            version=1,
            is_current=False,
        )
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job.finished_at = old
        foreign_job.finished_at = old
        memo.generated_at = old
        current_memo.generated_at = old
        foreign_memo.generated_at = old
        session.commit()

        repo = ReportingRetentionRepository(session)
        jobs = repo.list_retention_candidate_report_jobs(
            tenant_id=tenant.id, finished_before=cutoff
        )
        memos = repo.list_retention_candidate_task_memos(
            tenant_id=tenant.id, memo_before=cutoff
        )
        protected = repo.count_retention_protected_current_task_memos(
            tenant_id=tenant.id, memo_before=cutoff
        )

        assert [candidate.id for candidate in jobs] == [job.id]
        assert [candidate.id for candidate in memos] == [memo.id]
        assert protected == 1
        assert repo.delete_report_jobs(jobs) == 1
        assert repo.delete_task_memos(memos) == 1
        assert session.get(EngagementReportJob, foreign_job.id) is not None
        assert session.get(TaskClosureMemo, foreign_memo.id) is not None

    engine.dispose()
