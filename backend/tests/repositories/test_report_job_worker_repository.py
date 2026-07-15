"""Test canonical worker-only report-job queue persistence boundaries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReportJob
from backend.models.tenant import Tenant
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)


REPORTING_REPOSITORY_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
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


def test_claim_report_job_rechecks_global_limit_inside_update() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="claim-global")
        first_job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
        )
        second_job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
        )

        repo = ReportJobWorkerRepository(session)
        stale_candidates = repo.list_claimable_report_jobs(
            now=datetime.now(timezone.utc),
            limit=2,
        )

        first_claim = repo.claim_report_job(
            job_id=stale_candidates[0].id,
            worker_id="worker-one",
            claimed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            global_limit=1,
        )
        second_claim = repo.claim_report_job(
            job_id=stale_candidates[1].id,
            worker_id="worker-two",
            claimed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            global_limit=1,
        )
        session.refresh(first_job)
        session.refresh(second_job)

        assert first_claim is not None
        assert first_job.status == "generating"
        assert second_claim is None
        assert second_job.status == "queued"

    engine.dispose()


def test_claim_report_job_rechecks_tenant_and_user_limits_inside_update() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="claim-scoped")
        tenant_limited_job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=1,
        )
        tenant_candidate = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
        )
        user_limited_job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=1,
        )
        user_candidate = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
        )

        repo = ReportJobWorkerRepository(session)
        tenant_claim = repo.claim_report_job(
            job_id=tenant_candidate.id,
            worker_id="tenant-worker",
            claimed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            per_tenant_limit=1,
        )
        user_claim = repo.claim_report_job(
            job_id=user_candidate.id,
            worker_id="user-worker",
            claimed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            per_user_limit=1,
        )
        session.refresh(tenant_limited_job)
        session.refresh(tenant_candidate)
        session.refresh(user_limited_job)
        session.refresh(user_candidate)

        assert tenant_limited_job.status == "generating"
        assert tenant_claim is None
        assert tenant_candidate.status == "queued"
        assert user_limited_job.status == "generating"
        assert user_claim is None
        assert user_candidate.status == "queued"

    engine.dispose()


def test_report_job_claim_limit_lock_executes_on_postgres_bind() -> None:
    class _PostgresBind:
        class dialect:
            name = "postgresql"

    class _FakeDb:
        def get_bind(self):
            return _PostgresBind()

        def execute(self, statement, params=None):  # noqa: ANN001
            calls.append((str(statement), dict(params or {})))

    calls: list[tuple[str, dict[str, int]]] = []
    repo = ReportJobWorkerRepository(_FakeDb())  # type: ignore[arg-type]

    repo.acquire_report_job_claim_limit_lock(namespace_key=2861, claim_key=1)

    assert len(calls) == 1
    assert "pg_advisory_xact_lock" in calls[0][0]
    assert calls[0][1] == {"namespace_key": 2861, "claim_key": 1}


def test_report_job_claim_limit_lock_is_noop_on_non_postgres_bind() -> None:
    class _SqliteBind:
        class dialect:
            name = "sqlite"

    class _FakeDb:
        def get_bind(self):
            return _SqliteBind()

        def execute(self, statement, params=None):  # noqa: ANN001
            calls.append((str(statement), dict(params or {})))

    calls: list[tuple[str, dict[str, int]]] = []
    repo = ReportJobWorkerRepository(_FakeDb())  # type: ignore[arg-type]

    repo.acquire_report_job_claim_limit_lock(namespace_key=2861, claim_key=1)

    assert calls == []


def test_worker_id_operations_ignore_request_scope_and_preserve_progress_state() -> (
    None
):
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="worker-id")
        job = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=1,
        )
        report_id = uuid.uuid4()
        repo = ReportJobWorkerRepository(session)

        fetched = repo.get_report_job_by_id(job_id=job.id)
        linked = repo.link_report_job_attempt_by_id(job_id=job.id, report_id=report_id)
        progressed = repo.update_report_job_progress_by_id(
            job_id=job.id,
            current_section_id="findings",
            completed_sections=["summary"],
            total_sections=3,
            generation_phase="drafting",
            clear_error=True,
        )

        assert fetched is not None
        assert fetched.id == job.id
        assert linked is not None
        assert linked.report_id == report_id
        assert progressed is not None
        assert progressed.current_section_id == "findings"
        assert progressed.completed_sections == ["summary"]
        assert progressed.generation_phase == "drafting"

    engine.dispose()


def test_worker_stale_recovery_requeues_or_fails_by_durable_id() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="worker-stale")
        stale = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=1,
            max_attempts=3,
        )
        retryable = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=1,
            max_attempts=3,
        )
        exhausted = _add_job(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            attempt_count=3,
            max_attempts=3,
        )
        locked_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        retry_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        for job in (stale, retryable, exhausted):
            job.locked_by = "worker"
            job.locked_at = locked_at
        session.commit()
        repo = ReportJobWorkerRepository(session)

        candidates = repo.list_stale_generating_report_jobs(stale_before=cutoff)
        requeued = repo.requeue_stale_report_job(job_id=stale.id, stale_before=cutoff)
        retried = repo.requeue_report_job_after_failure_by_id(
            job_id=retryable.id,
            last_error_code="transient",
            error_message="retry",
            next_attempt_at=retry_at,
            last_error_at=cutoff,
        )
        rejected = repo.requeue_report_job_after_failure_by_id(
            job_id=exhausted.id,
            last_error_code="transient",
            error_message="retry",
            next_attempt_at=retry_at,
            last_error_at=cutoff,
        )
        failed = repo.mark_report_job_failed_by_id(
            job_id=exhausted.id,
            error_message="exhausted",
            last_error_code="permanent",
            finished_at=cutoff,
        )

        assert [candidate.id for candidate in candidates] == [
            stale.id,
            retryable.id,
            exhausted.id,
        ]
        assert requeued is not None
        assert requeued.status == "queued"
        assert retried is not None
        assert retried.status == "queued"
        assert retried.next_attempt_at == retry_at.replace(tzinfo=None)
        assert rejected is None
        assert failed is not None
        assert failed.status == "failed"
        assert failed.finished_at == cutoff.replace(tzinfo=None)

    engine.dispose()
