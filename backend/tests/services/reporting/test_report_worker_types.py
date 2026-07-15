"""Lock the canonical report worker envelope contracts after direct cutover."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any
from uuid import UUID

import pytest

from backend.services.reporting.report_worker_types import ReportWorkerRunResult
from backend.services.reporting.report_worker_types import (
    _ClaimedJobScope,
)


@pytest.mark.parametrize(
    ("envelope_type", "expected_fields", "values"),
    (
        (
            ReportWorkerRunResult,
            ("claimed", "job_id", "report_id", "status"),
            {
                "claimed": True,
                "job_id": UUID("11111111-1111-1111-1111-111111111111"),
                "report_id": UUID("22222222-2222-2222-2222-222222222222"),
                "status": "ready",
            },
        ),
        (
            _ClaimedJobScope,
            (
                "job_id",
                "tenant_id",
                "user_id",
                "requested_by_user_id",
                "engagement_id",
                "report_type",
                "selected_task_memo_ids",
                "include_candidate_findings",
                "report_id",
                "completed_section_ids",
                "llm_runtime_selection",
                "generation_phase",
            ),
            {
                "job_id": UUID("11111111-1111-1111-1111-111111111111"),
                "tenant_id": 1,
                "user_id": 2,
                "requested_by_user_id": 3,
                "engagement_id": 4,
                "report_type": "executive",
                "selected_task_memo_ids": ("memo-1",),
                "include_candidate_findings": True,
                "report_id": UUID("22222222-2222-2222-2222-222222222222"),
                "completed_section_ids": ("summary",),
                "llm_runtime_selection": {"provider": "test"},
                "generation_phase": "sections",
            },
        ),
    ),
)
def test_canonical_worker_dataclass_contract(
    envelope_type: type[object],
    expected_fields: tuple[str, ...],
    values: dict[str, Any],
) -> None:
    assert tuple(field.name for field in fields(envelope_type)) == expected_fields
    assert envelope_type.__dataclass_params__.frozen is True
    assert envelope_type.__slots__ == expected_fields
    assert asdict(envelope_type(**values)) == values
