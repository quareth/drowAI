"""Validate generated task closure memos against task-local source packets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.services.reporting.contracts import (
    GENERATION_METADATA_VALIDATION_STATUS_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    MEMO_MODE_LIMITED,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
    TaskMemoServiceErrorReason,
)
from backend.services.reporting.task_memo_context_builder import TaskMemoContext

_VALIDATION_VERSION = "task_closure_memo_validation.v1"
_VALIDATION_STATUS_PASSED = "passed"

_MemoConfidence = Literal["low", "medium", "high"]
_ActionSource = Literal["transcript", "evidence", "knowledge"]
_SeverityHint = Literal[
    "informational",
    "low",
    "medium",
    "high",
    "critical",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _IncludeRecommendation(_StrictModel):
    include: bool
    reason: str = Field(min_length=1, max_length=2000)


class _ActionPerformed(_StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    source: _ActionSource


class _SourceBackedMemoItem(_StrictModel):
    evidence_refs: list[str] = Field(max_length=100)
    knowledge_refs: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def _requires_source_ref(self) -> _SourceBackedMemoItem:
        if not self.evidence_refs and not self.knowledge_refs:
            raise ValueError("source-backed memo item must include a source ref")
        return self


class _ReportableObservation(_SourceBackedMemoItem):
    text: str = Field(min_length=1, max_length=4000)
    confidence: _MemoConfidence


class _PossibleFinding(_SourceBackedMemoItem):
    title: str = Field(min_length=1, max_length=512)
    severity_hint: _SeverityHint | None
    confidence: _MemoConfidence
    description: str | None = Field(max_length=4000)


class _TextNote(_StrictModel):
    text: str = Field(min_length=1, max_length=2000)


class _TaskClosureMemoPayload(_StrictModel):
    task_name: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=4000)
    include_in_report_recommendation: _IncludeRecommendation
    actions_performed: list[_ActionPerformed] = Field(max_length=100)
    reportable_observations: list[_ReportableObservation] = Field(max_length=100)
    possible_findings: list[_PossibleFinding] = Field(max_length=100)
    limitations: list[_TextNote] = Field(max_length=100)
    unsupported_notes: list[_TextNote] = Field(max_length=100)
    evidence_refs: list[str] = Field(max_length=500)
    knowledge_refs: list[str] = Field(max_length=500)


@dataclass(frozen=True, slots=True)
class TaskClosureMemoValidationIssue:
    """One safe validation issue without generated memo body content."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class TaskClosureMemoValidationResult:
    """Validated memo payload and safe validation metadata."""

    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]


class TaskClosureMemoValidationError(Exception):
    """Typed memo validation failure safe for failed-attempt persistence."""

    def __init__(
        self,
        *,
        issues: Sequence[TaskClosureMemoValidationIssue],
        reason: TaskMemoServiceErrorReason = TASK_MEMO_ERROR_VALIDATION_FAILED,
    ) -> None:
        super().__init__("Generated task closure memo failed validation.")
        self.reason = reason
        self.safe_message = "Generated task closure memo failed validation."
        self.issues = tuple(issues)
        self.metadata = {
            GENERATION_METADATA_VALIDATION_VERSION_KEY: _VALIDATION_VERSION,
            GENERATION_METADATA_VALIDATION_STATUS_KEY: "failed",
            "issue_count": len(self.issues),
            "issues": [asdict(issue) for issue in self.issues],
        }


class TaskClosureMemoValidator:
    """Validate generated memo output against the supplied task memo context."""

    def validate(
        self,
        *,
        payload: Mapping[str, Any],
        context: TaskMemoContext,
    ) -> TaskClosureMemoValidationResult:
        """Return sanitized payload when generated memo output is ready-safe."""

        issues: list[TaskClosureMemoValidationIssue] = []
        memo = _validate_schema(payload, issues)
        if memo is None:
            raise TaskClosureMemoValidationError(issues=issues)

        _validate_packet_scope(context=context, issues=issues)
        _validate_limited_mode(memo=memo, context=context, issues=issues)
        _validate_source_refs(memo=memo, context=context, issues=issues)
        _validate_referenced_item_scope(memo=memo, context=context, issues=issues)
        _validate_candidate_only_handling(memo=memo, context=context, issues=issues)

        if issues:
            raise TaskClosureMemoValidationError(issues=issues)

        return TaskClosureMemoValidationResult(
            payload=memo.model_dump(mode="json"),
            metadata={
                GENERATION_METADATA_VALIDATION_VERSION_KEY: _VALIDATION_VERSION,
                GENERATION_METADATA_VALIDATION_STATUS_KEY: _VALIDATION_STATUS_PASSED,
            },
        )


