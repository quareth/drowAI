"""Tests for the engagement report background scheduler lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

import pytest
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
from backend.services.reporting.report_job_service import ReportJobClaimLimits
from backend.services.reporting.report_scheduler import ReportScheduler
from backend.services.reporting.report_worker_types import ReportWorkerRunResult


REPORT_SCHEDULER_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _build_session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORT_SCHEDULER_TABLES)
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _seed_scope(db: Session) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex[:8]}", name="Tenant")
    user = User(username=f"user-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Engagement",
    )
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _add_job(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    status: str = "queued",
    locked_at: datetime | None = None,
    attempt_count: int = 0,
    max_attempts: int = 3,
) -> EngagementReportJob:
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        status=status,
        idempotency_key=f"report-job-{uuid.uuid4()}",
        selected_task_memo_ids=[str(uuid.uuid4())],
        include_candidate_findings=False,
        source_watermark={"schema_version": 1},
        completed_sections=["stale_section"] if status == "generating" else [],
        total_sections=3,
        locked_by="old-worker" if status == "generating" else None,
        locked_at=locked_at,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        started_at=locked_at if status == "generating" else None,
    )
    db.add(job)
    db.flush()
    return job


@pytest.mark.asyncio
async def test_scheduler_startup_recovers_stale_report_jobs() -> None:
    factory = _build_session_factory()
    with factory() as db:
        tenant, user, engagement = _seed_scope(db)
        job = _add_job(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            status="generating",
            locked_at=datetime.now(UTC) - timedelta(minutes=30),
            attempt_count=1,
            max_attempts=3,
        )
        job_id = job.id
        db.commit()

    async def fake_worker_runner(*_args, **_kwargs) -> ReportWorkerRunResult:
        return ReportWorkerRunResult(
            claimed=False,
            job_id=None,
            report_id=None,
            status="idle",
        )

    scheduler = ReportScheduler(
        session_factory=factory,
        worker_runner=fake_worker_runner,
        poll_interval_seconds=60,
    )
    await scheduler.start()
    assert scheduler.is_running is True
    await scheduler.stop()
    assert scheduler.is_running is False

    with factory() as db:
        recovered = db.get(EngagementReportJob, job_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.locked_at is None
        assert recovered.completed_sections == ["stale_section"]


@pytest.mark.asyncio
async def test_scheduler_dispatch_uses_one_session_per_worker_claim() -> None:
    sessions: list[_FakeSession] = []
    worker_calls: list[str] = []

    def session_factory() -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    async def fake_worker_runner(db, *, worker_id: str, **_kwargs):
        assert db in sessions
        worker_calls.append(worker_id)
        await asyncio.sleep(0)
        return ReportWorkerRunResult(
            claimed=False,
            job_id=None,
            report_id=None,
            status="idle",
        )

    scheduler = ReportScheduler(
        session_factory=session_factory,
        worker_runner=fake_worker_runner,
        claim_limits=ReportJobClaimLimits(
            global_limit=2,
            per_tenant_limit=2,
            per_user_limit=2,
        ),
    )

    dispatched = await scheduler.dispatch_once()
    active_tasks = list(scheduler._state.active_tasks)
    await asyncio.gather(*active_tasks)

    assert dispatched == 2
    assert len(worker_calls) == 2
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)


@pytest.mark.asyncio
async def test_scheduler_stop_cancels_loop_without_dispatching_more_claims() -> None:
    worker_calls: list[str] = []
    sleep_started = asyncio.Event()

    async def fake_worker_runner(*_args, **_kwargs):
        worker_calls.append("claim")
        return ReportWorkerRunResult(
            claimed=False,
            job_id=None,
            report_id=None,
            status="idle",
        )

    async def blocking_sleep(_seconds: float) -> None:
        sleep_started.set()
        await asyncio.Event().wait()

    scheduler = ReportScheduler(
        session_factory=_build_session_factory(),
        worker_runner=fake_worker_runner,
        poll_interval_seconds=1,
        sleep_func=blocking_sleep,
    )

    await scheduler.start()
    await sleep_started.wait()
    await scheduler.stop()
    calls_after_stop = len(worker_calls)
    await asyncio.sleep(0)

    assert len(worker_calls) == calls_after_stop


@pytest.mark.asyncio
async def test_scheduler_start_fails_closed_when_initial_database_session_fails() -> (
    None
):
    def unavailable_session():
        raise RuntimeError("database unavailable")

    scheduler = ReportScheduler(session_factory=unavailable_session)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await scheduler.start()

    assert scheduler.is_running is False


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.rolled_back = False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True
