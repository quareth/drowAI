"""Tests for generated engagement report section validation."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pytest

from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED,
    REPORT_SECTION_SCHEMA_VERSION,
)
from backend.services.reporting.report_context_builder import (
    ReportCandidateFindingPolicy,
    ReportContext,
    ReportEngagementMetadata,
    ReportEvidenceRef,
    ReportKnowledgeRef,
    ReportMemoPartitions,
    ReportMemoWatermark,
    ReportSelectedMemoBody,
    ReportSelectedMemoContext,
    ReportSelectedTaskMetadata,
    ReportSourceWatermark,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
    validate_report_section,
)


def test_validator_accepts_and_sanitizes_ready_section_payload() -> None:
    section = get_report_section_plan("pentest").sections[0]
    payload = _valid_payload(section)
    payload["source_refs"]["evidence_refs"] = [" evidence:web:1 ", "evidence:web:1"]

    result = validate_report_section(
        payload=payload,
        context=_report_context(),
        section_plan_item=section,
    )

    assert result.metadata["validation_status"] == "passed"
    assert result.payload["section_id"] == "executive_summary"
    assert result.payload["source_refs"]["evidence_refs"] == ["evidence:web:1"]


def test_validator_rejects_schema_and_plan_or_status_failures() -> None:
    section = get_report_section_plan("pentest").sections[0]
    missing_title = _valid_payload(section)
    missing_title.pop("title")

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=missing_title,
            context=_report_context(),
            section_plan_item=section,
        )

    error = exc_info.value
    assert error.reason == REPORT_GENERATION_ERROR_SECTION_VALIDATION_FAILED
    assert _issue_codes(error) == {"schema_invalid"}
    assert "Supported executive summary" not in error.safe_message
    assert "Supported executive summary" not in str(error.metadata)

    mismatched = _valid_payload(section)
    mismatched["section_id"] = "wrong"
    mismatched["section_type"] = "findings"
    mismatched["title"] = "Wrong"
    mismatched["status"] = "needs_review"

    with pytest.raises(ReportSectionValidationError) as mismatch_info:
        validate_report_section(
            payload=mismatched,
            context=_report_context(),
            section_plan_item=section,
        )

    assert _issue_codes(mismatch_info.value) == {
        "section_id_mismatch",
        "section_type_mismatch",
        "title_mismatch",
        "section_status_not_ready",
    }


def test_validator_rejects_unknown_and_scope_mismatched_refs() -> None:
    section = get_report_section_plan("pentest").sections[0]
    payload = _valid_payload(section)
    payload["source_refs"] = {
        "task_memo_ids": ["memo-missing"],
        "knowledge_refs": ["finding:1"],
        "evidence_refs": ["evidence:web:1"],
    }
    context = replace(
        _report_context(),
        compatible_knowledge_refs=(
            replace(_knowledge_ref(), task_id=999),
        ),
        compatible_evidence_refs=(
            replace(_evidence_ref(), task_id=999),
        ),
    )

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=payload,
            context=context,
            section_plan_item=section,
        )

    assert _issue_codes(exc_info.value) == {
        "unknown_task_memo_ref",
        "knowledge_ref_scope_mismatch",
        "evidence_ref_scope_mismatch",
    }


def test_validator_rejects_unsupported_grounding_policy_cases() -> None:
    section = get_report_section_plan("pentest").sections[0]
    transcript_only = _valid_payload(section)
    transcript_only["source_refs"] = {
        "task_memo_ids": ["memo-supported"],
        "knowledge_refs": [],
        "evidence_refs": [],
    }

    with pytest.raises(ReportSectionValidationError) as transcript_info:
        validate_report_section(
            payload=transcript_only,
            context=_report_context(),
            section_plan_item=section,
        )

    assert _issue_codes(transcript_info.value) == {
        "transcript_only_reportable_content"
    }

    limited_ref = _valid_payload(section)
    limited_ref["source_refs"]["task_memo_ids"] = ["memo-limited"]

    with pytest.raises(ReportSectionValidationError) as limited_info:
        validate_report_section(
            payload=limited_ref,
            context=_report_context(),
            section_plan_item=section,
        )

    assert _issue_codes(limited_info.value) == {"limited_memo_ref_misuse"}


def test_validator_rejects_candidate_refs_when_policy_excludes_candidates() -> None:
    section = get_report_section_plan("pentest").sections[0]
    payload = _valid_payload(section)
    payload["source_refs"]["knowledge_refs"] = ["finding:candidate"]
    context = replace(
        _report_context(),
        compatible_knowledge_refs=(
            _knowledge_ref(ref="finding:candidate", authoritative=False),
        ),
        allowed_knowledge_refs=frozenset({"finding:candidate"}),
    )

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=payload,
            context=context,
            section_plan_item=section,
        )

    assert _issue_codes(exc_info.value) == {"candidate_ref_disallowed"}


def test_validator_rejects_candidate_only_evidence_when_candidates_filtered() -> None:
    section = get_report_section_plan("pentest").sections[0]
    payload = _valid_payload(section)
    payload["source_refs"]["knowledge_refs"] = []
    context = replace(
        _report_context(),
        compatible_evidence_refs=(
            ReportEvidenceRef(
                ref="evidence:web:1",
                task_id=10,
                evidence_type="http_response",
                summary="Candidate-only evidence",
                excerpt="bounded excerpt",
                source_tool="http_probe",
                target="https://example.test/admin",
                observed_at="2026-06-09T12:20:00+00:00",
                created_at="2026-06-09T12:21:00+00:00",
                linked_knowledge_refs=("finding:candidate",),
            ),
        ),
        candidate_only_knowledge_refs=frozenset({"finding:candidate"}),
    )

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=payload,
            context=context,
            section_plan_item=section,
        )

    assert _issue_codes(exc_info.value) == {"candidate_ref_disallowed"}


def test_validator_rejects_internal_refs_in_customer_markdown() -> None:
    section = get_report_section_plan("pentest").sections[0]
    payload = _valid_payload(section)
    payload["content_markdown"] = (
        "Management summary cites evidence_archive:secret and memo-supported."
    )

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=payload,
            context=_report_context(),
            section_plan_item=section,
        )

    assert _issue_codes(exc_info.value) == {
        "customer_markdown_internal_identifier"
    }
    assert "evidence_archive:secret" not in str(exc_info.value.metadata)
    assert "memo-supported" not in str(exc_info.value.metadata)


def test_validator_rejects_finding_block_without_reportable_refs() -> None:
    section = get_report_section_plan("pentest").sections[3]
    payload = _valid_payload(section)
    payload["blocks"] = [_finding_block()]
    payload["blocks"][0]["source_refs"] = {
        "task_memo_ids": ["memo-supported"],
        "knowledge_refs": [],
        "evidence_refs": [],
    }

    with pytest.raises(ReportSectionValidationError) as exc_info:
        validate_report_section(
            payload=payload,
            context=_report_context(),
            section_plan_item=section,
        )

    assert "schema_invalid" in _issue_codes(exc_info.value)
    assert "finding_block_missing_reportable_ref" in _issue_codes(exc_info.value)


def test_limitations_section_can_cite_limited_memo_without_reportable_refs() -> None:
    section = get_report_section_plan("pentest").sections[5]
    payload = _valid_payload(section)
    payload["source_refs"] = {
        "task_memo_ids": ["memo-limited"],
        "knowledge_refs": [],
        "evidence_refs": [],
    }

    result = validate_report_section(
        payload=payload,
        context=_report_context(),
        section_plan_item=section,
    )

    assert result.payload["section_id"] == "limitations"


def _valid_payload(section: Any) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SECTION_SCHEMA_VERSION,
        "section_id": section.section_id,
        "section_type": section.section_type,
        "title": section.title,
        "status": "ready",
        "content_markdown": "Supported executive summary.",
        "blocks": [],
        "source_refs": {
            "task_memo_ids": ["memo-supported"],
            "knowledge_refs": ["finding:1"],
            "evidence_refs": ["evidence:web:1"],
        },
        "unsupported_notes": [],
        "generation_notes": [],
    }


def _finding_block() -> dict[str, Any]:
    return {
        "block_id": "finding-1",
        "block_type": "finding",
        "title": "Weak TLS configuration",
        "severity": "medium",
        "confidence": "high",
        "affected_assets": ["app.example.test"],
        "content_markdown": "TLS configuration allows weak ciphers.",
        "impact_markdown": "An attacker may downgrade transport security.",
        "remediation_markdown": "Disable weak ciphers and redeploy.",
        "source_refs": {
            "task_memo_ids": ["memo-supported"],
            "knowledge_refs": ["finding:1"],
            "evidence_refs": ["evidence:web:1"],
        },
    }


def _report_context() -> ReportContext:
    candidate_policy = ReportCandidateFindingPolicy(include_candidate_findings=False)
    return ReportContext(
        engagement=ReportEngagementMetadata(
            engagement_id=1,
            tenant_id=2,
            user_id=3,
            name="Acme External Assessment",
            description="Internet-facing systems",
            status="active",
            created_at="2026-06-09T12:00:00+00:00",
        ),
        report_type="pentest",
        selected_memos=(
            ReportSelectedMemoContext(
                memo_id="memo-supported",
                task_id=10,
                version=2,
                memo_mode="supported",
                generated_at="2026-06-09T12:05:00+00:00",
                summary="supported memo",
                body=ReportSelectedMemoBody(
                    actions_performed=(),
                    reportable_observations=(),
                    possible_findings=(),
                    limitations=(),
                    unsupported_notes=(),
                    evidence_refs=("evidence:web:1",),
                    knowledge_refs=("finding:1",),
                ),
                source_watermark=MappingProxyType({"hash": "a"}),
            ),
            ReportSelectedMemoContext(
                memo_id="memo-limited",
                task_id=11,
                version=1,
                memo_mode="limited",
                generated_at="2026-06-09T12:10:00+00:00",
                summary="limited memo",
                body=ReportSelectedMemoBody(
                    actions_performed=(),
                    reportable_observations=(),
                    possible_findings=(),
                    limitations=(MappingProxyType({"summary": "No credentials"}),),
                    unsupported_notes=(),
                    evidence_refs=(),
                    knowledge_refs=(),
                ),
                source_watermark=MappingProxyType({"hash": "b"}),
            ),
        ),
        memo_partitions=ReportMemoPartitions(
            supported_memo_ids=("memo-supported",),
            limited_memo_ids=("memo-limited",),
        ),
        selected_tasks=(
            ReportSelectedTaskMetadata(
                task_id=10,
                memo_id="memo-supported",
                name="External web test",
                description="External checks",
                scope="external",
                status="stopped",
                created_at=None,
                stopped_at=None,
            ),
            ReportSelectedTaskMetadata(
                task_id=11,
                memo_id="memo-limited",
                name="Credentialed review",
                description="Credentialed checks",
                scope="internal",
                status="stopped",
                created_at=None,
                stopped_at=None,
            ),
        ),
        compatible_knowledge_refs=(_knowledge_ref(),),
        compatible_evidence_refs=(_evidence_ref(),),
        candidate_policy=candidate_policy,
        source_watermark=ReportSourceWatermark(
            schema_version=1,
            report_type="pentest",
            candidate_policy=candidate_policy,
            selected_memos=(
                ReportMemoWatermark(
                    memo_id="memo-supported",
                    task_id=10,
                    version=2,
                    source_watermark=MappingProxyType({"hash": "a"}),
                ),
                ReportMemoWatermark(
                    memo_id="memo-limited",
                    task_id=11,
                    version=1,
                    source_watermark=MappingProxyType({"hash": "b"}),
                ),
            ),
            hash_algorithm="sha256",
            hash="report-input-hash",
            job_source_watermark=MappingProxyType({"hash": "report-input-hash"}),
            generation_metadata=MappingProxyType(
                {"source_watermark_hash": "report-input-hash"}
            ),
        ),
        allowed_task_memo_ids=frozenset({"memo-supported", "memo-limited"}),
        allowed_knowledge_refs=frozenset({"finding:1"}),
        allowed_evidence_refs=frozenset({"evidence:web:1"}),
        truncated=False,
    )


def _knowledge_ref(
    ref: str = "finding:1",
    *,
    authoritative: bool = True,
) -> ReportKnowledgeRef:
    return ReportKnowledgeRef(
        ref=ref,
        task_id=10,
        record_type="finding",
        summary="Missing access control",
        authoritative=authoritative,
        source_execution_ids=("exec-1",),
        evidence_archive_refs=("evidence:web:1",),
    )


def _evidence_ref() -> ReportEvidenceRef:
    return ReportEvidenceRef(
        ref="evidence:web:1",
        task_id=10,
        evidence_type="http_response",
        summary="Admin endpoint returned without authentication",
        excerpt="bounded excerpt",
        source_tool="http_probe",
        target="https://example.test/admin",
        observed_at="2026-06-09T12:20:00+00:00",
        created_at="2026-06-09T12:21:00+00:00",
        linked_knowledge_refs=("finding:1",),
    )


def _issue_codes(error: ReportSectionValidationError) -> set[str]:
    return {issue.code for issue in error.issues}
