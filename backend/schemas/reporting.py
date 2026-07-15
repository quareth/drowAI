"""Pydantic wire contracts for engagement reporting API requests and responses.

This module defines request/response shapes used by the reporting API surface.
It intentionally contains no database, query, generation, worker, or routing
logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from backend.services.reporting.contracts import (
    InputState,
    MemoMode,
    MemoStatus,
    ReportGenerationPhase,
    ReportJobStatus,
    ReportStatus,
    ReportType,
    ReportingReasonCode,
    validate_input_state,
    validate_memo_mode,
    validate_memo_status,
    validate_report_generation_phase,
    validate_report_job_status,
    validate_report_status,
    validate_report_type,
    validate_reporting_reason_code,
)


def _ensure_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _report_type(value: Any) -> ReportType:
    return validate_report_type(_ensure_string(value, "report_type"))


def _memo_status(value: Any) -> MemoStatus:
    return validate_memo_status(_ensure_string(value, "memo_status"))


def _report_status(value: Any) -> ReportStatus:
    return validate_report_status(_ensure_string(value, "report_status"))


def _report_job_status(value: Any) -> ReportJobStatus:
    return validate_report_job_status(_ensure_string(value, "report_job_status"))


def _report_generation_phase(value: Any) -> ReportGenerationPhase:
    return validate_report_generation_phase(
        _ensure_string(value, "report_generation_phase")
    )


def _memo_mode(value: Any) -> MemoMode:
    return validate_memo_mode(_ensure_string(value, "memo_mode"))


def _input_state(value: Any) -> InputState:
    return validate_input_state(_ensure_string(value, "input_state"))


def _reason_code(value: Any) -> ReportingReasonCode:
    return validate_reporting_reason_code(_ensure_string(value, "reason_code"))


def _source_ref(value: Any) -> str:
    ref = _ensure_string(value, "source_ref").strip()
    if not ref:
        raise ValueError("source_ref must not be empty")
    return ref


def _memo_body_or_none(value: Any) -> Any:
    if value is None or value == {}:
        return None
    return value


ReportTypeValue = Annotated[ReportType, BeforeValidator(_report_type)]
MemoStatusValue = Annotated[MemoStatus, BeforeValidator(_memo_status)]
ReportStatusValue = Annotated[ReportStatus, BeforeValidator(_report_status)]
ReportJobStatusValue = Annotated[ReportJobStatus, BeforeValidator(_report_job_status)]
ReportGenerationPhaseValue = Annotated[
    ReportGenerationPhase,
    BeforeValidator(_report_generation_phase),
]
MemoModeValue = Annotated[MemoMode, BeforeValidator(_memo_mode)]
InputStateValue = Annotated[InputState, BeforeValidator(_input_state)]
ReportingReasonCodeValue = Annotated[
    ReportingReasonCode,
    BeforeValidator(_reason_code),
]
SourceReference = Annotated[str, BeforeValidator(_source_ref), Field(max_length=256)]
MemoBodyValue = Annotated[
    "TaskClosureMemoBody | None", BeforeValidator(_memo_body_or_none)
]
MemoTextSource = Literal["transcript", "evidence", "knowledge"]
MemoConfidence = Literal["low", "medium", "high"]
MemoSeverityHint = Literal["informational", "low", "medium", "high", "critical"]
ReportSectionStatus = Literal["ready", "needs_review", "failed"]
ReportSectionType = Literal[
    "narrative",
    "summary",
    "findings",
    "recommendations",
    "limitations",
    "appendix",
]
ReportSectionBlockType = Literal[
    "finding",
    "evidence_note",
    "asset_note",
    "appendix_note",
]


class _ReportingSchema(BaseModel):
    """Base model configuration for reporting API schemas."""

    model_config = ConfigDict(from_attributes=True)


class SourceWatermarkSnapshot(_ReportingSchema):
    """Task-local source freshness markers used for memo staleness checks."""

    last_chat_message_id: int | None = None
    last_turn_sequence: int | None = None
    latest_tool_execution_id: str | None = None
    latest_evidence_created_at: datetime | None = None
    latest_knowledge_observed_at: datetime | None = None


class ReportingSourceCounts(_ReportingSchema):
    """Task-local reporting source counts shown in input inventory rows."""

    evidence: int = Field(default=0, ge=0)
    canonical_findings: int = Field(default=0, ge=0)
    candidate_findings: int = Field(default=0, ge=0)


class TaskClosureMemoIncludeInReportRecommendation(_ReportingSchema):
    """Recommendation for whether this task memo should feed a report."""

    include: bool
    reason: str = Field(min_length=1, max_length=2000)


class TaskClosureMemoActionItem(_ReportingSchema):
    """Bounded action performed during the task."""

    text: str = Field(min_length=1, max_length=4000)
    source: MemoTextSource = "transcript"


class _SourceBackedMemoItem(_ReportingSchema):
    """Base contract for memo items that must cite supplied source refs."""

    evidence_refs: list[SourceReference] = Field(min_length=0, max_length=100)
    knowledge_refs: list[SourceReference] = Field(min_length=0, max_length=100)

    @model_validator(mode="after")
    def _require_source_refs(self) -> "_SourceBackedMemoItem":
        if not self.evidence_refs and not self.knowledge_refs:
            raise ValueError(
                "reportable memo items require evidence_refs or knowledge_refs"
            )
        return self


class TaskClosureMemoReportableObservationItem(_SourceBackedMemoItem):
    """Source-backed observation that may be considered for reporting."""

    text: str = Field(min_length=1, max_length=4000)
    confidence: MemoConfidence


class TaskClosureMemoPossibleFindingItem(_SourceBackedMemoItem):
    """Source-backed possible finding candidate from one task memo."""

    title: str = Field(min_length=1, max_length=512)
    severity_hint: MemoSeverityHint | None = None
    confidence: MemoConfidence
    description: str | None = Field(default=None, max_length=4000)


class TaskClosureMemoLimitationItem(_ReportingSchema):
    """Bounded limitation for this task memo."""

    text: str = Field(min_length=1, max_length=2000)


class TaskClosureMemoUnsupportedNoteItem(_ReportingSchema):
    """Bounded note that is not supported by reportable source refs."""

    text: str = Field(min_length=1, max_length=2000)


class TaskClosureMemoBody(_ReportingSchema):
    """Structured task closure memo body safe for API responses."""

    task_name: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=4000)
    include_in_report_recommendation: TaskClosureMemoIncludeInReportRecommendation
    actions_performed: list[TaskClosureMemoActionItem] = Field(
        default_factory=list, max_length=100
    )
    reportable_observations: list[TaskClosureMemoReportableObservationItem] = Field(
        default_factory=list,
        max_length=100,
    )
    possible_findings: list[TaskClosureMemoPossibleFindingItem] = Field(
        default_factory=list,
        max_length=100,
    )
    limitations: list[TaskClosureMemoLimitationItem] = Field(
        default_factory=list, max_length=100
    )
    unsupported_notes: list[TaskClosureMemoUnsupportedNoteItem] = Field(
        default_factory=list, max_length=100
    )
    evidence_refs: list[SourceReference] = Field(default_factory=list, max_length=500)
    knowledge_refs: list[SourceReference] = Field(default_factory=list, max_length=500)


class TaskClosureMemoSummary(_ReportingSchema):
    """Current or latest task closure memo summary for an inventory row."""

    id: UUID
    version: int = Field(ge=1)
    status: MemoStatusValue
    memo_mode: MemoModeValue
    is_current: bool
    source_watermark: SourceWatermarkSnapshot
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None = None


class TaskClosureMemoAttemptSummary(_ReportingSchema):
    """Attempt metadata for one task closure memo version."""

    id: UUID
    schema_version: str
    engagement_id: int
    task_id: int
    version: int = Field(ge=1)
    status: MemoStatusValue
    memo_mode: MemoModeValue
    is_current: bool
    source_watermark: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None = None


class TaskClosureMemoPrepareRequest(_ReportingSchema):
    """Request body for task closure memo preparation."""

    regenerate: bool = False


class TaskClosureMemoReadResponse(TaskClosureMemoAttemptSummary):
    """Task closure memo version with structured body when available."""

    body: MemoBodyValue = Field(
        default=None, validation_alias=AliasChoices("body", "memo")
    )


class TaskClosureMemoPrepareResponse(_ReportingSchema):
    """Response returned after a memo preparation attempt."""

    task_id: int
    memo: TaskClosureMemoReadResponse


class TaskClosureMemoHistoryResponse(_ReportingSchema):
    """Historical task closure memo versions for one task."""

    task_id: int
    items: list[TaskClosureMemoReadResponse] = Field(default_factory=list)


class ReportingInputTaskRow(_ReportingSchema):
    """A single engagement task projected into reporting-input state."""

    task_id: int
    task_name: str
    task_status: str
    runtime_retired: bool
    is_reportable: bool
    is_preparable: bool
    memo_mode: MemoModeValue | None = None
    not_preparable_reason: ReportingReasonCodeValue | None = None
    input_state: InputStateValue
    current_memo: TaskClosureMemoSummary | None = None
    latest_memo_attempt: TaskClosureMemoSummary | None = None
    source_watermark: SourceWatermarkSnapshot
    counts: ReportingSourceCounts
    candidate_findings_require_explicit_inclusion: bool


class EngagementReportingInputsResponse(_ReportingSchema):
    """Engagement-wide reporting input inventory response."""

    engagement_id: int
    tasks: list[ReportingInputTaskRow] = Field(default_factory=list)


class EngagementReportGenerationRequest(_ReportingSchema):
    """Request body for creating an engagement report generation job."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    report_type: ReportTypeValue
    selected_task_memo_ids: list[UUID] = Field(min_length=1, max_length=100)
    include_candidate_findings: bool = False
    force_regenerate: bool = False


