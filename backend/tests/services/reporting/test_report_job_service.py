"""Tests for durable report job claim, recovery, and progress policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.models.tenant import Tenant
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)
from backend.services.reporting.report_job_service import (
    ReportJobClaimLimits,
    ReportJobService,
)


REPORT_JOB_SERVICE_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORT_JOB_SERVICE_TABLES)
    factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    return factory()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(
        slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}", name=f"Tenant {label}"
    )
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _add_user_engagement(
    db: Session,
    *,
    tenant: Tenant,
    label: str,
) -> tuple[User, Engagement]:
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add(user)
    db.flush()
    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    db.add(engagement)
    db.flush()
    return user, engagement


def _add_job(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    status: str = "queued",
    locked_by: str | None = None,
    locked_at: datetime | None = None,
    attempt_count: int = 0,
    max_attempts: int = 3,
    report_id: uuid.UUID | None = None,
) -> EngagementReportJob:
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=user_id,
        engagement_id=engagement_id,
        report_id=report_id,
        report_type="pentest",
        status=status,
        idempotency_key=f"job-{uuid.uuid4()}",
        selected_task_memo_ids=[str(uuid.uuid4())],
        include_candidate_findings=False,
        source_watermark={"schema_version": 1},
        completed_sections=[],
        total_sections=3,
        locked_by=locked_by,
        locked_at=locked_at,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        started_at=locked_at if status == "generating" else None,
    )
    db.add(job)
    db.flush()
    return job


def _add_report_attempt(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    version: int,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        version=version,
        status="generating",
        is_current=False,
        title="Report Attempt",
        sections=[],
        markdown_snapshot=None,
        source_task_memo_ids=[str(uuid.uuid4())],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={},
    )
    db.add(report)
    db.flush()
    return report


def test_claim_next_job_claims_queued_job_once_and_sets_lock_fields() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="claim-once")
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job.current_section_id = "stale_section"
    job.completed_sections = ["old_summary"]
    job.total_sections = 8
    db.commit()

    service = ReportJobService(db)
    limits = ReportJobClaimLimits(global_limit=5, per_tenant_limit=5, per_user_limit=5)
    claimed = service.claim_next_job(
        worker_id="worker-one",
        stale_after=timedelta(minutes=5),
        limits=limits,
    )
    second_claim = service.claim_next_job(
        worker_id="worker-two",
        stale_after=timedelta(minutes=5),
        limits=limits,
    )

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "generating"
    assert claimed.locked_by == "worker-one"
    assert claimed.locked_at is not None
    assert claimed.started_at is not None
    assert claimed.current_section_id == "stale_section"
    assert claimed.completed_sections == ["old_summary"]
    assert claimed.total_sections == 8
    assert claimed.attempt_count == 1
    assert second_claim is None


def test_claim_next_job_requests_claim_limit_advisory_lock() -> None:
    class _FakeRepository:
        def acquire_report_job_claim_limit_lock(
            self,
            *,
            namespace_key: int,
            claim_key: int,
        ) -> None:
            advisory_lock_calls.append(
                {"namespace_key": namespace_key, "claim_key": claim_key}
            )

        def count_active_report_jobs(self, **kwargs):  # noqa: ANN003
            return 0

        def list_claimable_report_jobs(self, *, now: datetime, limit: int):
            return [SimpleNamespace(id=uuid.uuid4(), tenant_id=1, user_id=2)]

        def list_stale_generating_report_jobs(self, **kwargs):  # noqa: ANN003
            return []

        def claim_report_job(self, **kwargs):  # noqa: ANN003
            claim_calls.append(dict(kwargs))
            return SimpleNamespace(
                id=kwargs["job_id"],
                tenant_id=1,
                user_id=2,
                engagement_id=3,
                report_type="pentest",
                attempt_count=1,
                max_attempts=3,
            )

    advisory_lock_calls: list[dict[str, int]] = []
    claim_calls: list[dict] = []

    claimed = ReportJobService(
        object(),
        worker_job_repository=_FakeRepository(),
    ).claim_next_job(
        worker_id="postgres-worker",
        stale_after=timedelta(minutes=5),
        limits=ReportJobClaimLimits(
            global_limit=5,
            per_tenant_limit=5,
            per_user_limit=5,
        ),
    )

    assert claimed is not None
    assert advisory_lock_calls == [{"namespace_key": 2861, "claim_key": 1}]
    assert claim_calls[0]["global_limit"] == 5
    assert claim_calls[0]["per_tenant_limit"] == 5
    assert claim_calls[0]["per_user_limit"] == 5


def test_claim_next_job_leaves_claim_lock_dialect_behavior_to_repository() -> None:
    class _FakeRepository:
        def acquire_report_job_claim_limit_lock(
            self,
            *,
            namespace_key: int,
            claim_key: int,
        ) -> None:
            advisory_lock_calls.append(
                {"namespace_key": namespace_key, "claim_key": claim_key}
            )

        def count_active_report_jobs(self, **kwargs):  # noqa: ANN003
            return 0

        def list_claimable_report_jobs(self, *, now: datetime, limit: int):
            return [SimpleNamespace(id=uuid.uuid4(), tenant_id=1, user_id=2)]

        def list_stale_generating_report_jobs(self, **kwargs):  # noqa: ANN003
            return []

        def claim_report_job(self, **kwargs):  # noqa: ANN003
            return SimpleNamespace(
                id=kwargs["job_id"],
                tenant_id=1,
                user_id=2,
                engagement_id=3,
                report_type="pentest",
                attempt_count=1,
                max_attempts=3,
            )

    advisory_lock_calls: list[dict[str, int]] = []

    claimed = ReportJobService(
        object(),
        worker_job_repository=_FakeRepository(),
    ).claim_next_job(
        worker_id="sqlite-worker",
        stale_after=timedelta(minutes=5),
        limits=ReportJobClaimLimits(
            global_limit=5,
            per_tenant_limit=5,
            per_user_limit=5,
        ),
    )

    assert claimed is not None
    assert advisory_lock_calls == [{"namespace_key": 2861, "claim_key": 1}]


def test_stale_generating_job_is_recovered_and_claimed_after_restart() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="stale-reclaim")
    old_locked_at = datetime.now(UTC) - timedelta(minutes=30)
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="old-worker",
        locked_at=old_locked_at,
        attempt_count=1,
        max_attempts=3,
    )
    db.commit()

    service_after_restart = ReportJobService(db)
    claimed = service_after_restart.claim_next_job(
        worker_id="new-worker",
        stale_after=timedelta(minutes=5),
        limits=ReportJobClaimLimits(
            global_limit=5,
            per_tenant_limit=5,
            per_user_limit=5,
        ),
    )

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "generating"
    assert claimed.locked_by == "new-worker"
    assert claimed.attempt_count == 2


def test_recover_stale_jobs_requeues_remaining_attempts_and_fails_exhausted_jobs() -> (
    None
):
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="recover")
    stale_locked_at = datetime.now(UTC) - timedelta(minutes=30)
    current_locked_at = datetime.now(UTC)
    recoverable = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="stale-worker",
        locked_at=stale_locked_at,
        attempt_count=1,
        max_attempts=3,
    )
    recoverable.current_section_id = "stale_section"
    recoverable.completed_sections = ["old_summary"]
    recoverable.total_sections = 8
    exhausted = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="exhausted-worker",
        locked_at=stale_locked_at,
        attempt_count=2,
        max_attempts=3,
    )
    fresh = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="fresh-worker",
        locked_at=current_locked_at,
        attempt_count=1,
        max_attempts=3,
    )
    db.commit()
    diagnostics = _FakeDiagnostics()

    result = ReportJobService(db, diagnostics=diagnostics).recover_stale_jobs(
        now=datetime.now(UTC),
        stale_after=timedelta(minutes=5),
        max_attempts=2,
    )
    db.refresh(recoverable)
    db.refresh(exhausted)
    db.refresh(fresh)

    assert result.requeued == 1
    assert result.failed == 1
    assert recoverable.status == "queued"
    assert recoverable.locked_by is None
    assert recoverable.locked_at is None
    assert recoverable.current_section_id == "stale_section"
    assert recoverable.completed_sections == ["old_summary"]
    assert recoverable.total_sections == 8
    assert exhausted.status == "failed"
    assert exhausted.last_error_code == "max_attempts_exceeded"
    assert exhausted.error_message == "Report generation exceeded the retry limit."
    assert fresh.status == "generating"
    assert fresh.locked_by == "fresh-worker"
    assert diagnostics.requeued == [
        {
            "job_id": recoverable.id,
            "report_id": None,
            "engagement_id": engagement.id,
            "report_type": "pentest",
            "reason": "stale_attempt_recovered",
            "attempt_count": 1,
            "max_attempts": 2,
        }
    ]
    assert diagnostics.failed == [
        {
            "job_id": exhausted.id,
            "report_id": None,
            "engagement_id": engagement.id,
            "report_type": "pentest",
            "reason": "max_attempts_exceeded",
            "attempt_count": 2,
            "max_attempts": 2,
        }
    ]


def test_recover_stale_jobs_preserves_recoverable_linked_generating_attempts() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="recover-linked-report")
    stale_locked_at = datetime.now(UTC) - timedelta(minutes=30)
    recoverable_report = _add_report_attempt(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
    )
    exhausted_report = _add_report_attempt(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=2,
    )
    recoverable = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="stale-worker",
        locked_at=stale_locked_at,
        attempt_count=1,
        max_attempts=3,
        report_id=recoverable_report.id,
    )
    exhausted = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="exhausted-worker",
        locked_at=stale_locked_at,
        attempt_count=2,
        max_attempts=3,
        report_id=exhausted_report.id,
    )
    db.commit()

    result = ReportJobService(db).recover_stale_jobs(
        now=datetime.now(UTC),
        stale_after=timedelta(minutes=5),
        max_attempts=2,
    )
    db.refresh(recoverable)
    db.refresh(exhausted)
    db.refresh(recoverable_report)
    db.refresh(exhausted_report)

    assert result.requeued == 1
    assert result.failed == 1
    assert recoverable.status == "queued"
    assert recoverable_report.status == "generating"
    assert recoverable_report.is_current is False
    assert recoverable_report.error_message is None
    assert recoverable_report.generation_metadata == {}
    assert exhausted.status == "failed"
    assert exhausted_report.status == "failed"
    assert exhausted_report.is_current is False
    assert exhausted_report.error_message == (
        "Report generation exceeded the retry limit."
    )
    assert exhausted_report.generation_metadata == {
        "error_code": "max_attempts_exceeded"
    }


def test_claim_next_job_enforces_global_and_per_tenant_active_limits() -> None:
    db = _build_session()
    tenant_one, user_one, engagement_one = _seed_scope(db, label="tenant-one")
    tenant_two, user_two, engagement_two = _seed_scope(db, label="tenant-two")
    _add_job(
        db,
        tenant_id=tenant_one.id,
        user_id=user_one.id,
        engagement_id=engagement_one.id,
        status="generating",
        locked_by="active-worker",
        locked_at=datetime.now(UTC),
        attempt_count=1,
    )
    _add_job(
        db,
        tenant_id=tenant_one.id,
        user_id=user_one.id,
        engagement_id=engagement_one.id,
    )
    other_tenant_job = _add_job(
        db,
        tenant_id=tenant_two.id,
        user_id=user_two.id,
        engagement_id=engagement_two.id,
    )
    db.commit()

    service = ReportJobService(db)
    assert (
        service.claim_next_job(
            worker_id="global-blocked-worker",
            stale_after=timedelta(minutes=5),
            limits=ReportJobClaimLimits(
                global_limit=1,
                per_tenant_limit=5,
                per_user_limit=5,
            ),
        )
        is None
    )

    claimed = service.claim_next_job(
        worker_id="tenant-skip-worker",
        stale_after=timedelta(minutes=5),
        limits=ReportJobClaimLimits(
            global_limit=5,
            per_tenant_limit=1,
            per_user_limit=5,
        ),
    )

    assert claimed is not None
    assert claimed.id == other_tenant_job.id


def test_claim_next_job_enforces_per_user_active_limit() -> None:
    db = _build_session()
    tenant, user_one, engagement_one = _seed_scope(db, label="user-limit")
    user_two, engagement_two = _add_user_engagement(
        db,
        tenant=tenant,
        label="user-limit-two",
    )
    _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user_one.id,
        engagement_id=engagement_one.id,
        status="generating",
        locked_by="active-worker",
        locked_at=datetime.now(UTC),
        attempt_count=1,
    )
    _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user_one.id,
        engagement_id=engagement_one.id,
    )
    other_user_job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user_two.id,
        engagement_id=engagement_two.id,
    )
    db.commit()

    claimed = ReportJobService(db).claim_next_job(
        worker_id="user-skip-worker",
        stale_after=timedelta(minutes=5),
        limits=ReportJobClaimLimits(
            global_limit=5,
            per_tenant_limit=5,
            per_user_limit=1,
        ),
    )

    assert claimed is not None
    assert claimed.id == other_user_job.id


def test_mark_progress_and_fail_job_persist_worker_visible_metadata() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="progress-fail")
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="worker",
        locked_at=datetime.now(UTC),
        attempt_count=1,
    )
    db.commit()

    service = ReportJobService(db)
    previous_locked_at = job.locked_at
    progress = service.mark_progress(
        job_id=job.id,
        current_section_id="findings",
        completed_sections=["summary"],
        total_sections=3,
    )
    assert progress is not None
    assert progress.current_section_id == "findings"
    assert progress.completed_sections == ["summary"]
    assert progress.total_sections == 3
    assert progress.locked_at is not None
    assert previous_locked_at is not None
    assert progress.locked_at.replace(tzinfo=None) >= previous_locked_at.replace(
        tzinfo=None
    )

    failed = service.fail_job(
        job_id=job.id,
        reason="section_generation_failed",
        safe_message="Section generation failed.",
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.locked_by is None
    assert failed.locked_at is None
    assert failed.last_error_code == "section_generation_failed"
    assert failed.error_message == "Section generation failed."


def test_failure_requeue_preserves_attempt_progress() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="failure-requeue-reset")
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="worker",
        locked_at=datetime.now(UTC),
        attempt_count=1,
        max_attempts=3,
    )
    job.current_section_id = "findings"
    job.completed_sections = ["summary"]
    job.total_sections = 8
    db.commit()

    repository = ReportJobWorkerRepository(db)
    requeued = repository.requeue_report_job_after_failure_by_id(
        job_id=job.id,
        last_error_code="section_timeout",
        error_message="Report section generation timed out.",
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=5),
        last_error_at=datetime.now(UTC),
    )

    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.current_section_id == "findings"
    assert requeued.completed_sections == ["summary"]
    assert requeued.total_sections == 8
    assert requeued.next_attempt_at is not None
    assert requeued.locked_by is None
    assert requeued.locked_at is None
    assert requeued.last_error_code == "section_timeout"
    assert (
        repository.claim_report_job(
            job_id=job.id,
            worker_id="early-worker",
            claimed_at=datetime.now(UTC),
        )
        is None
    )

    requeued.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    claimed = repository.claim_report_job(
        job_id=job.id,
        worker_id="due-worker",
        claimed_at=datetime.now(UTC),
    )

    assert claimed is not None
    assert claimed.status == "generating"
    assert claimed.attempt_count == 2


def test_retry_backoff_uses_durable_five_and_ten_second_delays() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="retry-backoff")
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="generating",
        locked_by="worker",
        locked_at=datetime.now(UTC),
        attempt_count=1,
        max_attempts=3,
    )
    db.commit()
    service = ReportJobService(db)
    first_failed_at = datetime.now(UTC)

    first = service.requeue_after_failure(
        job_id=job.id,
        reason="section_timeout",
        safe_message="Report section generation timed out.",
        now=first_failed_at,
    )

    assert first is not None
    assert first.next_attempt_at.replace(tzinfo=UTC) == first_failed_at + timedelta(
        seconds=5
    )

    first.status = "generating"
    first.attempt_count = 2
    first.locked_by = "worker"
    first.locked_at = datetime.now(UTC)
    db.flush()
    second_failed_at = first_failed_at + timedelta(seconds=20)
    second = service.requeue_after_failure(
        job_id=job.id,
        reason="section_timeout",
        safe_message="Report section generation timed out.",
        now=second_failed_at,
    )

    assert second is not None
    assert second.next_attempt_at.replace(tzinfo=UTC) == second_failed_at + timedelta(
        seconds=10
    )


class _FakeDiagnostics:
    def __init__(self) -> None:
        self.requeued: list[dict] = []
        self.failed: list[dict] = []

    def stale_job_requeued(self, **kwargs):  # noqa: ANN003
        self.requeued.append(dict(kwargs))

    def stale_job_failed(self, **kwargs):  # noqa: ANN003
        self.failed.append(dict(kwargs))
