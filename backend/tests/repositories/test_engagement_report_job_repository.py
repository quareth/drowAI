"""Test canonical requester-scoped report-job persistence boundaries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReport, EngagementReportJob
from backend.models.tenant import Tenant
from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)


REPORTING_REPOSITORY_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
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
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {tenant_label}",
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


def _add_report(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    report_type: str,
    version: int,
    status: str,
    is_current: bool,
    source_task_memo_ids: list[str] | None = None,
    generation_metadata: dict | None = None,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type=report_type,
        version=version,
        status=status,
        is_current=is_current,
        title=f"Report {version}",
        sections=[{"id": "summary"}],
        source_task_memo_ids=list(source_task_memo_ids or []),
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata=dict(generation_metadata or {}),
    )
    session.add(report)
    session.flush()
    return report


def _add_job(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    report_id: uuid.UUID | None = None,
    status: str = "queued",
    requested_by_user_id: int | None = None,
    report_type: str = "pentest",
    idempotency_key: str | None = None,
    selected_task_memo_ids: list[str] | None = None,
    include_candidate_findings: bool = False,
    source_watermark: dict | None = None,
    completed_sections: list[str] | None = None,
    total_sections: int = 0,
    attempt_count: int = 0,
    max_attempts: int = 3,
) -> EngagementReportJob:
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=requested_by_user_id or user_id,
        engagement_id=engagement_id,
        report_id=report_id,
        report_type=report_type,
        status=status,
        idempotency_key=idempotency_key or f"job-{uuid.uuid4()}",
        selected_task_memo_ids=list(selected_task_memo_ids or []),
        include_candidate_findings=include_candidate_findings,
        source_watermark=dict(source_watermark or {}),
        completed_sections=list(completed_sections or []),
        total_sections=total_sections,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.flush()
    return job


def test_scoped_report_job_reads_are_scoped() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="job-owner")
        _, other_user, _, _ = _seed_scope(session, tenant_label="job-other")
        job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
        )

        repo = EngagementReportJobRepository(session)

        fetched_job = repo.get_report_job(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=job.id,
        )
        cross_user_job = repo.get_report_job(
            tenant_id=tenant.id,
            user_id=other_user.id,
            engagement_id=engagement.id,
            job_id=job.id,
        )

        assert fetched_job is not None
        assert fetched_job.id == job.id
        assert cross_user_job is None
        assert (
            repo.get_report_job(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                job_id="not-a-uuid",
            )
            is None
        )

    engine.dispose()


def test_active_report_job_lookup_requires_scope_key_and_active_status() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="active-job")
        other_tenant, other_user, other_engagement, _ = _seed_scope(
            session, tenant_label="active-job-other"
        )
        active = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            status="queued",
            idempotency_key="same-source-active",
        )
        _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            status="ready",
            idempotency_key="same-source-ready",
        )
        _add_job(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            requested_by_user_id=other_user.id,
            engagement_id=other_engagement.id,
            report_type="pentest",
            status="generating",
            idempotency_key="same-source-active",
        )

        repo = EngagementReportJobRepository(session)

        found = repo.get_active_job_by_idempotency_key(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="same-source-active",
        )

        assert found is not None
        assert found.id == active.id
        assert (
            repo.get_active_job_by_idempotency_key(
                tenant_id=tenant.id,
                user_id=user.id,
                requested_by_user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
                idempotency_key="same-source-ready",
            )
            is None
        )
        assert (
            repo.get_active_job_by_idempotency_key(
                tenant_id=tenant.id,
                user_id=other_user.id,
                requested_by_user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
                idempotency_key="same-source-active",
            )
            is None
        )

    engine.dispose()


def test_create_report_job_persists_source_fields_and_rereads_idempotent_conflict() -> (
    None
):
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="create-job")
        memo_one_id = uuid.uuid4()
        memo_two_id = uuid.uuid4()
        repo = EngagementReportJobRepository(session)

        created = repo.create_report_job(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="source-idempotency",
            selected_task_memo_ids=[memo_two_id, memo_one_id, memo_two_id],
            include_candidate_findings=True,
            source_watermark={"hash": "source-hash"},
            total_sections=6,
            max_attempts=5,
        )
        duplicate = repo.create_report_job(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="source-idempotency",
            selected_task_memo_ids=[memo_one_id],
            include_candidate_findings=False,
            source_watermark={"hash": "other"},
        )

        assert created.status == "queued"
        assert created.idempotency_key == "source-idempotency"
        assert created.selected_task_memo_ids == sorted(
            [str(memo_one_id), str(memo_two_id)]
        )
        assert created.include_candidate_findings is True
        assert created.source_watermark == {"hash": "source-hash"}
        assert created.completed_sections == []
        assert created.current_section_id is None
        assert created.total_sections == 6
        assert created.attempt_count == 0
        assert created.max_attempts == 5
        assert duplicate.id == created.id
        assert session.query(EngagementReportJob).count() == 1

    engine.dispose()


def test_report_job_progress_ready_and_failed_updates_are_scoped() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="job-update")
        other_tenant, other_user, other_engagement, _ = _seed_scope(
            session, tenant_label="job-update-other"
        )
        report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
        )
        job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            total_sections=3,
        )
        failed_job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
        )

        repo = EngagementReportJobRepository(session)
        progress = repo.update_report_job_progress(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=job.id,
            current_section_id="findings",
            completed_sections=["summary"],
            total_sections=3,
        )
        assert progress is not None
        assert progress.current_section_id == "findings"
        assert progress.completed_sections == ["summary"]
        assert progress.total_sections == 3

        ready = repo.mark_report_job_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=job.id,
            report_id=report.id,
            finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        failed = repo.mark_report_job_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=failed_job.id,
            last_error_code="section_generation_failed",
            error_message="section generation failed",
            finished_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert ready is not None
        assert ready.status == "ready"
        assert ready.report_id == report.id
        assert ready.current_section_id is None
        assert ready.error_message is None
        assert ready.finished_at == datetime(2026, 1, 1)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.last_error_code == "section_generation_failed"
        assert failed.error_message == "section generation failed"
        assert report.is_current is True
        assert (
            repo.update_report_job_progress(
                tenant_id=other_tenant.id,
                user_id=other_user.id,
                engagement_id=other_engagement.id,
                job_id=job.id,
                current_section_id=None,
                completed_sections=[],
                total_sections=0,
            )
            is None
        )
        assert (
            repo.mark_report_job_ready(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                job_id=failed_job.id,
                report_id="not-a-uuid",
            )
            is None
        )

    engine.dispose()


def test_scoped_job_repository_methods_do_not_commit_transactions() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="no-commit-job")
        report_source_id = uuid.uuid4()
        report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
        )
        repo = EngagementReportJobRepository(session)
        session.commit = Mock(
            side_effect=AssertionError("repository methods must not commit")
        )

        report_job = repo.create_report_job(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="no-commit-report-job",
            selected_task_memo_ids=[report_source_id],
            include_candidate_findings=False,
            source_watermark={"hash": "no-commit-hash"},
            total_sections=1,
        )
        repo.get_active_job_by_idempotency_key(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="no-commit-report-job",
        )
        repo.update_report_job_progress(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=report_job.id,
            current_section_id="summary",
            completed_sections=[],
            total_sections=1,
        )
        repo.mark_report_job_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=report_job.id,
            report_id=report.id,
        )
        failed_report_job = repo.create_report_job(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            idempotency_key="no-commit-failed-report-job",
            selected_task_memo_ids=[report_source_id],
            include_candidate_findings=False,
            source_watermark={"hash": "no-commit-hash"},
        )
        repo.mark_report_job_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            job_id=failed_report_job.id,
            error_message="failed",
        )

        session.commit.assert_not_called()

    engine.dispose()


def test_requester_job_lookup_requires_requester_and_owned_engagement() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="requester")
        _, other_user, _, _ = _seed_scope(session, tenant_label="requester-other")
        job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            requested_by_user_id=user.id,
        )
        repo = EngagementReportJobRepository(session)

        owned = repo.get_report_job_by_id_for_requester(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            job_id=job.id,
        )
        wrong_requester = repo.get_report_job_by_id_for_requester(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=other_user.id,
            job_id=job.id,
        )
        active = repo.get_active_report_job_for_requester(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
        )

        assert owned is not None
        assert owned.id == job.id
        assert wrong_requester is None
        assert active is not None
        assert active.id == job.id

    engine.dispose()