class EngagementReportGenerationResponse(_ReportingSchema):
    """Response returned after a report generation request is accepted."""

    job_id: UUID | None = None
    report_id: UUID | None = None
    status: ReportJobStatusValue | ReportStatusValue

    @model_validator(mode="after")
    def _validate_response_shape(self) -> "EngagementReportGenerationResponse":
        if self.status in {"queued", "generating"}:
            if self.job_id is None or self.report_id is not None:
                raise ValueError("queued or generating responses require job_id only")
            return self
        if self.status == "ready":
            if self.report_id is None or self.job_id is not None:
                raise ValueError("ready responses require report_id only")
            return self
        raise ValueError(
            "generation response status must be queued, generating, or ready"
        )


class EngagementReportSectionSourceRefs(_ReportingSchema):
    """Source references cited by a generated report section or block."""

    task_memo_ids: list[SourceReference] = Field(default_factory=list, max_length=100)
    knowledge_refs: list[SourceReference] = Field(default_factory=list, max_length=500)
    evidence_refs: list[SourceReference] = Field(default_factory=list, max_length=500)


class EngagementReportSectionBlock(_ReportingSchema):
    """Structured block inside a generated report section."""

    block_id: str = Field(min_length=1, max_length=128)
    block_type: ReportSectionBlockType
    title: str = Field(min_length=1, max_length=512)
    severity: MemoSeverityHint | None = None
    confidence: MemoConfidence | None = None
    affected_assets: list[str] = Field(default_factory=list, max_length=100)
    content_markdown: str = Field(min_length=1, max_length=20000)
    impact_markdown: str = Field(min_length=1, max_length=20000)
    remediation_markdown: str = Field(min_length=1, max_length=20000)
    source_refs: EngagementReportSectionSourceRefs


