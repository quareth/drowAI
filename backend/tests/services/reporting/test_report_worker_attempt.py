"""Verify canonical report-attempt execution through the inherited worker path."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_PHASE_FINALIZING,
    REPORT_GENERATION_PHASE_SECTIONS,
)
from backend.services.reporting.report_generation_service import (
    ReportGenerationRequestError,
)
from backend.services.reporting.report_worker import ReportWorker
import backend.services.reporting.report_worker_attempt as attempt_module
from backend.services.reporting.report_worker_attempt import (
    _ReportWorkerAttemptExecution,
)
from backend.services.reporting.report_worker_failure import (
    _ReportWorkerFailure,
    _ReportWorkerFailurePersistence,
)
from backend.services.reporting.report_worker_sections import (
    _ReportWorkerSectionExecution,
)
from backend.services.reporting.report_worker_types import _ClaimedJobScope

_JOB_ID = UUID("11111111-1111-1111-1111-111111111111")
_REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")
_READY_REPORT_ID = UUID("33333333-3333-3333-3333-333333333333")
_READY_JOB_ID = UUID("44444444-4444-4444-4444-444444444444")
_NOW = datetime(2026, 7, 11, 7, 30, tzinfo=UTC)
_DEFAULT_READY_JOB = SimpleNamespace(
    id=_READY_JOB_ID,
    status="ready",
)


def test_inherited_attempt_creation_matches_canonical_base() -> None:
    inherited, inherited_events = _build_creation_worker(ReportWorker)
    canonical, canonical_events = _build_creation_worker(
        _ReportWorkerAttemptExecution
    )

    inherited_result = inherited._create_generating_report(_scope(report_id=None))
    canonical_result = canonical._create_generating_report(_scope(report_id=None))

    assert inherited_result == canonical_result
    assert inherited_events.mock_calls == canonical_events.mock_calls
    assert "__init__" not in _ReportWorkerAttemptExecution.__dict__
    assert _ReportWorkerAttemptExecution.__bases__ == (_ReportWorkerSectionExecution,)
    assert ReportWorker.__bases__ == (
        _ReportWorkerAttemptExecution,
        _ReportWorkerFailurePersistence,
    )
    assert (
        ReportWorker._create_generating_report
        is _ReportWorkerAttemptExecution._create_generating_report
    )
    assert "_create_generating_report" not in ReportWorker.__dict__
    assert attempt_module.logger.name == "backend.services.reporting.report_worker"
    assert _scoped_definitions() == {
        name: ["report_worker_attempt.py"]
        for name in _scoped_symbol_names()
    }


def test_inherited_attempt_resume_matches_canonical_base() -> None:
    existing = SimpleNamespace(id=_REPORT_ID, status="generating")
    legacy, legacy_events = _build_creation_worker(ReportWorker, existing=existing)
    extracted, extracted_events = _build_creation_worker(
        _ReportWorkerAttemptExecution,
        existing=existing,
    )

    legacy_result = legacy._create_generating_report(_scope())
    extracted_result = extracted._create_generating_report(_scope())

    assert legacy_result is existing
    assert extracted_result is existing
    assert legacy_events.mock_calls == extracted_events.mock_calls


@pytest.mark.asyncio
async def test_inherited_successful_finalization_matches_canonical_base() -> None:
    legacy, legacy_events = _build_generation_worker(ReportWorker)
    extracted, extracted_events = _build_generation_worker(
        _ReportWorkerAttemptExecution
    )

    with _patched_generation_globals():
        legacy_result = await legacy._generate_report(
            scope=_scope(generation_phase=REPORT_GENERATION_PHASE_SECTIONS),
            report=_report(),
        )
        extracted_result = await extracted._generate_report(
            scope=_scope(generation_phase=REPORT_GENERATION_PHASE_SECTIONS),
            report=_report(),
        )

    assert legacy_result == extracted_result
    assert legacy_events.mock_calls == extracted_events.mock_calls
    assert legacy._generate_sections.await_count == 1
    assert extracted._generate_sections.await_count == 1


@pytest.mark.asyncio
async def test_inherited_finalization_retry_matches_canonical_base() -> None:
    legacy, legacy_events = _build_generation_worker(ReportWorker)
    extracted, extracted_events = _build_generation_worker(
        _ReportWorkerAttemptExecution
    )

    with _patched_generation_globals():
        legacy_result = await legacy._generate_report(
            scope=_scope(generation_phase=REPORT_GENERATION_PHASE_FINALIZING),
            report=_report(),
        )
        extracted_result = await extracted._generate_report(
            scope=_scope(generation_phase=REPORT_GENERATION_PHASE_FINALIZING),
            report=_report(),
        )

    assert legacy_result == extracted_result
    assert legacy_events.mock_calls == extracted_events.mock_calls
    legacy._generate_sections.assert_not_awaited()
    extracted._generate_sections.assert_not_awaited()
    legacy._validate_selected_memos.assert_not_called()
    extracted._validate_selected_memos.assert_not_called()


def test_inherited_stale_memo_failure_matches_canonical_base() -> None:
    legacy = _bare_worker(ReportWorker)
    extracted = _bare_worker(_ReportWorkerAttemptExecution)
    legacy_service = _stale_memo_service()
    extracted_service = _stale_memo_service()

    with patch.object(
        attempt_module,
        "ReportGenerationService",
        legacy_service,
    ):
        legacy_failure = _captured_stale_memo_failure(legacy)
    with patch.object(
        attempt_module,
        "ReportGenerationService",
        extracted_service,
    ):
        extracted_failure = _captured_stale_memo_failure(extracted)

    assert _failure_snapshot(legacy_failure) == _failure_snapshot(extracted_failure)
    assert legacy_service.mock_calls == extracted_service.mock_calls


@pytest.mark.asyncio
async def test_inherited_ready_promotion_failure_matches_canonical_base() -> None:
    legacy, legacy_events = _build_generation_worker(
        ReportWorker,
        ready_job=None,
    )
    extracted, extracted_events = _build_generation_worker(
        _ReportWorkerAttemptExecution,
        ready_job=None,
    )

    with _patched_generation_globals():
        legacy_failure = await _captured_generation_failure(legacy)
        extracted_failure = await _captured_generation_failure(extracted)

    assert _failure_snapshot(legacy_failure) == _failure_snapshot(extracted_failure)
    assert legacy_failure.reason == REPORT_GENERATION_ERROR_PERSISTENCE_FAILED
    assert legacy_events.mock_calls == extracted_events.mock_calls
    legacy._db.commit.assert_not_called()
    extracted._db.commit.assert_not_called()


def _build_creation_worker(
    worker_type: type[object],
    *,
    existing: object | None = None,
) -> tuple[Any, MagicMock]:
    worker = worker_type.__new__(worker_type)
    events = MagicMock()
    report_repository = MagicMock()
    load_source_engagement = MagicMock(
        return_value=SimpleNamespace(name="Assessment", status="active")
    )
    events.attach_mock(report_repository, "report_repository")
    events.attach_mock(load_source_engagement, "load_source_engagement")
    report_repository.get_report_by_id.return_value = existing
    report_repository.next_report_version.return_value = 7
    report_repository.create_report_attempt.return_value = SimpleNamespace(
        id=_REPORT_ID,
        status="generating",
    )
    worker._report_repository = report_repository
    worker._load_source_engagement = load_source_engagement
    events.reset_mock()
    return worker, events


def _build_generation_worker(
    worker_type: type[object],
    *,
    ready_job: object | None = _DEFAULT_READY_JOB,
) -> tuple[Any, MagicMock]:
    worker = worker_type.__new__(worker_type)
    events = MagicMock()
    collaborators = {
        "db": MagicMock(),
        "diagnostics": MagicMock(),
        "report_repository": MagicMock(),
        "jobs": MagicMock(),
        "markdown_renderer": MagicMock(),
        "scoped_job_repository": MagicMock(),
        "validate_selected_memos": MagicMock(return_value=_SELECTED_MEMOS),
        "build_context": MagicMock(return_value=_CONTEXT),
        "generate_sections": AsyncMock(return_value=(_SECTIONS, _SECTION_METADATA)),
    }
    for name, collaborator in collaborators.items():
        events.attach_mock(collaborator, name)
        setattr(worker, f"_{name}", collaborator)
    worker._report_repository.update_report_sections.return_value = object()
    worker._jobs.mark_progress.return_value = object()
    worker._markdown_renderer.render.return_value = _RENDERED
    worker._report_repository.mark_report_ready.return_value = _READY_REPORT
    worker._scoped_job_repository.mark_report_job_ready.return_value = ready_job
    events.reset_mock()
    return worker, events


class _FrozenDateTime:
    @classmethod
    def now(cls, tz: object) -> datetime:
        assert tz is UTC
        return _NOW


class _PatchedGenerationGlobals:
    def __enter__(self) -> None:
        self._patches = [
            patch.object(
                attempt_module,
                "get_report_section_plan",
                return_value=_PLAN,
            ),
            patch.object(
                attempt_module,
                "build_finalization_checkpoint",
                return_value=_CHECKPOINT,
            ),
            patch.object(
                attempt_module,
                "load_finalization_checkpoint",
                return_value=_CHECKPOINT,
            ),
            patch.object(
                attempt_module,
                "checkpoint_generation_metadata",
                return_value=_CHECKPOINT_METADATA,
            ),
            patch.object(
                attempt_module,
                "final_generation_metadata",
                return_value=_FINAL_METADATA,
            ),
            patch.object(attempt_module, "datetime", _FrozenDateTime),
        ]
        for item in self._patches:
            item.start()

    def __exit__(self, *exc_info: object) -> None:
        for item in reversed(self._patches):
            item.stop()


def _patched_generation_globals() -> _PatchedGenerationGlobals:
    return _PatchedGenerationGlobals()


def _bare_worker(worker_type: type[object]) -> Any:
    worker = worker_type.__new__(worker_type)
    worker._db = _DB
    worker._memo_repository = _MEMO_REPOSITORY
    worker._report_repository = _REPORT_REPOSITORY
    worker._scoped_job_repository = _REQUEST_JOB_REPOSITORY
    worker._source_watermarks = _SOURCE_WATERMARKS
    return worker


def _stale_memo_service() -> MagicMock:
    service = MagicMock()
    service.return_value.validate_selected_current_ready_memos.side_effect = (
        ReportGenerationRequestError(
            reason=REPORT_GENERATION_ERROR_STALE_MEMO,
            safe_message="Selected task memo is stale.",
        )
    )
    return service


def _captured_stale_memo_failure(worker: Any) -> _ReportWorkerFailure:
    with pytest.raises(_ReportWorkerFailure) as captured:
        worker._validate_selected_memos(_scope())
    return captured.value


async def _captured_generation_failure(worker: Any) -> _ReportWorkerFailure:
    with pytest.raises(_ReportWorkerFailure) as captured:
        await worker._generate_report(
            scope=_scope(generation_phase=REPORT_GENERATION_PHASE_FINALIZING),
            report=_report(),
        )
    return captured.value


def _failure_snapshot(failure: _ReportWorkerFailure) -> dict[str, Any]:
    return {
        "reason": failure.reason,
        "safe_message": failure.safe_message,
        "metadata": failure.metadata,
        "retryable": failure.retryable,
        "phase": failure.phase,
    }


def _scope(
    *,
    report_id: UUID | None = _REPORT_ID,
    generation_phase: str = REPORT_GENERATION_PHASE_SECTIONS,
) -> _ClaimedJobScope:
    return _ClaimedJobScope(
        job_id=_JOB_ID,
        tenant_id=1,
        user_id=2,
        requested_by_user_id=2,
        engagement_id=3,
        report_type="pentest",
        selected_task_memo_ids=("memo-1",),
        include_candidate_findings=False,
        report_id=report_id,
        completed_section_ids=(),
        llm_runtime_selection={"provider": "test"},
        generation_phase=generation_phase,
    )


def _report() -> SimpleNamespace:
    return SimpleNamespace(
        id=_REPORT_ID,
        title="Pentest Report",
        generation_metadata={"finalization": {}},
    )


def _scoped_definitions() -> dict[str, list[str]]:
    root = _worker_path().parent
    definitions = {name: [] for name in _scoped_symbol_names()}
    for path in sorted(root.glob("report_worker*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in definitions:
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
                for node in ast.walk(tree)
            ):
                definitions[name].append(path.name)
    return definitions


def _scoped_symbol_names() -> tuple[str, ...]:
    return (
        "_create_generating_report",
        "_load_source_engagement",
        "_generate_report",
        "_validate_selected_memos",
        "_build_context",
        "_report_title",
    )


def _worker_path() -> Path:
    return Path(__file__).parents[3] / "services" / "reporting" / "report_worker.py"


_PLAN = SimpleNamespace(sections=(SimpleNamespace(section_id="summary"),))
_SELECTED_MEMOS = (SimpleNamespace(id="memo-1"),)
_CONTEXT = SimpleNamespace(name="context")
_SECTIONS = [{"section_id": "summary", "status": "ready"}]
_SECTION_METADATA = [{"section_id": "summary"}]
_CHECKPOINT = SimpleNamespace(
    sections=tuple(_SECTIONS),
    evidence_timeline=(),
    source_task_memo_ids=("memo-1",),
    source_knowledge_refs=(),
    source_evidence_refs=(),
)
_CHECKPOINT_METADATA = {"finalization": {"state": "checkpointed"}}
_FINAL_METADATA = {"finalization": {"state": "ready"}}
_RENDERED = SimpleNamespace(
    markdown_snapshot="# Pentest Report",
    generation_metadata={"renderer": "test"},
)
_READY_REPORT = SimpleNamespace(id=_READY_REPORT_ID, status="ready")
_DB = object()
_MEMO_REPOSITORY = object()
_REPORT_REPOSITORY = object()
_REQUEST_JOB_REPOSITORY = object()
_SOURCE_WATERMARKS = object()
