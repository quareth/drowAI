"""Define report worker result and claimed-job state envelopes.

This module is limited to side-effect-free worker dataclasses and does not own
job orchestration, persistence, generation, or failure handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ReportWorkerRunResult:
    """Outcome from one worker claim attempt."""

    claimed: bool
    job_id: UUID | None
    report_id: UUID | None
    status: str


@dataclass(frozen=True, slots=True)
class _ClaimedJobScope:
    job_id: UUID
    tenant_id: int
    user_id: int
    requested_by_user_id: int
    engagement_id: int
    report_type: str
    selected_task_memo_ids: tuple[str, ...]
    include_candidate_findings: bool
    report_id: UUID | None
    completed_section_ids: tuple[str, ...]
    llm_runtime_selection: Mapping[str, Any]
    generation_phase: str
