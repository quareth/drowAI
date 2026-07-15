"""Central reporting literals, lifecycle vocabularies, and reason codes.

This module is intentionally side-effect free. It owns shared reporting value
contracts only and does not import database, router, LLM, or worker modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeVar, cast

ReportType = Literal["pentest", "vulnerability_assessment"]
MemoStatus = Literal["preparing", "ready", "failed"]
ReportStatus = Literal["generating", "ready", "failed"]
ReportJobStatus = Literal["queued", "generating", "ready", "failed", "cancelled"]
ReportGenerationPhase = Literal["sections", "finalizing"]
MemoMode = Literal["supported", "limited"]
InputState = Literal["not_prepared", "preparing", "ready", "failed", "stale"]
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
ReportingReasonCode = Literal[
    "task_not_stopped",
    "runtime_retirement_not_confirmed",
    "no_useful_runtime_execution",
    "no_reportable_or_limited_source_material",
]
ReportGenerationServiceErrorReason = Literal[
    "duplicate_selected_task_memo_ids",
    "invalid_request",
    "stale_memo",
    "unsupported_memo_mix",
    "context_unavailable",
    "section_generation_failed",
    "section_timeout",
    "section_validation_failed",
    "finalization_failed",
    "persistence_failed",
    "job_claim_conflict",
    "llm_runtime_unavailable",
]
TaskMemoServiceErrorReason = Literal[
    "task_not_found",
    "engagement_not_found",
    "task_not_in_engagement",
    "task_not_stopped",
    "runtime_retirement_not_confirmed",
    "no_useful_runtime_execution",
    "no_reportable_or_limited_source_material",
    "memo_context_unavailable",
    "prompt_render_failed",
    "llm_runtime_unavailable",
    "memo_generation_failed",
    "memo_validation_failed",
    "memo_persistence_failed",
    "memo_preparation_in_progress",
]

REPORT_TYPE_PENTEST: ReportType = "pentest"
REPORT_TYPE_VULNERABILITY_ASSESSMENT: ReportType = "vulnerability_assessment"
MEMO_STATUS_PREPARING: MemoStatus = "preparing"
MEMO_STATUS_READY: MemoStatus = "ready"
MEMO_STATUS_FAILED: MemoStatus = "failed"
REPORT_STATUS_GENERATING: ReportStatus = "generating"
REPORT_STATUS_READY: ReportStatus = "ready"
REPORT_STATUS_FAILED: ReportStatus = "failed"
REPORT_JOB_STATUS_QUEUED: ReportJobStatus = "queued"
REPORT_JOB_STATUS_GENERATING: ReportJobStatus = "generating"
REPORT_JOB_STATUS_READY: ReportJobStatus = "ready"
REPORT_JOB_STATUS_FAILED: ReportJobStatus = "failed"
REPORT_JOB_STATUS_CANCELLED: ReportJobStatus = "cancelled"
REPORT_GENERATION_PHASE_SECTIONS: ReportGenerationPhase = "sections"
REPORT_GENERATION_PHASE_FINALIZING: ReportGenerationPhase = "finalizing"
MEMO_MODE_SUPPORTED: MemoMode = "supported"
MEMO_MODE_LIMITED: MemoMode = "limited"
INPUT_STATE_NOT_PREPARED: InputState = "not_prepared"
INPUT_STATE_PREPARING: InputState = "preparing"
INPUT_STATE_READY: InputState = "ready"
INPUT_STATE_FAILED: InputState = "failed"
INPUT_STATE_STALE: InputState = "stale"
REPORT_SECTION_STATUS_READY: ReportSectionStatus = "ready"
REPORT_SECTION_STATUS_NEEDS_REVIEW: ReportSectionStatus = "needs_review"
REPORT_SECTION_STATUS_FAILED: ReportSectionStatus = "failed"
REPORT_SECTION_TYPE_NARRATIVE: ReportSectionType = "narrative"
REPORT_SECTION_TYPE_SUMMARY: ReportSectionType = "summary"
REPORT_SECTION_TYPE_FINDINGS: ReportSectionType = "findings"
REPORT_SECTION_TYPE_RECOMMENDATIONS: ReportSectionType = "recommendations"
REPORT_SECTION_TYPE_LIMITATIONS: ReportSectionType = "limitations"
REPORT_SECTION_TYPE_APPENDIX: ReportSectionType = "appendix"
REPORT_SECTION_BLOCK_TYPE_FINDING: ReportSectionBlockType = "finding"
REPORT_SECTION_BLOCK_TYPE_EVIDENCE_NOTE: ReportSectionBlockType = "evidence_note"
REPORT_SECTION_BLOCK_TYPE_ASSET_NOTE: ReportSectionBlockType = "asset_note"
REPORT_SECTION_BLOCK_TYPE_APPENDIX_NOTE: ReportSectionBlockType = "appendix_note"

REPORT_TYPES: tuple[ReportType, ...] = (
    REPORT_TYPE_PENTEST,
    REPORT_TYPE_VULNERABILITY_ASSESSMENT,
)
MEMO_STATUSES: tuple[MemoStatus, ...] = (
    MEMO_STATUS_PREPARING,
    MEMO_STATUS_READY,
    MEMO_STATUS_FAILED,
)
REPORT_STATUSES: tuple[ReportStatus, ...] = (
    REPORT_STATUS_GENERATING,
    REPORT_STATUS_READY,
    REPORT_STATUS_FAILED,
)
REPORT_JOB_STATUSES: tuple[ReportJobStatus, ...] = (
    REPORT_JOB_STATUS_QUEUED,
    REPORT_JOB_STATUS_GENERATING,
    REPORT_JOB_STATUS_READY,
    REPORT_JOB_STATUS_FAILED,
    REPORT_JOB_STATUS_CANCELLED,
)
REPORT_GENERATION_PHASES: tuple[ReportGenerationPhase, ...] = (
    REPORT_GENERATION_PHASE_SECTIONS,
    REPORT_GENERATION_PHASE_FINALIZING,
)
MEMO_MODES: tuple[MemoMode, ...] = (
    MEMO_MODE_SUPPORTED,
    MEMO_MODE_LIMITED,
)
INPUT_STATES: tuple[InputState, ...] = (
    INPUT_STATE_NOT_PREPARED,
    INPUT_STATE_PREPARING,
    INPUT_STATE_READY,
    INPUT_STATE_FAILED,
    INPUT_STATE_STALE,
)
REPORT_SECTION_STATUSES: tuple[ReportSectionStatus, ...] = (
    REPORT_SECTION_STATUS_READY,
    REPORT_SECTION_STATUS_NEEDS_REVIEW,
    REPORT_SECTION_STATUS_FAILED,
)
CURRENT_REPORT_SECTION_STATUSES: tuple[ReportSectionStatus, ...] = (
    REPORT_SECTION_STATUS_READY,
)
REPORT_SECTION_TYPES: tuple[ReportSectionType, ...] = (
    REPORT_SECTION_TYPE_NARRATIVE,
    REPORT_SECTION_TYPE_SUMMARY,
    REPORT_SECTION_TYPE_FINDINGS,
    REPORT_SECTION_TYPE_RECOMMENDATIONS,
    REPORT_SECTION_TYPE_LIMITATIONS,
    REPORT_SECTION_TYPE_APPENDIX,
)
REPORT_SECTION_BLOCK_TYPES: tuple[ReportSectionBlockType, ...] = (
    REPORT_SECTION_BLOCK_TYPE_FINDING,
    REPORT_SECTION_BLOCK_TYPE_EVIDENCE_NOTE,
    REPORT_SECTION_BLOCK_TYPE_ASSET_NOTE,
    REPORT_SECTION_BLOCK_TYPE_APPENDIX_NOTE,
)

REASON_TASK_NOT_STOPPED: ReportingReasonCode = "task_not_stopped"
REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED: ReportingReasonCode = (
    "runtime_retirement_not_confirmed"
)
REASON_NO_USEFUL_RUNTIME_EXECUTION: ReportingReasonCode = "no_useful_runtime_execution"
REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL: ReportingReasonCode = (
    "no_reportable_or_limited_source_material"
)
REPORTING_REASON_CODES: tuple[ReportingReasonCode, ...] = (
    REASON_TASK_NOT_STOPPED,
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    REASON_NO_USEFUL_RUNTIME_EXECUTION,
    REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
)

TASK_CLOSURE_MEMO_SCHEMA_VERSION = "task_closure_memo.v1"
TASK_CLOSURE_MEMO_PROMPT_FAMILY = "task_closure_memo"
TASK_CLOSURE_MEMO_SYSTEM_PROMPT_ID = "task_closure_memo_system"
TASK_CLOSURE_MEMO_USER_PROMPT_ID = "task_closure_memo_user"
TASK_CLOSURE_MEMO_PROMPT_TEMPLATE_IDS = (
    TASK_CLOSURE_MEMO_SYSTEM_PROMPT_ID,
    TASK_CLOSURE_MEMO_USER_PROMPT_ID,
)
TASK_CLOSURE_MEMO_GENERATION_PURPOSE = "reporting.task_closure_memo"
ENGAGEMENT_REPORT_SCHEMA_VERSION = "engagement_report.v1"
REPORT_SECTION_SCHEMA_VERSION = "report_section.v1"
ENGAGEMENT_REPORT_SECTION_PROMPT_FAMILY = "engagement_report_section"

GENERATION_METADATA_PROMPT_FAMILY_KEY = "prompt_family"
GENERATION_METADATA_PROMPT_VERSION_KEY = "prompt_version"
GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY = "prompt_template_ids"
GENERATION_METADATA_PROVIDER_KEY = "provider"
GENERATION_METADATA_MODEL_KEY = "model"
GENERATION_METADATA_REASONING_EFFORT_KEY = "reasoning_effort"
GENERATION_METADATA_USAGE_KEY = "usage"
GENERATION_METADATA_DURATION_MS_KEY = "duration_ms"
GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY = "memo_schema_version"
GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY = (
    "source_watermark_schema_version"
)
GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY = "source_watermark_hash"
GENERATION_METADATA_VALIDATION_VERSION_KEY = "validation_version"
GENERATION_METADATA_VALIDATION_STATUS_KEY = "validation_status"
GENERATION_METADATA_SECTION_PLAN_VERSION_KEY = "section_plan_version"
GENERATION_METADATA_RENDERER_VERSION_KEY = "renderer_version"
TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS = (
    GENERATION_METADATA_PROMPT_FAMILY_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_USAGE_KEY,
    GENERATION_METADATA_DURATION_MS_KEY,
    GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    GENERATION_METADATA_VALIDATION_STATUS_KEY,
)
ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS = (
    GENERATION_METADATA_SECTION_PLAN_VERSION_KEY,
    GENERATION_METADATA_RENDERER_VERSION_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    GENERATION_METADATA_PROMPT_VERSION_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    GENERATION_METADATA_PROVIDER_KEY,
    GENERATION_METADATA_MODEL_KEY,
    GENERATION_METADATA_REASONING_EFFORT_KEY,
    GENERATION_METADATA_USAGE_KEY,
    GENERATION_METADATA_DURATION_MS_KEY,
)

REPORT_GENERATION_ERROR_INVALID_REQUEST: ReportGenerationServiceErrorReason = (
    "invalid_request"
)
REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS: ReportGenerationServiceErrorReason = "duplicate_selected_task_memo_ids"
REPORT_GENERATION_ERROR_STALE_MEMO: ReportGenerationServiceErrorReason = "stale_memo"
REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX: ReportGenerationServiceErrorReason = (
    "unsupported_memo_mix"
)
REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE: ReportGenerationServiceErrorReason = (
    "context_unavailable"
)
REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED: ReportGenerationServiceErrorReason = "section_generation_failed"
REPORT_GENERATION_ERROR_SECTION_TIMEOUT: ReportGenerationServiceErrorReason = (
    "section_timeout"
)
REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED: ReportGenerationServiceErrorReason = "section_validation_failed"
REPORT_GENERATION_ERROR_FINALIZATION_FAILED: ReportGenerationServiceErrorReason = (
    "finalization_failed"
)
REPORT_GENERATION_ERROR_PERSISTENCE_FAILED: ReportGenerationServiceErrorReason = (
    "persistence_failed"
)
REPORT_GENERATION_ERROR_JOB_CLAIM_CONFLICT: ReportGenerationServiceErrorReason = (
    "job_claim_conflict"
)
REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE: ReportGenerationServiceErrorReason = (
    "llm_runtime_unavailable"
)
REPORT_GENERATION_SERVICE_ERROR_REASONS: tuple[
    ReportGenerationServiceErrorReason, ...
] = (
    REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
    REPORT_GENERATION_ERROR_INVALID_REQUEST,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
    REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE,
    REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED,
    REPORT_GENERATION_ERROR_SECTION_TIMEOUT,
    REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED,
    REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_ERROR_JOB_CLAIM_CONFLICT,
    REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
)

TASK_MEMO_ERROR_TASK_NOT_FOUND: TaskMemoServiceErrorReason = "task_not_found"
TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND: TaskMemoServiceErrorReason = (
    "engagement_not_found"
)
TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT: TaskMemoServiceErrorReason = (
    "task_not_in_engagement"
)
TASK_MEMO_ERROR_TASK_NOT_STOPPED: TaskMemoServiceErrorReason = REASON_TASK_NOT_STOPPED
TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED: TaskMemoServiceErrorReason = (
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED
)
TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION: TaskMemoServiceErrorReason = (
    REASON_NO_USEFUL_RUNTIME_EXECUTION
)
TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL: TaskMemoServiceErrorReason = (
    REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL
)
TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE: TaskMemoServiceErrorReason = (
    "memo_context_unavailable"
)
TASK_MEMO_ERROR_PROMPT_RENDER_FAILED: TaskMemoServiceErrorReason = (
    "prompt_render_failed"
)
TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE: TaskMemoServiceErrorReason = (
    "llm_runtime_unavailable"
)
TASK_MEMO_ERROR_GENERATION_FAILED: TaskMemoServiceErrorReason = "memo_generation_failed"
TASK_MEMO_ERROR_VALIDATION_FAILED: TaskMemoServiceErrorReason = "memo_validation_failed"
TASK_MEMO_ERROR_PERSISTENCE_FAILED: TaskMemoServiceErrorReason = (
    "memo_persistence_failed"
)
TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS: TaskMemoServiceErrorReason = (
    "memo_preparation_in_progress"
)
TASK_MEMO_SERVICE_ERROR_REASONS: tuple[TaskMemoServiceErrorReason, ...] = (
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND,
    TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT,
    TASK_MEMO_ERROR_TASK_NOT_STOPPED,
    TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION,
    TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
    TASK_MEMO_ERROR_PROMPT_RENDER_FAILED,
    TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE,
    TASK_MEMO_ERROR_GENERATION_FAILED,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
    TASK_MEMO_ERROR_PERSISTENCE_FAILED,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
)


@dataclass(frozen=True, slots=True)
class ReportingContractValues:
    """Immutable vocabulary used by reporting validation and serialization."""

    report_types: tuple[ReportType, ...] = REPORT_TYPES
    memo_statuses: tuple[MemoStatus, ...] = MEMO_STATUSES
    report_statuses: tuple[ReportStatus, ...] = REPORT_STATUSES
    report_job_statuses: tuple[ReportJobStatus, ...] = REPORT_JOB_STATUSES
    report_generation_phases: tuple[ReportGenerationPhase, ...] = (
        REPORT_GENERATION_PHASES
    )
    memo_modes: tuple[MemoMode, ...] = MEMO_MODES
    input_states: tuple[InputState, ...] = INPUT_STATES
    reason_codes: tuple[ReportingReasonCode, ...] = REPORTING_REASON_CODES


REPORTING_CONTRACTS = ReportingContractValues()


@dataclass(frozen=True, slots=True)
class EngagementReportGenerationContractValues:
    """Immutable engagement report generation contract identifiers."""

    report_schema_version: str = ENGAGEMENT_REPORT_SCHEMA_VERSION
    section_schema_version: str = REPORT_SECTION_SCHEMA_VERSION
    section_prompt_family: str = ENGAGEMENT_REPORT_SECTION_PROMPT_FAMILY
    section_statuses: tuple[ReportSectionStatus, ...] = REPORT_SECTION_STATUSES
    current_report_section_statuses: tuple[ReportSectionStatus, ...] = (
        CURRENT_REPORT_SECTION_STATUSES
    )
    section_types: tuple[ReportSectionType, ...] = REPORT_SECTION_TYPES
    section_block_types: tuple[ReportSectionBlockType, ...] = REPORT_SECTION_BLOCK_TYPES
    generation_metadata_keys: tuple[str, ...] = (
        ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS
    )
    service_error_reasons: tuple[ReportGenerationServiceErrorReason, ...] = (
        REPORT_GENERATION_SERVICE_ERROR_REASONS
    )


ENGAGEMENT_REPORT_GENERATION_CONTRACTS = EngagementReportGenerationContractValues()


@dataclass(frozen=True, slots=True)
class TaskClosureMemoContractValues:
    """Immutable memo preparation contract identifiers and metadata keys."""

    schema_version: str = TASK_CLOSURE_MEMO_SCHEMA_VERSION
    prompt_family: str = TASK_CLOSURE_MEMO_PROMPT_FAMILY
    system_prompt_id: str = TASK_CLOSURE_MEMO_SYSTEM_PROMPT_ID
    user_prompt_id: str = TASK_CLOSURE_MEMO_USER_PROMPT_ID
    prompt_template_ids: tuple[str, ...] = TASK_CLOSURE_MEMO_PROMPT_TEMPLATE_IDS
    generation_purpose: str = TASK_CLOSURE_MEMO_GENERATION_PURPOSE
    generation_metadata_keys: tuple[str, ...] = (
        TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS
    )
    service_error_reasons: tuple[TaskMemoServiceErrorReason, ...] = (
        TASK_MEMO_SERVICE_ERROR_REASONS
    )


TASK_CLOSURE_MEMO_CONTRACTS = TaskClosureMemoContractValues()

_LiteralValue = TypeVar("_LiteralValue", bound=str)


def _validate_literal(
    value: str,
    allowed_values: tuple[_LiteralValue, ...],
    field_name: str,
) -> _LiteralValue:
    normalized = str(value).strip()
    if normalized in allowed_values:
        return cast(_LiteralValue, normalized)
    allowed = ", ".join(allowed_values)
    raise ValueError(
        f"Unsupported reporting {field_name}: {value!r}. Allowed: {allowed}"
    )


def validate_report_type(value: str) -> ReportType:
    """Return a validated MVP report type."""

    return _validate_literal(value, REPORT_TYPES, "report_type")


def validate_memo_status(value: str) -> MemoStatus:
    """Return a validated task closure memo status."""

    return _validate_literal(value, MEMO_STATUSES, "memo_status")


def validate_report_status(value: str) -> ReportStatus:
    """Return a validated engagement report lifecycle status."""

    return _validate_literal(value, REPORT_STATUSES, "report_status")


def validate_report_job_status(value: str) -> ReportJobStatus:
    """Return a validated engagement report job lifecycle status."""

    return _validate_literal(value, REPORT_JOB_STATUSES, "report_job_status")


def validate_report_generation_phase(value: str) -> ReportGenerationPhase:
    """Return a validated durable report generation phase."""

    return _validate_literal(value, REPORT_GENERATION_PHASES, "report_generation_phase")


def validate_memo_mode(value: str) -> MemoMode:
    """Return a validated reporting input memo mode."""

    return _validate_literal(value, MEMO_MODES, "memo_mode")


def validate_input_state(value: str) -> InputState:
    """Return a validated reporting input display state."""

    return _validate_literal(value, INPUT_STATES, "input_state")


def validate_report_section_status(value: str) -> ReportSectionStatus:
    """Return a validated report section parser status."""

    return _validate_literal(value, REPORT_SECTION_STATUSES, "section_status")


def validate_current_report_section_status(value: str) -> ReportSectionStatus:
    """Return a validated section status eligible for current reports."""

    return _validate_literal(
        value,
        CURRENT_REPORT_SECTION_STATUSES,
        "current_report_section_status",
    )


def validate_report_section_type(value: str) -> ReportSectionType:
    """Return a validated report section type."""

    return _validate_literal(value, REPORT_SECTION_TYPES, "section_type")


def validate_report_section_block_type(value: str) -> ReportSectionBlockType:
    """Return a validated report section block type."""

    return _validate_literal(value, REPORT_SECTION_BLOCK_TYPES, "block_type")


def validate_reporting_reason_code(value: str) -> ReportingReasonCode:
    """Return a validated reporting reason code."""

    return _validate_literal(value, REPORTING_REASON_CODES, "reason_code")


def validate_report_generation_service_error_reason(
    value: str,
) -> ReportGenerationServiceErrorReason:
    """Return a validated engagement report generation service error reason."""

    return _validate_literal(
        value,
        REPORT_GENERATION_SERVICE_ERROR_REASONS,
        "service_error",
    )


def validate_task_memo_service_error_reason(
    value: str,
) -> TaskMemoServiceErrorReason:
    """Return a validated task closure memo service error reason."""

    return _validate_literal(value, TASK_MEMO_SERVICE_ERROR_REASONS, "service_error")


__all__ = [
    "GENERATION_METADATA_DURATION_MS_KEY",
    "GENERATION_METADATA_MEMO_SCHEMA_VERSION_KEY",
    "GENERATION_METADATA_MODEL_KEY",
    "GENERATION_METADATA_PROMPT_FAMILY_KEY",
    "GENERATION_METADATA_PROMPT_TEMPLATE_IDS_KEY",
    "GENERATION_METADATA_PROMPT_VERSION_KEY",
    "GENERATION_METADATA_PROVIDER_KEY",
    "GENERATION_METADATA_REASONING_EFFORT_KEY",
    "GENERATION_METADATA_RENDERER_VERSION_KEY",
    "GENERATION_METADATA_SECTION_PLAN_VERSION_KEY",
    "GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY",
    "GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY",
    "GENERATION_METADATA_USAGE_KEY",
    "GENERATION_METADATA_VALIDATION_STATUS_KEY",
    "GENERATION_METADATA_VALIDATION_VERSION_KEY",
    "CURRENT_REPORT_SECTION_STATUSES",
    "ENGAGEMENT_REPORT_GENERATION_CONTRACTS",
    "ENGAGEMENT_REPORT_GENERATION_METADATA_KEYS",
    "ENGAGEMENT_REPORT_SCHEMA_VERSION",
    "ENGAGEMENT_REPORT_SECTION_PROMPT_FAMILY",
    "INPUT_STATES",
    "INPUT_STATE_FAILED",
    "INPUT_STATE_NOT_PREPARED",
    "INPUT_STATE_PREPARING",
    "INPUT_STATE_READY",
    "INPUT_STATE_STALE",
    "MEMO_MODE_LIMITED",
    "MEMO_MODE_SUPPORTED",
    "MEMO_MODES",
    "MEMO_STATUS_FAILED",
    "MEMO_STATUS_PREPARING",
    "MEMO_STATUS_READY",
    "MEMO_STATUSES",
    "REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL",
    "REASON_NO_USEFUL_RUNTIME_EXECUTION",
    "REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED",
    "REASON_TASK_NOT_STOPPED",
    "REPORTING_CONTRACTS",
    "REPORTING_REASON_CODES",
    "REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE",
    "REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS",
    "REPORT_GENERATION_ERROR_FINALIZATION_FAILED",
    "REPORT_GENERATION_ERROR_INVALID_REQUEST",
    "REPORT_GENERATION_ERROR_JOB_CLAIM_CONFLICT",
    "REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE",
    "REPORT_GENERATION_ERROR_PERSISTENCE_FAILED",
    "REPORT_GENERATION_ERROR_SECTION_GENERATION_FAILED",
    "REPORT_GENERATION_ERROR_SECTION_TIMEOUT",
    "REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED",
    "REPORT_GENERATION_ERROR_STALE_MEMO",
    "REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX",
    "REPORT_GENERATION_PHASE_FINALIZING",
    "REPORT_GENERATION_PHASE_SECTIONS",
    "REPORT_GENERATION_PHASES",
    "REPORT_GENERATION_SERVICE_ERROR_REASONS",
    "REPORT_JOB_STATUS_CANCELLED",
    "REPORT_JOB_STATUS_FAILED",
    "REPORT_JOB_STATUS_GENERATING",
    "REPORT_JOB_STATUS_QUEUED",
    "REPORT_JOB_STATUS_READY",
    "REPORT_JOB_STATUSES",
    "REPORT_STATUS_FAILED",
    "REPORT_STATUS_GENERATING",
    "REPORT_STATUS_READY",
    "REPORT_STATUSES",
    "REPORT_SECTION_BLOCK_TYPES",
    "REPORT_SECTION_BLOCK_TYPE_APPENDIX_NOTE",
    "REPORT_SECTION_BLOCK_TYPE_ASSET_NOTE",
    "REPORT_SECTION_BLOCK_TYPE_EVIDENCE_NOTE",
    "REPORT_SECTION_BLOCK_TYPE_FINDING",
    "REPORT_SECTION_SCHEMA_VERSION",
    "REPORT_SECTION_STATUSES",
    "REPORT_SECTION_STATUS_FAILED",
    "REPORT_SECTION_STATUS_NEEDS_REVIEW",
    "REPORT_SECTION_STATUS_READY",
    "REPORT_SECTION_TYPES",
    "REPORT_SECTION_TYPE_APPENDIX",
    "REPORT_SECTION_TYPE_FINDINGS",
    "REPORT_SECTION_TYPE_LIMITATIONS",
    "REPORT_SECTION_TYPE_NARRATIVE",
    "REPORT_SECTION_TYPE_RECOMMENDATIONS",
    "REPORT_SECTION_TYPE_SUMMARY",
    "REPORT_TYPE_PENTEST",
    "REPORT_TYPE_VULNERABILITY_ASSESSMENT",
    "REPORT_TYPES",
    "TASK_CLOSURE_MEMO_CONTRACTS",
    "TASK_CLOSURE_MEMO_GENERATION_METADATA_KEYS",
    "TASK_CLOSURE_MEMO_GENERATION_PURPOSE",
    "TASK_CLOSURE_MEMO_PROMPT_FAMILY",
    "TASK_CLOSURE_MEMO_PROMPT_TEMPLATE_IDS",
    "TASK_CLOSURE_MEMO_SCHEMA_VERSION",
    "TASK_CLOSURE_MEMO_SYSTEM_PROMPT_ID",
    "TASK_CLOSURE_MEMO_USER_PROMPT_ID",
    "TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE",
    "TASK_MEMO_ERROR_ENGAGEMENT_NOT_FOUND",
    "TASK_MEMO_ERROR_GENERATION_FAILED",
    "TASK_MEMO_ERROR_LLM_RUNTIME_UNAVAILABLE",
    "TASK_MEMO_ERROR_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL",
    "TASK_MEMO_ERROR_NO_USEFUL_RUNTIME_EXECUTION",
    "TASK_MEMO_ERROR_PERSISTENCE_FAILED",
    "TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS",
    "TASK_MEMO_ERROR_PROMPT_RENDER_FAILED",
    "TASK_MEMO_ERROR_RUNTIME_RETIREMENT_NOT_CONFIRMED",
    "TASK_MEMO_ERROR_TASK_NOT_FOUND",
    "TASK_MEMO_ERROR_TASK_NOT_IN_ENGAGEMENT",
    "TASK_MEMO_ERROR_TASK_NOT_STOPPED",
    "TASK_MEMO_ERROR_VALIDATION_FAILED",
    "TASK_MEMO_SERVICE_ERROR_REASONS",
    "EngagementReportGenerationContractValues",
    "InputState",
    "MemoMode",
    "MemoStatus",
    "ReportGenerationPhase",
    "ReportGenerationServiceErrorReason",
    "ReportJobStatus",
    "ReportSectionBlockType",
    "ReportSectionStatus",
    "ReportSectionType",
    "ReportStatus",
    "ReportType",
    "ReportingContractValues",
    "ReportingReasonCode",
    "TaskClosureMemoContractValues",
    "TaskMemoServiceErrorReason",
    "validate_input_state",
    "validate_memo_mode",
    "validate_memo_status",
    "validate_current_report_section_status",
    "validate_report_generation_phase",
    "validate_report_generation_service_error_reason",
    "validate_report_job_status",
    "validate_report_section_block_type",
    "validate_report_section_status",
    "validate_report_section_type",
    "validate_report_status",
    "validate_report_type",
    "validate_reporting_reason_code",
    "validate_task_memo_service_error_reason",
]
