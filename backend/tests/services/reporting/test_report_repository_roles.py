"""Verify explicit repository-role construction across the report worker path."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

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
from backend.services.reporting import report_worker as report_worker_module
from backend.services.reporting import report_worker_attempt as attempt_module
from backend.services.reporting.report_generation_service import (
    ReportGenerationService,
)
from backend.services.reporting.report_job_service import ReportJobService
from backend.services.reporting.report_worker import ReportWorker


class _FalseyRepository:
    """Repository test double that must be selected despite falsey truthiness."""

    def __bool__(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("constructor", "expected_roles"),
    [
        (
            ReportWorker.__init__,
            {
                "memo_repository",
                "report_repository",
                "request_job_repository",
                "worker_job_repository",
            },
        ),
        (
            ReportJobService.__init__,
            {"report_repository", "worker_job_repository"},
        ),
        (
            ReportGenerationService.__init__,
            {
                "memo_repository",
                "report_repository",
                "request_job_repository",
            },
        ),
    ],
)
def test_reporting_constructors_expose_only_explicit_repository_roles(
    constructor: object,
    expected_roles: set[str],
) -> None:
    parameters = inspect.signature(constructor).parameters

    assert "repository" not in parameters
    assert expected_roles <= set(parameters)
    assert all(parameters[name].default is None for name in expected_roles)


def test_generation_service_honors_falsey_explicit_repositories() -> None:
    memo_repository = _FalseyRepository()
    report_repository = _FalseyRepository()
    request_job_repository = _FalseyRepository()

    service = ReportGenerationService(
        object(),
        memo_repository=memo_repository,  # type: ignore[arg-type]
        report_repository=report_repository,  # type: ignore[arg-type]
        request_job_repository=request_job_repository,  # type: ignore[arg-type]
    )

    assert service._memo_repository is memo_repository
    assert service._report_repository is report_repository
    assert service._job_repository is request_job_repository


def test_job_service_honors_falsey_explicit_repositories() -> None:
    report_repository = _FalseyRepository()
    worker_job_repository = _FalseyRepository()

    service = ReportJobService(
        object(),
        report_repository=report_repository,  # type: ignore[arg-type]
        worker_job_repository=worker_job_repository,  # type: ignore[arg-type]
    )

    assert service._report_repository is report_repository
    assert service._worker_repository is worker_job_repository


def test_worker_honors_falsey_roles_and_routes_default_job_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memo_repository = _FalseyRepository()
    report_repository = _FalseyRepository()
    request_job_repository = _FalseyRepository()
    worker_job_repository = _FalseyRepository()
    captured: dict[str, object] = {}

    class _CapturingJobService:
        def __init__(self, db: object, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(report_worker_module, "ReportJobService", _CapturingJobService)

    worker = ReportWorker(
        object(),
        memo_repository=memo_repository,  # type: ignore[arg-type]
        report_repository=report_repository,  # type: ignore[arg-type]
        request_job_repository=request_job_repository,  # type: ignore[arg-type]
        worker_job_repository=worker_job_repository,  # type: ignore[arg-type]
    )

    assert worker._memo_repository is memo_repository
    assert worker._report_repository is report_repository
    assert worker._scoped_job_repository is request_job_repository
    assert worker._worker_repository is worker_job_repository
    assert captured["report_repository"] is report_repository
    assert captured["worker_job_repository"] is worker_job_repository


def test_worker_custom_job_service_remains_authoritative() -> None:
    custom_job_service = object()

    worker = ReportWorker(object(), job_service=custom_job_service)  # type: ignore[arg-type]

    assert worker._jobs is custom_job_service


def test_worker_routes_resolved_roles_to_memo_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memo_repository = _FalseyRepository()
    report_repository = _FalseyRepository()
    request_job_repository = _FalseyRepository()
    worker_job_repository = _FalseyRepository()
    source_watermarks = object()
    captured: dict[str, object] = {}

    class _CapturingGenerationService:
        def __init__(self, db: object, **kwargs: object) -> None:
            captured.update(kwargs)

        def validate_selected_current_ready_memos(self, **kwargs: object) -> list[str]:
            return ["selected-memo"]

    monkeypatch.setattr(
        attempt_module,
        "ReportGenerationService",
        _CapturingGenerationService,
    )
    worker = ReportWorker(
        object(),
        memo_repository=memo_repository,  # type: ignore[arg-type]
        report_repository=report_repository,  # type: ignore[arg-type]
        request_job_repository=request_job_repository,  # type: ignore[arg-type]
        worker_job_repository=worker_job_repository,  # type: ignore[arg-type]
        source_watermarks=source_watermarks,  # type: ignore[arg-type]
    )

    result = worker._validate_selected_memos(
        SimpleNamespace(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            selected_task_memo_ids=("memo-id",),
        )
    )

    assert result == ["selected-memo"]
    assert captured == {
        "memo_repository": memo_repository,
        "report_repository": report_repository,
        "request_job_repository": request_job_repository,
        "source_watermarks": source_watermarks,
    }


def test_default_construction_uses_existing_concrete_repository_classes() -> None:
    worker = ReportWorker(object())
    job_service = ReportJobService(object())
    generation_service = ReportGenerationService(object())

    assert isinstance(worker._memo_repository, TaskClosureMemoRepository)
    assert isinstance(worker._report_repository, EngagementReportRepository)
    assert isinstance(worker._scoped_job_repository, EngagementReportJobRepository)
    assert isinstance(worker._worker_repository, ReportJobWorkerRepository)
    assert isinstance(job_service._report_repository, EngagementReportRepository)
    assert isinstance(job_service._worker_repository, ReportJobWorkerRepository)
    assert isinstance(
        generation_service._memo_repository,
        TaskClosureMemoRepository,
    )
    assert isinstance(
        generation_service._report_repository,
        EngagementReportRepository,
    )
    assert isinstance(
        generation_service._job_repository,
        EngagementReportJobRepository,
    )
