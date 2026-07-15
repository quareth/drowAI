"""Validate generated engagement report sections against report context."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from backend.services.reporting.contracts import (
    GENERATION_METADATA_VALIDATION_STATUS_KEY,
    GENERATION_METADATA_VALIDATION_VERSION_KEY,
    REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED,
    REPORT_SECTION_STATUS_READY,
    REPORT_SECTION_TYPE_APPENDIX,
    REPORT_SECTION_TYPE_LIMITATIONS,
    ReportGenerationServiceErrorReason,
)
from backend.services.reporting.report_content_safety import (
    internal_identifier_markers,
)
from backend.services.reporting.report_context_builder import ReportContext
from backend.services.reporting.report_section_plan import ReportSectionPlanItem
from backend.services.reporting.report_section_schema import (
    engagement_report_section_json_schema,
)

_VALIDATION_VERSION = "engagement_report_section_validation.v1"
_VALIDATION_STATUS_PASSED = "passed"
_SOURCE_REF_KEYS = ("task_memo_ids", "knowledge_refs", "evidence_refs")
_REPORTABLE_EXCLUDED_SECTION_TYPES = {
    REPORT_SECTION_TYPE_APPENDIX,
    REPORT_SECTION_TYPE_LIMITATIONS,
}


@dataclass(frozen=True, slots=True)
class ReportSectionValidationIssue:
    """One safe validation issue without generated report body content."""

    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class ReportSectionValidationResult:
    """Validated section payload and safe validation metadata."""

    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]


class ReportSectionValidationError(Exception):
    """Typed section validation failure safe for report job persistence."""

    def __init__(
        self,
        *,
        issues: Sequence[ReportSectionValidationIssue],
        reason: ReportGenerationServiceErrorReason = (
            REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED
        ),
    ) -> None:
        super().__init__("Generated report section failed validation.")
        self.reason = reason
        self.safe_message = "Generated report section failed validation."
        self.issues = tuple(issues)
        self.metadata = {
            GENERATION_METADATA_VALIDATION_VERSION_KEY: _VALIDATION_VERSION,
            GENERATION_METADATA_VALIDATION_STATUS_KEY: "failed",
            "issue_count": len(self.issues),
            "issues": [asdict(issue) for issue in self.issues],
        }


class ReportSectionValidator:
    """Validate generated section output against supplied context and plan."""

    def __init__(self) -> None:
        self._schema_validator = Draft202012Validator(
            engagement_report_section_json_schema()
        )

    def validate(
        self,
        *,
        payload: Mapping[str, Any],
        context: ReportContext,
        section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
    ) -> ReportSectionValidationResult:
        """Return sanitized payload when generated section output is ready-safe."""

        issues: list[ReportSectionValidationIssue] = []
        section = _validate_schema(
            payload=payload,
            validator=self._schema_validator,
            issues=issues,
        )
        if section is None:
            _append_raw_finding_ref_issues(payload=payload, issues=issues)
            raise ReportSectionValidationError(issues=issues)

        plan = _section_plan_payload(section_plan_item)
        _validate_plan_match(section=section, plan=plan, issues=issues)
        _validate_ready_status(section=section, issues=issues)
        _validate_source_refs(section=section, context=context, issues=issues)
        _validate_reportable_grounding(section=section, context=context, issues=issues)
        _validate_customer_markdown_identifiers(
            section=section,
            context=context,
            issues=issues,
        )

        if issues:
            raise ReportSectionValidationError(issues=issues)

        return ReportSectionValidationResult(
            payload=section,
            metadata={
                GENERATION_METADATA_VALIDATION_VERSION_KEY: _VALIDATION_VERSION,
                GENERATION_METADATA_VALIDATION_STATUS_KEY: _VALIDATION_STATUS_PASSED,
            },
        )


def validate_report_section(
    *,
    payload: Mapping[str, Any],
    context: ReportContext,
    section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
) -> ReportSectionValidationResult:
    """Validate generated section output with the default section validator."""

    return ReportSectionValidator().validate(
        payload=payload,
        context=context,
        section_plan_item=section_plan_item,
    )


def _validate_schema(
    *,
    payload: Mapping[str, Any],
    validator: Draft202012Validator,
    issues: list[ReportSectionValidationIssue],
) -> dict[str, Any] | None:
    errors = sorted(validator.iter_errors(payload), key=lambda error: error.path)
    if errors:
        for error in errors:
            issues.append(
                ReportSectionValidationIssue(
                    code="schema_invalid",
                    path=_format_error_path(error),
                    message="Generated section does not match the required schema.",
                )
            )
        return None
    return _sanitize_section(payload)


def _sanitize_section(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(payload["schema_version"]),
        "section_id": str(payload["section_id"]),
        "section_type": str(payload["section_type"]),
        "title": str(payload["title"]),
        "status": str(payload["status"]),
        "content_markdown": str(payload["content_markdown"]),
        "blocks": [_sanitize_block(block) for block in payload["blocks"]],
        "source_refs": _sanitize_source_refs(payload["source_refs"]),
        "unsupported_notes": [str(note) for note in payload["unsupported_notes"]],
        "generation_notes": [str(note) for note in payload["generation_notes"]],
    }


def _sanitize_block(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(payload["block_id"]),
        "block_type": str(payload["block_type"]),
        "title": str(payload["title"]),
        "severity": (
            str(payload["severity"]) if payload["severity"] is not None else None
        ),
        "confidence": (
            str(payload["confidence"]) if payload["confidence"] is not None else None
        ),
        "affected_assets": [str(asset) for asset in payload["affected_assets"]],
        "content_markdown": str(payload["content_markdown"]),
        "impact_markdown": str(payload["impact_markdown"]),
        "remediation_markdown": str(payload["remediation_markdown"]),
        "source_refs": _sanitize_source_refs(payload["source_refs"]),
    }


def _sanitize_source_refs(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        key: _unique_refs(str(ref).strip() for ref in payload[key])
        for key in _SOURCE_REF_KEYS
    }


def _validate_plan_match(
    *,
    section: Mapping[str, Any],
    plan: Mapping[str, Any],
    issues: list[ReportSectionValidationIssue],
) -> None:
    for field in ("section_id", "section_type", "title"):
        if str(section[field]) == str(plan.get(field)):
            continue
        issues.append(
            ReportSectionValidationIssue(
                code=f"{field}_mismatch",
                path=field,
                message="Generated section does not match the fixed section plan.",
            )
        )


def _validate_ready_status(
    *,
    section: Mapping[str, Any],
    issues: list[ReportSectionValidationIssue],
) -> None:
    if section["status"] == REPORT_SECTION_STATUS_READY:
        return
    issues.append(
        ReportSectionValidationIssue(
            code="section_status_not_ready",
            path="status",
            message="Only ready generated sections can be persisted in a ready report.",
        )
    )


def _validate_source_refs(
    *,
    section: Mapping[str, Any],
    context: ReportContext,
    issues: list[ReportSectionValidationIssue],
) -> None:
    knowledge_by_ref = {item.ref: item for item in context.compatible_knowledge_refs}
    evidence_by_ref = {item.ref: item for item in context.compatible_evidence_refs}
    selected_task_ids = {int(task.task_id) for task in context.selected_tasks}
    memo_task_ids = {memo.memo_id: int(memo.task_id) for memo in context.selected_memos}

    for source_path, refs in _iter_source_refs(section):
        _append_unknown_ref_issues(
            refs=refs["task_memo_ids"],
            allowed_refs=context.allowed_task_memo_ids,
            code="unknown_task_memo_ref",
            path=f"{source_path}.task_memo_ids",
            message="Generated section referenced an unknown task memo source.",
            issues=issues,
        )
        _append_unknown_ref_issues(
            refs=refs["knowledge_refs"],
            allowed_refs=context.allowed_knowledge_refs,
            code="unknown_knowledge_ref",
            path=f"{source_path}.knowledge_refs",
            message="Generated section referenced an unknown knowledge source.",
            issues=issues,
        )
        _append_unknown_ref_issues(
            refs=refs["evidence_refs"],
            allowed_refs=context.allowed_evidence_refs,
            code="unknown_evidence_ref",
            path=f"{source_path}.evidence_refs",
            message="Generated section referenced an unknown evidence source.",
            issues=issues,
        )

        for ref in refs["task_memo_ids"]:
            if ref not in context.allowed_task_memo_ids:
                continue
            if ref in memo_task_ids:
                continue
            issues.append(
                ReportSectionValidationIssue(
                    code="task_memo_ref_scope_mismatch",
                    path=f"{source_path}.task_memo_ids",
                    message="Generated section referenced a memo outside report scope.",
                )
            )
        for ref in refs["knowledge_refs"]:
            if ref not in context.allowed_knowledge_refs:
                continue
            item = knowledge_by_ref.get(ref)
            if item is not None and int(item.task_id) in selected_task_ids:
                continue
            issues.append(
                ReportSectionValidationIssue(
                    code="knowledge_ref_scope_mismatch",
                    path=f"{source_path}.knowledge_refs",
                    message=(
                        "Generated section referenced knowledge outside selected "
                        "task lineage."
                    ),
                )
            )
        for ref in refs["evidence_refs"]:
            if ref not in context.allowed_evidence_refs:
                continue
            item = evidence_by_ref.get(ref)
            if item is not None and int(item.task_id) in selected_task_ids:
                continue
            issues.append(
                ReportSectionValidationIssue(
                    code="evidence_ref_scope_mismatch",
                    path=f"{source_path}.evidence_refs",
                    message=(
                        "Generated section referenced evidence outside selected "
                        "task lineage."
                    ),
                )
            )


def _validate_reportable_grounding(
    *,
    section: Mapping[str, Any],
    context: ReportContext,
    issues: list[ReportSectionValidationIssue],
) -> None:
    limited_memo_ids = set(context.memo_partitions.limited_memo_ids)
    section_type = str(section["section_type"])
    reportable_section = section_type not in _REPORTABLE_EXCLUDED_SECTION_TYPES
    candidate_knowledge_refs = {
        item.ref for item in context.compatible_knowledge_refs if not item.authoritative
    }
    candidate_knowledge_refs.update(context.candidate_only_knowledge_refs)
    evidence_by_ref = {item.ref: item for item in context.compatible_evidence_refs}

    for source_path, refs in _iter_source_refs(section):
        if source_path.startswith("blocks."):
            block = section["blocks"][int(source_path.split(".", 2)[1])]
            if block["block_type"] == "finding" and not (
                refs["knowledge_refs"] or refs["evidence_refs"]
            ):
                issues.append(
                    ReportSectionValidationIssue(
                        code="finding_block_missing_reportable_ref",
                        path=f"{source_path}.source_refs",
                        message=(
                            "Finding blocks must include knowledge or evidence refs."
                        ),
                    )
                )

        if not context.candidate_policy.include_candidate_findings:
            _append_candidate_ref_issues(
                refs=refs,
                candidate_knowledge_refs=candidate_knowledge_refs,
                evidence_by_ref=evidence_by_ref,
                path=source_path,
                issues=issues,
            )

        if not reportable_section:
            continue
        if refs["task_memo_ids"] and not (
            refs["knowledge_refs"] or refs["evidence_refs"]
        ):
            issues.append(
                ReportSectionValidationIssue(
                    code="transcript_only_reportable_content",
                    path=source_path,
                    message=(
                        "Reportable section content must cite supplied knowledge or "
                        "evidence context."
                    ),
                )
            )
        limited_refs = set(refs["task_memo_ids"]) & limited_memo_ids
        if limited_refs:
            issues.append(
                ReportSectionValidationIssue(
                    code="limited_memo_ref_misuse",
                    path=f"{source_path}.task_memo_ids",
                    message=(
                        "Limited memo refs can only support limitations or appendix "
                        "sections."
                    ),
                )
            )


def _validate_customer_markdown_identifiers(
    *,
    section: Mapping[str, Any],
    context: ReportContext,
    issues: list[ReportSectionValidationIssue],
) -> None:
    forbidden_refs = _customer_forbidden_refs(context)
    for path, value in _iter_customer_text_fields(section):
        if not internal_identifier_markers(value, forbidden_refs=forbidden_refs):
            continue
        issues.append(
            ReportSectionValidationIssue(
                code="customer_markdown_internal_identifier",
                path=path,
                message=(
                    "Generated customer-facing report text included an internal "
                    "reporting identifier."
                ),
            )
        )


def _iter_customer_text_fields(
    section: Mapping[str, Any],
) -> Iterable[tuple[str, Any]]:
    yield "title", section.get("title")
    yield "content_markdown", section.get("content_markdown")
    for index, note in enumerate(section.get("unsupported_notes") or ()):
        yield f"unsupported_notes.{index}", note
    for index, note in enumerate(section.get("generation_notes") or ()):
        yield f"generation_notes.{index}", note
    for index, block in enumerate(section.get("blocks") or ()):
        if not isinstance(block, Mapping):
            continue
        yield f"blocks.{index}.title", block.get("title")
        yield f"blocks.{index}.content_markdown", block.get("content_markdown")
        yield f"blocks.{index}.impact_markdown", block.get("impact_markdown")
        yield f"blocks.{index}.remediation_markdown", block.get(
            "remediation_markdown"
        )


def _customer_forbidden_refs(context: ReportContext) -> tuple[str, ...]:
    values = (
        *context.allowed_task_memo_ids,
        *context.allowed_knowledge_refs,
        *context.allowed_evidence_refs,
        context.source_watermark.hash,
    )
    return tuple(sorted(str(value) for value in values if str(value).strip()))


def _append_candidate_ref_issues(
    *,
    refs: Mapping[str, Sequence[str]],
    candidate_knowledge_refs: set[str],
    evidence_by_ref: Mapping[str, Any],
    path: str,
    issues: list[ReportSectionValidationIssue],
) -> None:
    for ref in refs["knowledge_refs"]:
        if ref not in candidate_knowledge_refs:
            continue
        issues.append(
            ReportSectionValidationIssue(
                code="candidate_ref_disallowed",
                path=f"{path}.knowledge_refs",
                message="Candidate-only refs are disabled for this report context.",
            )
        )

    for ref in refs["evidence_refs"]:
        evidence = evidence_by_ref.get(ref)
        if evidence is None:
            continue
        linked_refs = set(evidence.linked_knowledge_refs)
        if linked_refs and linked_refs.issubset(candidate_knowledge_refs):
            issues.append(
                ReportSectionValidationIssue(
                    code="candidate_ref_disallowed",
                    path=f"{path}.evidence_refs",
                    message="Candidate-only refs are disabled for this report context.",
                )
            )


def _append_raw_finding_ref_issues(
    *,
    payload: Mapping[str, Any],
    issues: list[ReportSectionValidationIssue],
) -> None:
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping) or block.get("block_type") != "finding":
            continue
        source_refs = block.get("source_refs")
        if not isinstance(source_refs, Mapping):
            continue
        if source_refs.get("knowledge_refs") or source_refs.get("evidence_refs"):
            continue
        issues.append(
            ReportSectionValidationIssue(
                code="finding_block_missing_reportable_ref",
                path=f"blocks.{index}.source_refs",
                message="Finding blocks must include knowledge or evidence refs.",
            )
        )


def _append_unknown_ref_issues(
    *,
    refs: Iterable[str],
    allowed_refs: frozenset[str],
    code: str,
    path: str,
    message: str,
    issues: list[ReportSectionValidationIssue],
) -> None:
    for ref in refs:
        if ref in allowed_refs:
            continue
        issues.append(
            ReportSectionValidationIssue(
                code=code,
                path=path,
                message=message,
            )
        )


def _iter_source_refs(
    section: Mapping[str, Any],
) -> Iterable[tuple[str, Mapping[str, Sequence[str]]]]:
    yield "source_refs", section["source_refs"]
    for index, block in enumerate(section["blocks"]):
        yield f"blocks.{index}.source_refs", block["source_refs"]


def _section_plan_payload(
    section_plan_item: ReportSectionPlanItem | Mapping[str, Any],
) -> Mapping[str, Any]:
    if isinstance(section_plan_item, ReportSectionPlanItem):
        return section_plan_item.as_llm_input()
    if isinstance(section_plan_item, Mapping):
        return section_plan_item
    raise TypeError("section_plan_item must be a report section plan item or mapping")


def _unique_refs(refs: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for ref in refs:
        if ref and ref not in unique:
            unique.append(ref)
    return unique


def _format_error_path(error: ValidationError) -> str:
    if not error.path:
        return "$"
    return ".".join(str(part) for part in error.path)


__all__ = [
    "ReportSectionValidationError",
    "ReportSectionValidationIssue",
    "ReportSectionValidationResult",
    "ReportSectionValidator",
    "validate_report_section",
]
