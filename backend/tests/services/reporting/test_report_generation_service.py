"""Tests for engagement report generation request validation policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import ChatMessage
from backend.models.core import Engagement, Task, User
from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.models.tenant import Tenant
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionMissingError,
)
from backend.services.llm_provider.types import LLMCredentialRef, LLMRuntimeSelection
from backend.services.reporting.contracts import (
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
    REPORT_GENERATION_ERROR_INVALID_REQUEST,
    REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
)
from backend.services.reporting.report_generation_service import (
    ReportGenerationRequestError,
    ReportGenerationService,
)
from backend.services.reporting.source_watermark_service import (
    ReportSourceMemoWatermarkInput,
    SourceWatermarkService,
)


class _FakeRepository:
    def __init__(self, selected_memos=None, selected_tasks=None) -> None:
        self._selected_memos = list(selected_memos or [])
        self._selected_tasks = list(selected_tasks or [])
        self.selected_memo_lookup_calls: list[dict] = []
        self.selected_task_lookup_calls: list[dict] = []

    def normalize_selected_memo_ids(self, selected_task_memo_ids):
        return ReportingRepositoryBase.normalize_selected_memo_ids(
            selected_task_memo_ids
        )

    def list_selected_current_ready_memos(self, **kwargs):
        self.selected_memo_lookup_calls.append(dict(kwargs))
        return list(self._selected_memos)

    def get_selected_memo_tasks(self, **kwargs):
        self.selected_task_lookup_calls.append(dict(kwargs))
        return list(self._selected_tasks)


class _FakeWatermarks:
    def compute_for_task(self, **kwargs):
        return {"schema_version": 1, "sources": {"task_id": kwargs["task_id"]}}


class _FakeReportingSelectionService:
    def __init__(
        self,
        *,
        provider: str = "openai",
        model: str = "gpt-report",
        reasoning_effort: str | None = None,
        missing: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.missing = missing
        self.calls: list[int] = []

    def build_runtime_selection(self, *, user_id: int) -> LLMRuntimeSelection:
        self.calls.append(user_id)
        if self.missing:
            raise ReportingLLMSelectionMissingError(
                "Reporting model is not configured."
            )
        return LLMRuntimeSelection(
            provider=self.provider,
            model=self.model,
            credential_ref=LLMCredentialRef(
                user_id=user_id,
                provider=self.provider,
            ),
            reasoning_effort=self.reasoning_effort,
        )


def _runtime_selection_payload(
    *,
    user_id: int,
    provider: str = "openai",
    model: str = "gpt-report",
    reasoning_effort: str | None = None,
) -> dict:
    return {
        "provider": provider,
        "model": model,
        "credential_ref": {"user_id": user_id, "provider": provider},
        "reasoning_effort": reasoning_effort,
    }


def _report_generation_service(
    db: Session,
    *,
    reporting_selection_service: _FakeReportingSelectionService | None = None,
) -> ReportGenerationService:
    return ReportGenerationService(
        db,
        reporting_selection_service=(
            reporting_selection_service or _FakeReportingSelectionService()
        ),  # type: ignore[arg-type]
    )


def _build_session() -> Session:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return factory()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement, Task]:
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

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
        status=TaskStatus.STOPPED.value,
    )
    db.add(task)
    db.flush()
    return tenant, user, engagement, task


def _add_current_ready_memo(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    source_watermark: dict,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=task.tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        version=1,
        is_current=True,
        status="ready",
        memo_mode="supported",
        source_watermark=source_watermark,
        memo={"summary": "ready memo"},
    )
    db.add(memo)
    db.flush()
    return memo


def _add_chat_message(db: Session, *, task: Task) -> ChatMessage:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    message = ChatMessage(
        tenant_id=task.tenant_id,
        task_id=task.id,
        conversation_id=f"conversation-{task.id}",
        turn_number=1,
        message_type="assistant",
        message="new task source",
        created_at=now,
        updated_at=now,
    )
    db.add(message)
    db.flush()
    return message


def test_duplicate_selected_memo_ids_are_rejected_before_lookup() -> None:
    memo_id = uuid.uuid4()
    repository = _FakeRepository()
    service = ReportGenerationService(object(), memo_repository=repository)

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.validate_selected_current_ready_memos(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            selected_task_memo_ids=[memo_id, str(memo_id)],
        )

    assert exc_info.value.reason == (
        REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS
    )
    assert exc_info.value.safe_message == "Selected task memo IDs must be unique."
    assert repository.selected_memo_lookup_calls == []


def test_unique_selected_memo_ids_reach_repository_validation_path() -> None:
    first_memo_id = uuid.uuid4()
    second_memo_id = uuid.uuid4()
    expected_memos = [
        SimpleNamespace(
            id=first_memo_id,
            task_id=4,
            memo_mode="supported",
            source_watermark={"schema_version": 1, "sources": {"task_id": 4}},
        ),
        SimpleNamespace(
            id=second_memo_id,
            task_id=5,
            memo_mode="limited",
            source_watermark={"schema_version": 1, "sources": {"task_id": 5}},
        ),
    ]
    repository = _FakeRepository(
        expected_memos,
        selected_tasks=[
            (first_memo_id, SimpleNamespace(id=4, status=TaskStatus.STOPPED.value)),
            (second_memo_id, SimpleNamespace(id=5, status=TaskStatus.STOPPED.value)),
        ],
    )
    service = ReportGenerationService(
        object(),
        memo_repository=repository,
        source_watermarks=_FakeWatermarks(),
    )

    selected_memos = service.validate_selected_current_ready_memos(
        tenant_id=1,
        user_id=2,
        engagement_id=3,
        selected_task_memo_ids=[str(first_memo_id), second_memo_id],
    )

    assert selected_memos == expected_memos
    assert repository.selected_memo_lookup_calls == [
        {
            "tenant_id": 1,
            "user_id": 2,
            "engagement_id": 3,
            "selected_task_memo_ids": [first_memo_id, second_memo_id],
        }
    ]
    assert repository.selected_task_lookup_calls == [
        {
            "tenant_id": 1,
            "user_id": 2,
            "engagement_id": 3,
            "selected_task_memo_ids": [first_memo_id, second_memo_id],
        }
    ]


def test_missing_or_foreign_selected_memo_is_rejected_without_id_detail() -> None:
    requested_memo_id = uuid.uuid4()
    repository = _FakeRepository()
    service = ReportGenerationService(
        object(),
        memo_repository=repository,
        source_watermarks=_FakeWatermarks(),
    )

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.validate_selected_current_ready_memos(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            selected_task_memo_ids=[requested_memo_id],
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_INVALID_REQUEST
    assert exc_info.value.safe_message == (
        "Selected task memo IDs must reference eligible memos."
    )
    assert str(requested_memo_id) not in exc_info.value.safe_message


def test_selected_memo_task_must_belong_to_stopped_engagement_task() -> None:
    memo_id = uuid.uuid4()
    repository = _FakeRepository(
        selected_memos=[
            SimpleNamespace(
                id=memo_id,
                task_id=4,
                memo_mode="supported",
                source_watermark={"schema_version": 1, "sources": {"task_id": 4}},
            )
        ],
        selected_tasks=[],
    )
    service = ReportGenerationService(
        object(),
        memo_repository=repository,
        source_watermarks=_FakeWatermarks(),
    )

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.validate_selected_current_ready_memos(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            selected_task_memo_ids=[memo_id],
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_INVALID_REQUEST
    assert exc_info.value.safe_message == (
        "Selected task memo IDs must reference eligible memos."
    )


def test_limited_only_selected_memos_are_rejected_before_job_creation() -> None:
    memo_id = uuid.uuid4()
    repository = _FakeRepository(
        selected_memos=[
            SimpleNamespace(
                id=memo_id,
                task_id=4,
                memo_mode="limited",
                source_watermark={"schema_version": 1, "sources": {"task_id": 4}},
            )
        ],
        selected_tasks=[
            (memo_id, SimpleNamespace(id=4, status=TaskStatus.STOPPED.value))
        ],
    )
    service = ReportGenerationService(
        object(),
        memo_repository=repository,
        source_watermarks=_FakeWatermarks(),
    )

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.validate_selected_current_ready_memos(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            selected_task_memo_ids=[memo_id],
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX
    assert exc_info.value.safe_message == (
        "At least one selected task memo must support report generation."
    )


def test_stale_current_ready_selected_memo_is_rejected_before_job_creation() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="stale-report-generation")
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    _add_chat_message(db, task=task)
    db.commit()

    service = _report_generation_service(db)

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.validate_selected_current_ready_memos(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            selected_task_memo_ids=[memo.id],
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_STALE_MEMO
    assert exc_info.value.safe_message == "Selected task memo is stale."


def test_generation_request_creates_queued_job_with_source_metadata() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="queued-report-job")
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    result = _report_generation_service(db).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
        include_candidate_findings=True,
    )

    job = db.query(EngagementReportJob).one()
    assert result.job_id == job.id
    assert result.report_id is None
    assert result.status == "queued"
    assert job.status == "queued"
    assert job.report_type == "pentest"
    assert job.selected_task_memo_ids == [str(memo.id)]
    assert job.include_candidate_findings is True
    assert job.source_watermark["hash"]
    assert job.source_watermark["selected_memos"][0]["memo_id"] == str(memo.id)
    assert job.source_watermark["idempotency"]["key"] == job.idempotency_key
    assert job.source_watermark["idempotency"]["requested_by_user_id"] == user.id
    assert str(job.source_watermark["hash"]) in job.idempotency_key
    assert job.llm_runtime_selection == _runtime_selection_payload(user_id=user.id)
    assert job.source_watermark["llm_runtime_selection"] == job.llm_runtime_selection
    assert job.total_sections > 0


def test_generation_request_rejects_missing_reporting_model() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db, label="missing-reporting-model"
    )
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    service = _report_generation_service(
        db,
        reporting_selection_service=_FakeReportingSelectionService(missing=True),
    )

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.request_generation(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_task_memo_ids=[memo.id],
            engagement_is_owned=True,
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE
    assert exc_info.value.safe_message == "Reporting model is not configured."
    assert db.query(EngagementReportJob).count() == 0


def test_duplicate_matching_request_reuses_active_job() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="active-job-reuse")
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    service = _report_generation_service(db)
    first = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
    )
    second = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[str(memo.id)],
        engagement_is_owned=True,
    )

    assert second == first
    assert db.query(EngagementReportJob).count() == 1


def test_matching_request_with_different_reporting_model_creates_separate_job() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db, label="report-model-idempotency"
    )
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    first = _report_generation_service(
        db,
        reporting_selection_service=_FakeReportingSelectionService(
            provider="openai",
            model="gpt-report",
            reasoning_effort="medium",
        ),
    ).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
    )
    second = _report_generation_service(
        db,
        reporting_selection_service=_FakeReportingSelectionService(
            provider="anthropic",
            model="claude-haiku-report",
        ),
    ).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[str(memo.id)],
        engagement_is_owned=True,
    )

    assert first.job_id != second.job_id
    jobs = db.query(EngagementReportJob).order_by(EngagementReportJob.created_at).all()
    assert [job.llm_runtime_selection for job in jobs] == [
        _runtime_selection_payload(
            user_id=user.id,
            provider="openai",
            model="gpt-report",
            reasoning_effort="medium",
        ),
        _runtime_selection_payload(
            user_id=user.id,
            provider="anthropic",
            model="claude-haiku-report",
        ),
    ]


def test_matching_request_with_different_requester_creates_separate_job() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db, label="requester-idempotency"
    )
    requester = User(
        username=f"requester-{uuid.uuid4().hex[:8]}",
        password="hashed",
    )
    db.add(requester)
    db.flush()
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    service = _report_generation_service(db)
    owner_request = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
    )
    delegated_request = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=requester.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[str(memo.id)],
        engagement_is_owned=True,
    )
    delegated_duplicate = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=requester.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[str(memo.id)],
        engagement_is_owned=True,
    )

    owner_job = (
        db.query(EngagementReportJob)
        .filter_by(id=owner_request.job_id)
        .one()
    )
    delegated_job = (
        db.query(EngagementReportJob)
        .filter_by(id=delegated_request.job_id)
        .one()
    )
    assert delegated_request.job_id != owner_request.job_id
    assert delegated_duplicate == delegated_request
    assert owner_job.idempotency_key != delegated_job.idempotency_key
    assert (
        owner_job.source_watermark["idempotency"]["requested_by_user_id"]
        == user.id
    )
    assert (
        delegated_job.source_watermark["idempotency"]["requested_by_user_id"]
        == requester.id
    )
    assert db.query(EngagementReportJob).count() == 2


def test_matching_request_after_failed_job_creates_new_queued_job() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="failed-job-retry")
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    db.commit()

    service = _report_generation_service(db)
    first = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
    )
    first_job = db.query(EngagementReportJob).filter_by(id=first.job_id).one()
    first_job.status = "failed"
    db.commit()

    second = service.request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[str(memo.id)],
        engagement_is_owned=True,
    )

    second_job = db.query(EngagementReportJob).filter_by(id=second.job_id).one()
    assert second.status == "queued"
    assert second.job_id != first.job_id
    assert second.report_id is None
    assert second_job.status == "queued"
    assert second_job.idempotency_key != first_job.idempotency_key
    assert (
        second_job.source_watermark["idempotency"]["original_key"]
        == first_job.idempotency_key
    )
    assert db.query(EngagementReportJob).count() == 2


def test_matching_current_ready_report_is_returned_without_job_creation() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="current-report-reuse")
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    report_watermark = SourceWatermarkService(db).compute_for_report(
        report_type="pentest",
        selected_memos=[
            ReportSourceMemoWatermarkInput(
                memo_id=str(memo.id),
                version=memo.version,
                source_watermark=memo.source_watermark,
            )
        ],
        include_candidate_findings=False,
    )
    report = EngagementReport(
        tenant_id=tenant.id,
        user_id=user.id,
        created_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        version=1,
        status="ready",
        is_current=True,
        title="Current Report",
        sections=[],
        markdown_snapshot="# Current Report",
        source_task_memo_ids=[str(memo.id)],
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata={
            GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: report_watermark["hash"],
            "llm_runtime_selection": _runtime_selection_payload(user_id=user.id),
        },
    )
    db.add(report)
    db.commit()

    result = _report_generation_service(db).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
    )

    assert result.job_id is None
    assert result.report_id == report.id
    assert result.status == "ready"
    assert db.query(EngagementReportJob).count() == 0


def test_force_regenerate_matching_current_ready_report_creates_queued_job() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(
        db, label="current-report-regenerate"
    )
    stored_watermark = SourceWatermarkService(db).compute_for_task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
    )
    memo = _add_current_ready_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        source_watermark=stored_watermark,
    )
    report_watermark = SourceWatermarkService(db).compute_for_report(
        report_type="pentest",
        selected_memos=[
            ReportSourceMemoWatermarkInput(
                memo_id=str(memo.id),
                version=memo.version,
                source_watermark=memo.source_watermark,
            )
        ],
        include_candidate_findings=False,
    )
    db.add(
        EngagementReport(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            title="Current Report",
            sections=[],
            markdown_snapshot="# Current Report",
            source_task_memo_ids=[str(memo.id)],
            source_knowledge_refs=[],
            source_evidence_refs=[],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: report_watermark["hash"]
            },
        )
    )
    db.commit()

    result = _report_generation_service(db).request_generation(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
        selected_task_memo_ids=[memo.id],
        engagement_is_owned=True,
        force_regenerate=True,
    )

    job = db.query(EngagementReportJob).filter_by(id=result.job_id).one()
    assert result.status == "queued"
    assert result.report_id is None
    assert job.selected_task_memo_ids == [str(memo.id)]
    assert db.query(EngagementReportJob).count() == 1


def test_generation_request_rejects_task_id_based_input() -> None:
    service = ReportGenerationService(object(), memo_repository=_FakeRepository())

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.request_generation(
            tenant_id=1,
            user_id=2,
            requested_by_user_id=2,
            engagement_id=3,
            report_type="pentest",
            selected_task_memo_ids=[],
            engagement_is_owned=True,
            task_ids=[4],
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_INVALID_REQUEST
    assert exc_info.value.safe_message == (
        "Report generation requires selected task memo IDs."
    )


def test_generation_request_rejects_unowned_engagement_before_lookup() -> None:
    repository = _FakeRepository()
    service = ReportGenerationService(object(), memo_repository=repository)

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.request_generation(
            tenant_id=1,
            user_id=2,
            requested_by_user_id=2,
            engagement_id=3,
            report_type="pentest",
            selected_task_memo_ids=[uuid.uuid4()],
            engagement_is_owned=False,
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_INVALID_REQUEST
    assert exc_info.value.safe_message == (
        "Engagement is not available for report generation."
    )
    assert repository.selected_memo_lookup_calls == []


def test_generation_request_rejects_unsupported_report_type_before_lookup() -> None:
    repository = _FakeRepository()
    service = ReportGenerationService(object(), memo_repository=repository)

    with pytest.raises(ReportGenerationRequestError) as exc_info:
        service.request_generation(
            tenant_id=1,
            user_id=2,
            requested_by_user_id=2,
            engagement_id=3,
            report_type="executive_brief",
            selected_task_memo_ids=[uuid.uuid4()],
            engagement_is_owned=True,
        )

    assert exc_info.value.reason == REPORT_GENERATION_ERROR_INVALID_REQUEST
    assert exc_info.value.safe_message == "Report type is not supported."
    assert repository.selected_memo_lookup_calls == []