class EngagementReportSection(_ReportingSchema):
    """Structured generated report section ready for API serialization."""

    schema_version: str = Field(min_length=1, max_length=64)
    section_id: str = Field(min_length=1, max_length=128)
    section_type: ReportSectionType
    title: str = Field(min_length=1, max_length=512)
    status: ReportSectionStatus
    content_markdown: str = Field(default="", max_length=50000)
    blocks: list[EngagementReportSectionBlock] = Field(default_factory=list)
    source_refs: EngagementReportSectionSourceRefs
    unsupported_notes: list[str] = Field(default_factory=list, max_length=100)
    generation_notes: list[str] = Field(default_factory=list, max_length=100)


class EngagementReportSourceKnowledgeRef(_ReportingSchema):
    """Report-level knowledge source reference used by read projections."""

    ref: SourceReference
    task_id: int
    record_type: str = Field(min_length=1, max_length=64)
    authoritative: bool


class EngagementReportSourceEvidenceRef(_ReportingSchema):
    """Report-level evidence source reference used by read projections."""

    ref: SourceReference
    task_id: int
    evidence_type: str = Field(min_length=1, max_length=64)
    source_tool: str = Field(min_length=1, max_length=128)


class EngagementReportSummary(_ReportingSchema):
    """Summary of a persisted engagement report version."""

    id: UUID
    engagement_id: int
    engagement_name_snapshot: str | None = None
    engagement_status_snapshot: str | None = None
    report_type: ReportTypeValue
    version: int = Field(ge=1)
    status: ReportStatusValue
    is_current: bool
    title: str
    sections: list[dict[str, Any]] = Field(default_factory=list)
    markdown_snapshot: str | None = None
    source_task_memo_ids: list[str] = Field(default_factory=list)
    source_knowledge_refs: list[EngagementReportSourceKnowledgeRef] = Field(
        default_factory=list
    )
    source_evidence_refs: list[EngagementReportSourceEvidenceRef] = Field(
        default_factory=list
    )
    generation_metadata: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None = None


