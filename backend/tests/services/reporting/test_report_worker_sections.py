"""Verify canonical report-section execution through the inherited worker path."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_GENERATION_PHASE_SECTIONS,
)
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
)
from backend.services.reporting.report_section_plan import (
    ReportSectionPlan,
    ReportSectionPlanItem,
)
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
    ReportSectionValidationIssue,
)
from backend.services.reporting.report_worker import ReportWorker
from backend.services.reporting.report_worker_failure import _ReportWorkerFailure
from backend.services.reporting.report_worker_sections import (
    _ReportWorkerSectionExecution,
)
from backend.services.reporting.report_worker_types import _ClaimedJobScope

_JOB_ID = UUID("11111111-1111-1111-1111-111111111111")
_REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")


@pytest.mark.asyncio
async def test_inherited_section_success_matches_canonical_base() -> None:
    inherited, inherited_events = _build_worker(ReportWorker)
    canonical, canonical_events = _build_worker(_ReportWorkerSectionExecution)

    inherited_result = await inherited._generate_sections(
        scope=_scope(),
        report=_report(),
        context=_context(),
        section_plan=_plan(),
    )
    canonical_result = await canonical._generate_sections(
        scope=_scope(),
        report=_report(),
        context=_context(),
        section_plan=_plan(),
    )

    assert inherited_result == canonical_result
    assert inherited_events.mock_calls == canonical_events.mock_calls
    assert "__init__" not in _ReportWorkerSectionExecution.__dict__
    assert ReportWorker._generate_sections is _ReportWorkerSectionExecution._generate_sections
    assert "_generate_sections" not in ReportWorker.__dict__
    assert _method_definitions() == ["report_worker_sections.py"]


@pytest.mark.asyncio
async def test_inherited_section_checkpoint_resume_matches_canonical_base() -> None:
    first_section = _plan().sections[0]
    scope = _scope(completed_section_ids=(first_section.section_id,))
    report = _report(
        sections=[{"section_id": first_section.section_id, "status": "ready"}],
        section_metadata=[{"section_id": first_section.section_id}],
    )
    inherited, inherited_events = _build_worker(ReportWorker)
    canonical, canonical_events = _build_worker(_ReportWorkerSectionExecution)

    inherited_result = await inherited._generate_sections(
        scope=scope,
        report=report,
        context=_context(),
        section_plan=_plan(),
    )
    canonical_result = await canonical._generate_sections(
        scope=scope,
        report=report,
        context=_context(),
        section_plan=_plan(),
    )

    assert inherited_result == canonical_result
    assert inherited_events.mock_calls == canonical_events.mock_calls
    assert inherited._section_generator.generate.await_count == 1


@pytest.mark.asyncio
async def test_inherited_section_generation_failure_matches_canonical_base() -> None:
    failure = ReportSectionGenerationError(
        reason=REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
        safe_message="LLM report section generation failed.",
        metadata={"provider": "test"},
        retryable=True,
    )
    inherited, inherited_events = _build_worker(
        ReportWorker,
        generation_failure=failure,
    )
    canonical, canonical_events = _build_worker(
        _ReportWorkerSectionExecution,
        generation_failure=failure,
    )

    inherited_failure = await _captured_failure(inherited)
    canonical_failure = await _captured_failure(canonical)

    assert _failure_snapshot(inherited_failure) == _failure_snapshot(canonical_failure)
    assert inherited_events.mock_calls == canonical_events.mock_calls


@pytest.mark.asyncio
async def test_inherited_section_validation_failure_matches_canonical_base() -> None:
    failure = ReportSectionValidationError(
        issues=(
            ReportSectionValidationIssue(
                code="invalid",
                path="blocks.0",
                message="Safe validation detail.",
            ),
        )
    )
    inherited, inherited_events = _build_worker(
        ReportWorker,
        validation_failure=failure,
    )
    canonical, canonical_events = _build_worker(
        _ReportWorkerSectionExecution,
        validation_failure=failure,
    )

    inherited_failure = await _captured_failure(inherited)
    canonical_failure = await _captured_failure(canonical)

    assert _failure_snapshot(inherited_failure) == _failure_snapshot(canonical_failure)
    assert inherited_events.mock_calls == canonical_events.mock_calls


def _build_worker(
    worker_type: type[object],
    *,
    generation_failure: ReportSectionGenerationError | None = None,
    validation_failure: ReportSectionValidationError | None = None,
) -> tuple[Any, MagicMock]:
    worker = worker_type.__new__(worker_type)
    events = MagicMock()
    db = MagicMock()
    jobs = MagicMock()
    diagnostics = MagicMock()
    prompt_renderer = MagicMock()
    section_generator = MagicMock()
    section_validator = MagicMock()
    report_repository = MagicMock()
    for name, collaborator in (
        ("db", db),
        ("jobs", jobs),
        ("diagnostics", diagnostics),
        ("prompt_renderer", prompt_renderer),
        ("section_generator", section_generator),
        ("section_validator", section_validator),
        ("report_repository", report_repository),
    ):
        events.attach_mock(collaborator, name)

    jobs.mark_progress.return_value = object()
    prompt_renderer.render.side_effect = lambda **kwargs: SimpleNamespace(
        prompt="rendered",
        section=kwargs["section_plan_item"],
    )
    if generation_failure is not None:
        section_generator.generate = AsyncMock(side_effect=generation_failure)
    else:
        section_generator.generate = AsyncMock(side_effect=_generated_section)
    if validation_failure is not None:
        section_validator.validate.side_effect = validation_failure
    else:
        section_validator.validate.side_effect = _validated_section
    report_repository.update_report_sections.return_value = object()

    worker._db = db
    worker._jobs = jobs
    worker._diagnostics = diagnostics
    worker._prompt_renderer = prompt_renderer
    worker._section_generator = section_generator
    worker._section_validator = section_validator
    worker._report_repository = report_repository
    events.reset_mock()
    return worker, events


async def _generated_section(**kwargs: Any) -> SimpleNamespace:
    section = kwargs["rendered_prompt"].section
    return SimpleNamespace(
        payload={"section_id": section.section_id, "status": "ready"},
        metadata={"provider": "test", "section_id": section.section_id},
    )


def _validated_section(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        payload=kwargs["payload"],
        metadata={"validation_status": "passed"},
    )


async def _captured_failure(worker: Any) -> _ReportWorkerFailure:
    with pytest.raises(_ReportWorkerFailure) as captured:
        await worker._generate_sections(
            scope=_scope(),
            report=_report(),
            context=_context(),
            section_plan=_plan(),
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


def _scope(*, completed_section_ids: tuple[str, ...] = ()) -> _ClaimedJobScope:
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
        completed_section_ids=completed_section_ids,
        llm_runtime_selection={"provider": "test"},
        generation_phase=REPORT_GENERATION_PHASE_SECTIONS,
    )


def _report(
    *,
    sections: list[dict[str, Any]] | None = None,
    section_metadata: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=_REPORT_ID,
        sections=sections or [],
        generation_metadata={"sections": section_metadata or []},
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(candidate_policy=SimpleNamespace(enabled=False))


def _plan() -> ReportSectionPlan:
    return ReportSectionPlan(
        report_type="pentest",
        version="test.v1",
        sections=(
            ReportSectionPlanItem(
                section_id="summary",
                title="Summary",
                order=1,
                section_type="summary",
                prompt_purpose="Summarize.",
            ),
            ReportSectionPlanItem(
                section_id="limitations",
                title="Limitations",
                order=2,
                section_type="limitations",
                prompt_purpose="List limitations.",
            ),
        ),
    )


def _method_definitions() -> list[str]:
    root = Path(__file__).parents[3] / "services" / "reporting"
    definitions: list[str] = []
    for path in sorted(root.glob("report_worker*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_generate_sections"
            for node in ast.walk(tree)
        ):
            definitions.append(path.name)
    return definitions