def validate_task_closure_memo(
    *,
    payload: Mapping[str, Any],
    context: TaskMemoContext,
) -> TaskClosureMemoValidationResult:
    """Validate generated memo output with the default task closure validator."""

    return TaskClosureMemoValidator().validate(payload=payload, context=context)


def _validate_schema(
    payload: Mapping[str, Any],
    issues: list[TaskClosureMemoValidationIssue],
) -> _TaskClosureMemoPayload | None:
    try:
        return _TaskClosureMemoPayload.model_validate(payload)
    except ValidationError as exc:
        for error in exc.errors():
            issues.append(
                TaskClosureMemoValidationIssue(
                    code="schema_invalid",
                    path=_format_path(error.get("loc", ())),
                    message="Generated memo does not match the required schema.",
                )
            )
        return None


def _validate_packet_scope(
    *,
    context: TaskMemoContext,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    task_id = int(context.task.task_id)
    if int(context.evidence.task_id) != task_id:
        issues.append(
            TaskClosureMemoValidationIssue(
                code="packet_scope_mismatch",
                path="evidence_packet.task_id",
                message="Evidence packet does not match the selected task.",
            )
        )
    if int(context.knowledge.task_id) != task_id:
        issues.append(
            TaskClosureMemoValidationIssue(
                code="packet_scope_mismatch",
                path="knowledge_packet.task_id",
                message="Knowledge packet does not match the selected task.",
            )
        )
    if int(context.transcript.task_id) != task_id:
        issues.append(
            TaskClosureMemoValidationIssue(
                code="packet_scope_mismatch",
                path="transcript_context.task_id",
                message="Transcript context does not match the selected task.",
            )
        )


def _validate_limited_mode(
    *,
    memo: _TaskClosureMemoPayload,
    context: TaskMemoContext,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    if context.memo_mode != MEMO_MODE_LIMITED:
        return
    if memo.reportable_observations:
        issues.append(
            TaskClosureMemoValidationIssue(
                code="limited_mode_reportable_content",
                path="reportable_observations",
                message="Limited memo output cannot include reportable observations.",
            )
        )
    if memo.possible_findings:
        issues.append(
            TaskClosureMemoValidationIssue(
                code="limited_mode_reportable_content",
                path="possible_findings",
                message="Limited memo output cannot include possible findings.",
            )
        )


def _validate_source_refs(
    *,
    memo: _TaskClosureMemoPayload,
    context: TaskMemoContext,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    packet_evidence_refs = frozenset(item.ref for item in context.evidence.items)
    packet_knowledge_refs = frozenset(item.ref for item in context.knowledge.items)
    _append_unknown_ref_issues(
        refs=_all_evidence_refs(memo),
        allowed_refs=packet_evidence_refs & context.allowed_evidence_refs,
        ref_kind="evidence",
        issues=issues,
    )
    _append_unknown_ref_issues(
        refs=_all_knowledge_refs(memo),
        allowed_refs=packet_knowledge_refs & context.allowed_knowledge_refs,
        ref_kind="knowledge",
        issues=issues,
    )


def _validate_referenced_item_scope(
    *,
    memo: _TaskClosureMemoPayload,
    context: TaskMemoContext,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    evidence_by_ref = {item.ref: item for item in context.evidence.items}
    knowledge_by_ref = {item.ref: item for item in context.knowledge.items}
    for ref in _all_evidence_refs(memo):
        item = evidence_by_ref.get(ref)
        if item is None:
            continue
        if not _matches_task_scope(
            tenant_id=item.tenant_id,
            user_id=item.user_id,
            engagement_id=item.engagement_id,
            task_id=item.task_id,
            context=context,
        ):
            issues.append(
                TaskClosureMemoValidationIssue(
                    code="evidence_ref_scope_mismatch",
                    path="evidence_refs",
                    message=(
                        "Generated memo referenced an evidence source outside "
                        "the selected task scope."
                    ),
                )
            )
    for ref in _all_knowledge_refs(memo):
        item = knowledge_by_ref.get(ref)
        if item is None:
            continue
        if not _matches_task_scope(
            tenant_id=item.tenant_id,
            user_id=item.user_id,
            engagement_id=item.engagement_id,
            task_id=item.task_id,
            context=context,
        ):
            issues.append(
                TaskClosureMemoValidationIssue(
                    code="knowledge_ref_scope_mismatch",
                    path="knowledge_refs",
                    message=(
                        "Generated memo referenced a knowledge source outside "
                        "the selected task scope."
                    ),
                )
            )


def _matches_task_scope(
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    context: TaskMemoContext,
) -> bool:
    task = context.task
    return (
        int(tenant_id) == int(task.tenant_id)
        and int(user_id) == int(task.user_id)
        and int(engagement_id) == int(task.engagement_id)
        and int(task_id) == int(task.task_id)
    )


def _validate_candidate_only_handling(
    *,
    memo: _TaskClosureMemoPayload,
    context: TaskMemoContext,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    candidate_refs = {
        item.ref
        for item in context.knowledge.items
        if not bool(item.authoritative)
    }
    authoritative_refs = {
        item.ref
        for item in context.knowledge.items
        if bool(item.authoritative)
    }
    if not candidate_refs:
        return

    for index, observation in enumerate(memo.reportable_observations):
        if (
            not observation.evidence_refs
            and set(observation.knowledge_refs).issubset(candidate_refs)
        ):
            issues.append(
                TaskClosureMemoValidationIssue(
                    code="candidate_only_reportable_observation",
                    path=f"reportable_observations.{index}.knowledge_refs",
                    message=(
                        "Candidate-only knowledge cannot be promoted to a "
                        "reportable observation."
                    ),
                )
            )

    for index, finding in enumerate(memo.possible_findings):
        knowledge_refs = set(finding.knowledge_refs)
        has_authoritative_ref = bool(knowledge_refs & authoritative_refs)
        if (
            finding.confidence != "low"
            and not finding.evidence_refs
            and knowledge_refs
            and not has_authoritative_ref
            and knowledge_refs.issubset(candidate_refs)
        ):
            issues.append(
                TaskClosureMemoValidationIssue(
                    code="candidate_only_confidence",
                    path=f"possible_findings.{index}.confidence",
                    message="Candidate-only possible findings must stay low confidence.",
                )
            )


def _all_evidence_refs(memo: _TaskClosureMemoPayload) -> tuple[str, ...]:
    return _unique_refs(
        [
            *memo.evidence_refs,
            *(
                ref
                for item in memo.reportable_observations
                for ref in item.evidence_refs
            ),
            *(ref for item in memo.possible_findings for ref in item.evidence_refs),
        ]
    )


def _all_knowledge_refs(memo: _TaskClosureMemoPayload) -> tuple[str, ...]:
    return _unique_refs(
        [
            *memo.knowledge_refs,
            *(
                ref
                for item in memo.reportable_observations
                for ref in item.knowledge_refs
            ),
            *(ref for item in memo.possible_findings for ref in item.knowledge_refs),
        ]
    )


def _append_unknown_ref_issues(
    *,
    refs: Iterable[str],
    allowed_refs: frozenset[str],
    ref_kind: str,
    issues: list[TaskClosureMemoValidationIssue],
) -> None:
    for ref in refs:
        if ref in allowed_refs:
            continue
        issues.append(
            TaskClosureMemoValidationIssue(
                code=f"unknown_{ref_kind}_ref",
                path=f"{ref_kind}_refs",
                message=f"Generated memo referenced an unknown {ref_kind} source.",
            )
        )


def _unique_refs(refs: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    for ref in refs:
        text = str(ref).strip()
        if text and text not in unique:
            unique.append(text)
    return tuple(unique)


def _format_path(path: object) -> str:
    if not isinstance(path, tuple | list) or not path:
        return "$"
    return ".".join(str(part) for part in path)


__all__ = [
    "TaskClosureMemoValidationError",
    "TaskClosureMemoValidationIssue",
    "TaskClosureMemoValidationResult",
    "TaskClosureMemoValidator",
    "validate_task_closure_memo",
]