class EngagementReportReadResponse(EngagementReportSummary):
    """Direct read response for one persisted engagement report version."""

    schema_version: str
    sections: list[EngagementReportSection] = Field(default_factory=list)


class EngagementReportHistoryItem(_ReportingSchema):
    """Compact report history row without full generated section content."""

    report_id: UUID = Field(validation_alias=AliasChoices("report_id", "id"))
    engagement_id: int
    engagement_name_snapshot: str | None = None
    engagement_status_snapshot: str | None = None
    report_type: ReportTypeValue
    version: int = Field(ge=1)
    status: ReportStatusValue
    is_current: bool
    title: str
    source_task_memo_ids: list[str] = Field(default_factory=list)
    source_knowledge_refs: list[EngagementReportSourceKnowledgeRef] = Field(
        default_factory=list
    )
    source_evidence_refs: list[EngagementReportSourceEvidenceRef] = Field(
        default_factory=list
    )
    generation_metadata: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None = None


class CurrentEngagementReportResponse(_ReportingSchema):
    """Current report lookup response with a stable empty state."""

    engagement_id: int
    report_type: ReportTypeValue
    report: EngagementReportReadResponse | None = None


class EngagementReportHistoryResponse(_ReportingSchema):
    """Historical report versions for an engagement/report type."""

    engagement_id: int
    report_type: ReportTypeValue
    reports: list[EngagementReportHistoryItem] = Field(default_factory=list)


class ReportLibraryItem(_ReportingSchema):
    """Compact generated report artifact row for tenant/user report library views."""

    report_id: UUID = Field(validation_alias=AliasChoices("report_id", "id"))
    engagement_id: int
    engagement_name_snapshot: str | None = None
    engagement_status_snapshot: str | None = None
    report_type: ReportTypeValue
    version: int = Field(ge=1)
    status: ReportStatusValue
    is_current: bool
    title: str
    source_task_count: int = Field(ge=0)
    source_knowledge_count: int = Field(ge=0)
    source_evidence_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None = None


class ReportLibraryResponse(_ReportingSchema):
    """Tenant/user-owned generated report library page."""

    reports: list[ReportLibraryItem] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class EngagementReportDeleteResponse(_ReportingSchema):
    """Response returned after scheduling report deletion."""

    report_id: UUID
    engagement_id: int
    report_type: ReportTypeValue
    deleted_current: bool
    current_report_id: UUID | None = None
    undo_until: datetime


