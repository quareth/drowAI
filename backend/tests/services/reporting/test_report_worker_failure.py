"""Prove the canonical report-worker failure policy and production cutover."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError

from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_GENERATION_PHASE_FINALIZING,
    REPORT_GENERATION_PHASE_SECTIONS,
)
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
)
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
    ReportSectionValidationIssue,
)
from backend.services.reporting.report_worker import ReportWorker
from backend.services.reporting.report_worker_failure import (
    _ReportWorkerFailure,
    _ReportWorkerFailurePersistence,
    _is_expected_failure,
    _safe_failure,
)
from backend.services.reporting.report_worker_types import _ClaimedJobScope

_JOB_ID = UUID("11111111-1111-1111-1111-111111111111")
_REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")
_FIXED_NOW = datetime(2026, 7, 11, 6, 45, tzinfo=UTC)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz: object | None = None) -> datetime:
        return _FIXED_NOW


def _failure_snapshot(failure: Exception) -> dict[str, object]:
    return {
        "reason": getattr(failure, "reason"),
        "safe_message": getattr(failure, "safe_message"),
        "metadata": getattr(failure, "metadata"),
        "retryable": getattr(failure, "retryable"),
        "phase": getattr(failure, "phase"),
    }


def _classification_inputs() -> tuple[Exception, ...]:
    return (
        ReportSectionGenerationError(
            reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
            safe_message="LLM report section generation failed.",
            metadata={"provider": "test"},
            retryable=True,
        ),
        ReportSectionValidationError(
            issues=(
                ReportSectionValidationIssue(
                    code="invalid",
                    path="blocks.0",
                    message="Safe validation detail.",
                ),
            )
        ),
        OperationalError("UPDATE engagement_reports", {}, Exception("temporary")),
        ValueError("unsafe detail"),
    )


@pytest.mark.parametrize("phase", (REPORT_GENERATION_PHASE_SECTIONS, REPORT_GENERATION_PHASE_FINALIZING))
def test_canonical_failure_classification_is_safe(phase: str) -> None:
    for exc in _classification_inputs():
        failure = _safe_failure(exc, phase=phase)
        assert isinstance(failure, _ReportWorkerFailure)
        assert failure.phase == phase
        assert _is_expected_failure(exc) is not isinstance(exc, ValueError)

    canonical = _ReportWorkerFailure(
        reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        safe_message="safe",
        metadata={"key": "value"},
        retryable=True,
        phase=phase,
    )
    assert _safe_failure(canonical, phase=phase) is canonical
    assert _failure_snapshot(canonical) == {
        "reason": REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        "safe_message": "safe",
        "metadata": {"key": "value"},
        "retryable": True,
        "phase": phase,
    }
    assert _is_expected_failure(canonical) is True


@pytest.mark.parametrize(
    ("retryable", "phase"),
    (
        (True, REPORT_GENERATION_PHASE_SECTIONS),
        (False, REPORT_GENERATION_PHASE_FINALIZING),
    ),
)
def test_production_worker_uses_canonical_failure_persistence(
    monkeypatch: pytest.MonkeyPatch,
    retryable: bool,
    phase: str,
) -> None:
    import backend.services.reporting.report_worker_failure as failure_module

    monkeypatch.setattr(failure_module, "datetime", _FixedDatetime)

    production_worker, production_events = _build_worker(
        ReportWorker,
        retryable=retryable,
    )
    canonical_worker, canonical_events = _build_worker(
        _ReportWorkerFailurePersistence,
        retryable=retryable,
    )
    scope = _scope(phase=phase)
    failure = _ReportWorkerFailure(
        reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        safe_message="Safe failure.",
        metadata={"safe_key": "safe_value"},
        retryable=retryable,
        phase=phase,
    )

    production_result = production_worker._persist_failure(
        scope=scope,
        report_id=_REPORT_ID,
        failure=failure,
    )
    canonical_result = canonical_worker._persist_failure(
        scope=scope,
        report_id=_REPORT_ID,
        failure=failure,
    )

    assert production_result == canonical_result
    assert production_events.mock_calls == canonical_events.mock_calls


def test_linked_terminal_failure_rolls_back_when_report_update_is_missing() -> None:
    worker, _events = _build_worker(
        _ReportWorkerFailurePersistence,
        retryable=False,
    )
    worker._report_repository.mark_report_failed.return_value = None

    with pytest.raises(
        RuntimeError,
        match="^Report attempt failure state could not be persisted\\.$",
    ):
        worker._persist_failure(
            scope=_scope(phase=REPORT_GENERATION_PHASE_SECTIONS),
            report_id=_REPORT_ID,
            failure=_terminal_failure(),
        )

    worker._db.rollback.assert_called_once_with()
    worker._worker_repository.mark_report_job_failed_by_id.assert_not_called()
    worker._db.commit.assert_not_called()
    worker._diagnostics.job_failed.assert_not_called()


def test_successful_linked_terminal_failure_updates_both_records_once() -> None:
    worker, _events = _build_worker(
        _ReportWorkerFailurePersistence,
        retryable=False,
    )
    worker._report_repository.mark_report_failed.return_value = object()

    result = worker._persist_failure(
        scope=_scope(phase=REPORT_GENERATION_PHASE_SECTIONS),
        report_id=_REPORT_ID,
        failure=_terminal_failure(),
    )

    assert result.status == "failed"
    worker._report_repository.mark_report_failed.assert_called_once()
    worker._worker_repository.mark_report_job_failed_by_id.assert_called_once()
    worker._db.commit.assert_called_once_with()
    worker._db.rollback.assert_not_called()
    worker._diagnostics.job_failed.assert_called_once()


def test_reportless_terminal_failure_retains_job_only_transition() -> None:
    worker, _events = _build_worker(
        _ReportWorkerFailurePersistence,
        retryable=False,
    )

    result = worker._persist_failure(
        scope=_scope(phase=REPORT_GENERATION_PHASE_SECTIONS),
        report_id=None,
        failure=_terminal_failure(),
    )

    assert result.status == "failed"
    worker._report_repository.mark_report_failed.assert_not_called()
    worker._worker_repository.mark_report_job_failed_by_id.assert_called_once()
    worker._db.commit.assert_called_once_with()
    worker._diagnostics.job_failed.assert_called_once()


def test_terminal_job_update_failure_retains_existing_rollback_error() -> None:
    worker, _events = _build_worker(
        _ReportWorkerFailurePersistence,
        retryable=False,
    )
    worker._report_repository.mark_report_failed.return_value = object()
    worker._worker_repository.mark_report_job_failed_by_id.return_value = None

    with pytest.raises(
        RuntimeError,
        match="^Report failure state could not be persisted\\.$",
    ):
        worker._persist_failure(
            scope=_scope(phase=REPORT_GENERATION_PHASE_SECTIONS),
            report_id=_REPORT_ID,
            failure=_terminal_failure(),
        )

    worker._report_repository.mark_report_failed.assert_called_once()
    worker._worker_repository.mark_report_job_failed_by_id.assert_called_once()
    worker._db.rollback.assert_called_once_with()
    worker._db.commit.assert_not_called()
    worker._diagnostics.job_failed.assert_not_called()


def test_failure_cutover_has_one_definition_and_no_base_constructor() -> None:
    root = Path(__file__).parents[3] / "services" / "reporting"
    legacy_tree = ast.parse((root / "report_worker.py").read_text(encoding="utf-8"))
    extracted_tree = ast.parse(
        (root / "report_worker_failure.py").read_text(encoding="utf-8")
    )

    worker_class = _class_node(legacy_tree, "ReportWorker")
    assert [ast.unparse(base) for base in worker_class.bases] == [
        "_ReportWorkerAttemptExecution",
        "_ReportWorkerFailurePersistence",
    ]
    assert _class_node(extracted_tree, "_ReportWorkerFailure")
    assert _class_node(extracted_tree, "_ReportWorkerFailurePersistence")
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "_ReportWorkerFailure"
        for node in legacy_tree.body
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_safe_failure", "_is_expected_failure"}
        for node in legacy_tree.body
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_persist_failure"
        for node in worker_class.body
    )
    assert "__init__" not in _ReportWorkerFailurePersistence.__dict__


def _build_worker(
    worker_type: type[object],
    *,
    retryable: bool,
) -> tuple[object, MagicMock]:
    worker = worker_type.__new__(worker_type)
    events = MagicMock()
    db = MagicMock()
    worker_repository = MagicMock()
    report_repository = MagicMock()
    jobs = MagicMock()
    diagnostics = MagicMock()
    for name, collaborator in (
        ("db", db),
        ("worker_repository", worker_repository),
        ("report_repository", report_repository),
        ("jobs", jobs),
        ("diagnostics", diagnostics),
    ):
        events.attach_mock(collaborator, name)

    current_job = SimpleNamespace(attempt_count=1, max_attempts=3)
    worker_repository.get_report_job_by_id.return_value = current_job
    report_repository.merge_report_generation_metadata.return_value = object()
    jobs.requeue_after_failure.return_value = (
        SimpleNamespace(id=_JOB_ID, status="queued") if retryable else None
    )
    worker_repository.mark_report_job_failed_by_id.return_value = SimpleNamespace(
        attempt_count=1,
        max_attempts=3,
        status="failed",
    )
    worker._db = db
    worker._worker_repository = worker_repository
    worker._report_repository = report_repository
    worker._jobs = jobs
    worker._diagnostics = diagnostics
    events.reset_mock()
    return worker, events


def _scope(*, phase: str) -> _ClaimedJobScope:
    return _ClaimedJobScope(
        job_id=_JOB_ID,
        tenant_id=1,
        user_id=2,
        requested_by_user_id=2,
        engagement_id=3,
        report_type="pentest",
        selected_task_memo_ids=("memo-1",),
        include_candidate_findings=False,
        report_id=_REPORT_ID,
        completed_section_ids=(),
        llm_runtime_selection={},
        generation_phase=phase,
    )


def _terminal_failure() -> _ReportWorkerFailure:
    return _ReportWorkerFailure(
        reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        safe_message="Safe failure.",
        metadata={"safe_key": "safe_value"},
        retryable=False,
        phase=REPORT_GENERATION_PHASE_SECTIONS,
    )


def _class_node(tree: ast.AST, name: str) -> ast.ClassDef:
    return next(
        candidate
        for candidate in ast.walk(tree)
        if isinstance(candidate, ast.ClassDef) and candidate.name == name
    )
