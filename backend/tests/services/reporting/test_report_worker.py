"""Tests for durable engagement report worker lifecycle behavior."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.models.tenant import Tenant
from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_SECTION_SCHEMA_VERSION,
)
from backend.services.reporting.report_job_service import ReportJobService
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
    ReportSectionGenerationResult,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.report_section_prompt import RenderedReportSectionPrompt
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
    ReportSectionValidationIssue,
    ReportSectionValidationResult,
)
from backend.services.reporting.report_worker import ReportWorker
from backend.services.reporting.report_worker import _claimed_job_scope
from backend.services.reporting.report_worker_failure import _ReportWorkerFailure


REPORT_WORKER_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORT_WORKER_TABLES)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return factory()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(
        slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}",
        name=f"Tenant {label}",
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


def _add_memo(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int = 1,
) -> TaskClosureMemo:
    task = Task(
        id=task_id,
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        name=f"Task {task_id}",
        status=TaskStatus.STOPPED.value,
    )
    db.add(task)
    db.flush()
    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=1,
        is_current=True,
        status="ready",
        memo_mode="supported",
        source_watermark={"schema_version": 1, "task_id": task_id},
        memo={"summary": "Ready memo"},
    )
    db.add(memo)
    db.flush()
    return memo


def _add_job(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    selected_task_memo_ids: list[str],
    max_attempts: int = 3,
) -> EngagementReportJob:
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        status="queued",
        idempotency_key=f"report-job-{uuid.uuid4()}",
        selected_task_memo_ids=selected_task_memo_ids,
        include_candidate_findings=False,
        llm_runtime_selection=_runtime_selection_payload(user_id=user_id),
        source_watermark={"schema_version": 1, "hash": "source-hash"},
        completed_sections=[],
        total_sections=len(get_report_section_plan("pentest").sections),
        max_attempts=max_attempts,
    )
    db.add(job)
    db.flush()
    return job


def _runtime_selection_payload(*, user_id: int) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "model": "claude-haiku-report",
        "credential_ref": {"user_id": user_id, "provider": "anthropic"},
        "reasoning_effort": None,
    }


def _runtime_model_metadata() -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "model": "claude-haiku-report",
        "reasoning_effort": None,
    }


def _add_current_report(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    memo_id: str,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type="pentest",
        version=1,
        status="ready",
        is_current=True,
        title="Previous Current Report",
        sections=[],
        markdown_snapshot="# Previous Current Report\n",
        source_task_memo_ids=[memo_id],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={"source_watermark_hash": "previous"},
    )
    db.add(report)
    db.flush()
    return report


class _FakeContextBuilder:
    def build(self, *, selected_memos: list[TaskClosureMemo], **kwargs: Any) -> Any:
        return SimpleNamespace(
            candidate_policy=SimpleNamespace(include_candidate_findings=False),
            selected_memos=tuple(
                SimpleNamespace(memo_id=str(memo.id)) for memo in selected_memos
            ),
            compatible_knowledge_refs=(),
            compatible_evidence_refs=(),
            allowed_task_memo_ids=frozenset(str(memo.id) for memo in selected_memos),
            allowed_knowledge_refs=frozenset(),
            allowed_evidence_refs=frozenset(),
            source_watermark=SimpleNamespace(
                hash="source-hash",
                generation_metadata={"source_watermark_hash": "source-hash"},
            ),
        )


class _FakeSourceWatermarks:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale

    def compute_for_task(self, *, task_id: int, **kwargs: Any) -> dict[str, Any]:
        watermark = {"schema_version": 1, "task_id": task_id}
        if self.stale:
            watermark["changed"] = True
        return watermark


class _FakePromptRenderer:
    def render(
        self, *, section_plan_item: Any, **kwargs: Any
    ) -> RenderedReportSectionPrompt:
        return RenderedReportSectionPrompt(
            system_prompt="system",
            user_prompt="user",
            metadata={
                "section_id": section_plan_item.section_id,
                "section_type": section_plan_item.section_type,
                "title": section_plan_item.title,
            },
            report_context_json="{}",
            section_plan_json="{}",
        )


class _FakeSectionGenerator:
    def __init__(self, *, fail_section_id: str | None = None) -> None:
        self.fail_section_id = fail_section_id
        self.calls: list[str] = []
        self.runtime_selection_calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        rendered_prompt: RenderedReportSectionPrompt,
        **kwargs: Any,
    ) -> ReportSectionGenerationResult:
        section_id = str(rendered_prompt.metadata["section_id"])
        self.calls.append(section_id)
        self.runtime_selection_calls.append(dict(kwargs.get("runtime_selection") or {}))
        if section_id == self.fail_section_id:
            raise ReportSectionGenerationError(
                reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
                safe_message="LLM report section generation failed.",
                metadata={"section_id": section_id},
                retryable=True,
            )
        return ReportSectionGenerationResult(
            payload={
                "schema_version": REPORT_SECTION_SCHEMA_VERSION,
                "section_id": section_id,
                "section_type": str(rendered_prompt.metadata["section_type"]),
                "title": str(rendered_prompt.metadata["title"]),
                "status": "ready",
                "content_markdown": f"{section_id} content",
                "blocks": [],
                "source_refs": {
                    "task_memo_ids": [],
                    "knowledge_refs": [],
                    "evidence_refs": [],
                },
                "unsupported_notes": [],
                "generation_notes": [],
            },
            metadata={"section_id": section_id, "provider": "fake"},
        )


class _FakeSectionValidator:
    def validate(
        self, *, payload: dict[str, Any], **kwargs: Any
    ) -> ReportSectionValidationResult:
        return ReportSectionValidationResult(
            payload=payload,
            metadata={"validation_status": "passed"},
        )


class _InjectedReportingRepository(
    EngagementReportJobRepository,
    EngagementReportRepository,
    ReportJobWorkerRepository,
    TaskClosureMemoRepository,
):
    """Provide all repository roles required by injected worker test doubles."""


class _TransientReadyFailureRepository(_InjectedReportingRepository):
    """Fail the first ready-report persistence call like a lost DB connection."""

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self.fail_ready_once = True

    def mark_report_ready(self, **kwargs: Any) -> EngagementReport | None:
        if self.fail_ready_once:
            self.fail_ready_once = False
            raise OperationalError(
                "UPDATE engagement_reports", {}, Exception("temporary")
            )
        return super().mark_report_ready(**kwargs)


class _MissingSectionReportCheckpointRepository(_InjectedReportingRepository):
    """Return a missing report after flushing the first section update."""

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self.fail_section_checkpoint_once = True

    def update_report_sections(self, **kwargs: Any) -> EngagementReport | None:
        report = super().update_report_sections(**kwargs)
        if self.fail_section_checkpoint_once and len(kwargs["sections"]) == 1:
            self.fail_section_checkpoint_once = False
            return None
        return report


class _MissingSectionJobCheckpointService(ReportJobService):
    """Return a missing job after flushing the first completed-section update."""

    def __init__(
        self,
        db: Session,
        *,
        repositories: _InjectedReportingRepository,
    ) -> None:
        super().__init__(
            db,
            report_repository=repositories,
            worker_job_repository=repositories,
        )
        self.fail_section_checkpoint_once = True

    def mark_progress(self, **kwargs: Any) -> EngagementReportJob | None:
        job = super().mark_progress(**kwargs)
        if self.fail_section_checkpoint_once and kwargs["completed_sections"]:
            self.fail_section_checkpoint_once = False
            return None
        return job


class _RejectedRequeueRepository(_InjectedReportingRepository):
    """Simulate a requeue rejected by a changed durable job state."""

    def __init__(self, db: Session) -> None:
        super().__init__(db)
        self.requeue_attempted = False

    def requeue_report_job_after_failure_by_id(self, **_kwargs: Any) -> None:
        self.requeue_attempted = True
        return None


class _MissingTerminalReportRepository(_InjectedReportingRepository):
    """Mutate a linked report, then simulate a missing persistence result."""

    def mark_report_failed(self, **kwargs: Any) -> EngagementReport | None:
        super().mark_report_failed(**kwargs)
        return None


class _FailingSectionValidator:
    def validate(self, **kwargs: Any) -> ReportSectionValidationResult:
        raise ReportSectionValidationError(
            issues=(
                ReportSectionValidationIssue(
                    code="customer_markdown_internal_identifier",
                    path="blocks.0.content_markdown",
                    message="Leaked evidence_archive:secret in generated Markdown.",
                ),
            )
        )


def _section_payload(section: Any) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SECTION_SCHEMA_VERSION,
        "section_id": section.section_id,
        "section_type": str(section.section_type),
        "title": str(section.title),
        "status": "ready",
        "content_markdown": f"{section.section_id} content",
        "blocks": [],
        "source_refs": {
            "task_memo_ids": [],
            "knowledge_refs": [],
            "evidence_refs": [],
        },
        "unsupported_notes": [],
        "generation_notes": [],
    }


@pytest.mark.asyncio
async def test_worker_happy_path_creates_current_ready_report_and_ready_job() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="happy")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    db.commit()

    generator = _FakeSectionGenerator()
    result = await _worker(db, generator=generator).run_once(worker_id="worker-happy")

    db.refresh(job)
    report = (
        db.query(EngagementReport).filter(EngagementReport.id == result.report_id).one()
    )
    section_ids = [section["section_id"] for section in report.sections]
    expected_section_ids = [
        section.section_id for section in get_report_section_plan("pentest").sections
    ]

    assert result.claimed is True
    assert result.status == "ready"
    assert job.status == "ready"
    assert job.report_id == report.id
    assert job.completed_sections == expected_section_ids
    assert job.finished_at is not None
    assert report.status == "ready"
    assert report.is_current is True
    assert section_ids == expected_section_ids
    assert report.markdown_snapshot.startswith("# Pentest Report")
    assert report.generation_metadata["source_watermark_hash"] == "source-hash"
    assert (
        report.generation_metadata["llm_runtime_selection"] == _runtime_model_metadata()
    )
    assert generator.runtime_selection_calls == [
        _runtime_selection_payload(user_id=user.id) for _section in expected_section_ids
    ]
    assert "system_prompt" not in str(report.generation_metadata)
    assert "user_prompt" not in str(report.generation_metadata)


@pytest.mark.asyncio
async def test_worker_resumes_checkpointed_generating_report_sections() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="resume")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    first_section = get_report_section_plan("pentest").sections[0]
    report = EngagementReport(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        version=1,
        status="generating",
        is_current=False,
        title="Pentest Report",
        sections=[_section_payload(first_section)],
        markdown_snapshot=None,
        source_task_memo_ids=[str(memo.id)],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={
            "sections": [
                {
                    "section_id": first_section.section_id,
                    "generation": {"provider": "fake"},
                    "validation": {"validation_status": "passed"},
                }
            ]
        },
    )
    db.add(report)
    db.flush()
    job.report_id = report.id
    job.completed_sections = [first_section.section_id]
    job.total_sections = len(get_report_section_plan("pentest").sections)
    db.commit()

    generator = _FakeSectionGenerator()
    result = await _worker(db, generator=generator).run_once(worker_id="worker-resume")

    db.refresh(job)
    db.refresh(report)
    assert result.status == "ready"
    assert "executive_summary" not in generator.calls
    assert generator.calls[0] == "scope_and_methodology"
    assert job.completed_sections == [
        section.section_id for section in get_report_section_plan("pentest").sections
    ]
    assert report.sections[0]["section_id"] == "executive_summary"


@pytest.mark.asyncio
async def test_worker_requeues_generation_failure_when_attempts_remain() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="retry")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
        max_attempts=2,
    )
    db.commit()

    generator = _FakeSectionGenerator(fail_section_id="executive_summary")
    first = await _worker(
        db,
        generator=generator,
    ).run_once(worker_id="worker-retry")
    db.refresh(job)
    generating_report = db.query(EngagementReport).one()

    assert first.status == "queued"
    assert job.status == "queued"
    assert job.attempt_count == 1
    assert job.report_id == generating_report.id
    assert job.last_error_code == REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED
    assert job.error_message == "LLM report section generation failed."
    assert generating_report.status == "generating"
    assert generating_report.is_current is False
    assert generating_report.generation_metadata["last_failure_retryable"] is True
    assert (
        generating_report.generation_metadata["failed_section_id"]
        == "executive_summary"
    )
    assert job.next_attempt_at is not None
    assert generator.calls == ["executive_summary"]
    assert "SECRET" not in job.error_message


@pytest.mark.asyncio
async def test_worker_retry_resumes_at_first_unfinished_section() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="retry-checkpoint")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    plan = get_report_section_plan("pentest").sections
    failed_section = plan[3]
    db.commit()

    generator = _FakeSectionGenerator(fail_section_id=failed_section.section_id)
    first = await _worker(db, generator=generator).run_once(
        worker_id="worker-checkpoint-1"
    )
    db.refresh(job)
    report = db.query(EngagementReport).one()

    assert first.status == "queued"
    assert job.completed_sections == [section.section_id for section in plan[:3]]
    assert [section["section_id"] for section in report.sections] == [
        section.section_id for section in plan[:3]
    ]
    assert len(report.generation_metadata["sections"]) == 3

    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    generator.fail_section_id = None
    db.commit()
    second = await _worker(db, generator=generator).run_once(
        worker_id="worker-checkpoint-2"
    )
    db.refresh(job)
    db.refresh(report)

    assert second.status == "ready"
    assert job.report_id == report.id
    assert generator.calls == [
        *[section.section_id for section in plan[:4]],
        *[section.section_id for section in plan[3:]],
    ]
    assert len(report.generation_metadata["sections"]) == len(plan)


@pytest.mark.asyncio
async def test_section_checkpoint_rolls_back_when_report_update_is_missing() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="missing-report-checkpoint")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    repository = _MissingSectionReportCheckpointRepository(db)
    db.commit()

    result = await _worker(db, repositories=repository).run_once(
        worker_id="worker-missing-report-checkpoint"
    )
    db.refresh(job)
    report = db.query(EngagementReport).one()

    assert result.status == "failed"
    assert job.status == "failed"
    assert job.completed_sections == []
    assert report.status == "failed"
    assert report.sections == []


@pytest.mark.asyncio
async def test_section_checkpoint_rolls_back_when_job_update_is_missing() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="missing-job-checkpoint")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    repository = _InjectedReportingRepository(db)
    job_service = _MissingSectionJobCheckpointService(
        db,
        repositories=repository,
    )
    db.commit()

    result = await _worker(
        db,
        repositories=repository,
        job_service=job_service,
    ).run_once(worker_id="worker-missing-job-checkpoint")
    db.refresh(job)
    report = db.query(EngagementReport).one()

    assert result.status == "failed"
    assert job.status == "failed"
    assert job.completed_sections == []
    assert report.status == "failed"
    assert report.sections == []


@pytest.mark.asyncio
async def test_requeue_rejection_commits_only_terminal_failure_state() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="requeue-rejected")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    repository = _RejectedRequeueRepository(db)
    generator = _FakeSectionGenerator(fail_section_id="executive_summary")
    db.commit()
    original_commit = db.commit
    committed_states: list[tuple[str, str | None]] = []

    def tracked_commit() -> None:
        if repository.requeue_attempted:
            report = db.query(EngagementReport).one_or_none()
            committed_states.append(
                (str(job.status), str(report.status) if report is not None else None)
            )
        original_commit()

    db.commit = tracked_commit  # type: ignore[method-assign]

    result = await _worker(
        db,
        generator=generator,
        repositories=repository,
    ).run_once(worker_id="worker-requeue-rejected")

    assert result.status == "failed"
    assert committed_states == [("failed", "failed")]


@pytest.mark.asyncio
async def test_finalization_retry_makes_no_additional_llm_calls() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="finalization-retry")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
    )
    db.commit()

    repository = _TransientReadyFailureRepository(db)
    generator = _FakeSectionGenerator()
    first = await _worker(
        db,
        generator=generator,
        repositories=repository,
    ).run_once(worker_id="worker-finalization-1")
    db.refresh(job)
    report = db.query(EngagementReport).one()
    first_call_count = len(generator.calls)

    assert first.status == "queued"
    assert job.generation_phase == "finalizing"
    assert len(job.completed_sections) == job.total_sections
    assert report.status == "generating"
    assert "finalization" in report.generation_metadata

    job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    second = await _worker(
        db,
        generator=generator,
        repositories=repository,
    ).run_once(worker_id="worker-finalization-2")
    db.refresh(job)
    db.refresh(report)

    assert second.status == "ready"
    assert len(generator.calls) == first_call_count
    assert report.status == "ready"


@pytest.mark.asyncio
async def test_worker_final_failure_keeps_previous_current_ready_report() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="final-failure")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    previous = _add_current_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        memo_id=str(memo.id),
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
        max_attempts=1,
    )
    db.commit()

    result = await _worker(
        db,
        generator=_FakeSectionGenerator(fail_section_id="executive_summary"),
    ).run_once(worker_id="worker-fail")

    db.refresh(job)
    db.refresh(previous)
    failed_report = (
        db.query(EngagementReport).filter(EngagementReport.id != previous.id).one()
    )

    assert result.status == "failed"
    assert job.status == "failed"
    assert job.finished_at is not None
    assert job.report_id == failed_report.id
    assert failed_report.status == "failed"
    assert failed_report.is_current is False
    assert failed_report.error_message == "LLM report section generation failed."
    assert previous.status == "ready"
    assert previous.is_current is True


@pytest.mark.asyncio
async def test_worker_validation_failure_persists_safe_section_diagnostics() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="validation-failure")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
        max_attempts=1,
    )
    db.commit()

    result = await _worker(
        db,
        validator=_FailingSectionValidator(),
    ).run_once(worker_id="worker-validation-fail")

    db.refresh(job)
    failed_report = db.query(EngagementReport).one()
    metadata = failed_report.generation_metadata

    assert result.status == "failed"
    assert job.status == "failed"
    assert job.last_error_code == REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED
    assert failed_report.error_message == "Generated report section failed validation."
    assert metadata["error_code"] == REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED
    assert metadata["failed_section_id"] == "executive_summary"
    assert metadata["failed_section_order"] == 1
    assert metadata["validation_issues"] == [
        {
            "code": "customer_markdown_internal_identifier",
            "path": "blocks.0.content_markdown",
        }
    ]
    assert "evidence_archive:secret" not in str(metadata)


@pytest.mark.asyncio
async def test_worker_stale_selected_memo_fails_without_promoting_report() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="stale-memo")
    memo = _add_memo(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
    )
    previous = _add_current_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        memo_id=str(memo.id),
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[str(memo.id)],
        max_attempts=1,
    )
    generator = _FakeSectionGenerator()
    db.commit()

    result = await _worker(
        db,
        generator=generator,
        source_watermarks=_FakeSourceWatermarks(stale=True),
    ).run_once(worker_id="worker-stale")

    db.refresh(job)
    db.refresh(previous)
    failed_report = (
        db.query(EngagementReport).filter(EngagementReport.id != previous.id).one()
    )

    assert result.status == "failed"
    assert job.status == "failed"
    assert job.report_id == failed_report.id
    assert job.last_error_code == REPORT_GENERATION_ERROR_STALE_MEMO
    assert job.error_message == "Selected task memo is stale."
    assert failed_report.status == "failed"
    assert failed_report.is_current is False
    assert previous.status == "ready"
    assert previous.is_current is True
    assert generator.calls == []


def test_terminal_report_failure_rollback_preserves_stale_recovery_path() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="terminal-failure-rollback")
    repository = _MissingTerminalReportRepository(db)
    report = repository.create_report_attempt(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        version=1,
        title="Generating report",
        source_task_memo_ids=[],
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        selected_task_memo_ids=[],
    )
    job.status = "generating"
    job.report_id = report.id
    job.locked_by = "stale-worker"
    job.locked_at = datetime.now(UTC) - timedelta(minutes=30)
    job.started_at = job.locked_at
    job.attempt_count = 1
    db.commit()

    worker = _worker(db, repositories=repository)
    failure = _ReportWorkerFailure(
        reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        safe_message="LLM report section generation failed.",
        retryable=False,
    )

    with pytest.raises(
        RuntimeError,
        match="^Report attempt failure state could not be persisted\\.$",
    ):
        worker._persist_failure(
            scope=_claimed_job_scope(job),
            report_id=report.id,
            failure=failure,
        )

    db.refresh(job)
    db.refresh(report)
    assert job.status == "generating"
    assert job.last_error_code is None
    assert job.error_message is None
    assert report.status == "generating"
    assert report.error_message is None
    assert report.generation_metadata == {}

    recovery = ReportJobService(
        db,
        report_repository=repository,
        worker_job_repository=repository,
    ).recover_stale_jobs(
        now=datetime.now(UTC),
        stale_after=timedelta(minutes=5),
        max_attempts=3,
    )

    db.refresh(job)
    db.refresh(report)
    assert recovery.requeued == 1
    assert recovery.failed == 0
    assert job.status == "queued"
    assert job.report_id == report.id
    assert report.status == "generating"
    assert report.error_message is None
    assert report.generation_metadata == {}


@pytest.mark.asyncio
async def test_worker_does_not_create_report_without_successful_claim() -> None:
    db = _build_session()

    result = await _worker(db).run_once(worker_id="worker-idle")

    assert result.claimed is False
    assert result.status == "idle"
    assert db.query(EngagementReport).count() == 0
    assert db.query(EngagementReportJob).count() == 0


def test_worker_module_does_not_import_runtime_docker_or_agent_paths() -> None:
    module_path = (
        Path(__file__).parents[3] / "services" / "reporting" / "report_worker.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
            imported_modules.add(node.module)

    assert not (imported_roots & {"agent", "docker", "kali_executor"})
    assert "backend.services.metrics.utils" not in imported_modules


def _worker(
    db: Session,
    *,
    generator: _FakeSectionGenerator | None = None,
    validator: Any | None = None,
    source_watermarks: _FakeSourceWatermarks | None = None,
    repositories: _InjectedReportingRepository | None = None,
    job_service: ReportJobService | None = None,
) -> ReportWorker:
    return ReportWorker(
        db,
        memo_repository=repositories,
        report_repository=repositories,
        request_job_repository=repositories,
        worker_job_repository=repositories,
        job_service=job_service,
        context_builder=_FakeContextBuilder(),  # type: ignore[arg-type]
        prompt_renderer=_FakePromptRenderer(),  # type: ignore[arg-type]
        section_generator=generator or _FakeSectionGenerator(),  # type: ignore[arg-type]
        section_validator=validator or _FakeSectionValidator(),  # type: ignore[arg-type]
        source_watermarks=source_watermarks or _FakeSourceWatermarks(),  # type: ignore[arg-type]
    )