class EngagementReportUndoDeleteResponse(_ReportingSchema):
    """Response returned after cancelling pending report deletion."""

    report_id: UUID
    engagement_id: int
    report_type: ReportTypeValue
    restored_current: bool
    current_report_id: UUID | None = None


class EngagementReportJobValidationIssue(_ReportingSchema):
    """Safe validation issue summary attached to a failed report job."""

    code: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=256)


class EngagementReportJobFailureDetails(_ReportingSchema):
    """Safe failed-section diagnostics for report job status responses."""

    failed_section_id: str | None = Field(default=None, max_length=128)
    failed_section_order: int | None = Field(default=None, ge=0)
    failed_section_type: str | None = Field(default=None, max_length=64)
    validation_issues: list[EngagementReportJobValidationIssue] = Field(
        default_factory=list,
        max_length=50,
    )


class EngagementReportJobStatusResponse(_ReportingSchema):
    """Read-only status view of a persisted engagement report job."""

    id: UUID
    engagement_id: int
    report_id: UUID | None = None
    report_type: ReportTypeValue
    status: ReportJobStatusValue
    generation_phase: ReportGenerationPhaseValue
    selected_task_memo_ids: list[str] = Field(default_factory=list)
    include_candidate_findings: bool
    source_watermark: dict[str, Any] = Field(default_factory=dict)
    current_section_id: str | None = None
    completed_sections: list[str] = Field(default_factory=list)
    total_sections: int = Field(ge=0)
    next_attempt_at: datetime | None = None
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    last_error_code: str | None = None
    error_message: str | None = None
    last_error_at: datetime | None = None
    failure_details: EngagementReportJobFailureDetails | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EngagementReportActiveJobResponse(_ReportingSchema):
    """Latest active report job for an engagement/report type, if one exists."""

    job: EngagementReportJobStatusResponse | None = None


__all__ = [
    "CurrentEngagementReportResponse",
    "EngagementReportActiveJobResponse",
    "EngagementReportDeleteResponse",
    "EngagementReportGenerationRequest",
    "EngagementReportGenerationResponse",
    "EngagementReportHistoryResponse",
    "EngagementReportHistoryItem",
    "EngagementReportJobFailureDetails",
    "EngagementReportJobStatusResponse",
    "EngagementReportJobValidationIssue",
    "EngagementReportReadResponse",
    "EngagementReportSection",
    "EngagementReportSectionBlock",
    "EngagementReportSectionSourceRefs",
    "EngagementReportSourceEvidenceRef",
    "EngagementReportSourceKnowledgeRef",
    "EngagementReportSummary",
    "EngagementReportUndoDeleteResponse",
    "EngagementReportingInputsResponse",
    "InputStateValue",
    "MemoBodyValue",
    "MemoConfidence",
    "MemoModeValue",
    "MemoSeverityHint",
    "MemoStatusValue",
    "MemoTextSource",
    "ReportGenerationPhaseValue",
    "ReportJobStatusValue",
    "ReportStatusValue",
    "ReportTypeValue",
    "ReportingInputTaskRow",
    "ReportingReasonCodeValue",
    "ReportingSourceCounts",
    "ReportSectionBlockType",
    "ReportSectionStatus",
    "ReportSectionType",
    "SourceReference",
    "SourceWatermarkSnapshot",
    "TaskClosureMemoActionItem",
    "TaskClosureMemoAttemptSummary",
    "TaskClosureMemoBody",
    "TaskClosureMemoHistoryResponse",
    "TaskClosureMemoIncludeInReportRecommendation",
    "TaskClosureMemoLimitationItem",
    "TaskClosureMemoPossibleFindingItem",
    "TaskClosureMemoPrepareRequest",
    "TaskClosureMemoPrepareResponse",
    "TaskClosureMemoReadResponse",
    "TaskClosureMemoReportableObservationItem",
    "TaskClosureMemoSummary",
    "TaskClosureMemoUnsupportedNoteItem",
]
