"""Tests for reporting Pydantic schema contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.schemas.reporting import (
    CurrentEngagementReportResponse,
    EngagementReportGenerationRequest,
    EngagementReportGenerationResponse,
    EngagementReportHistoryItem,
    EngagementReportJobStatusResponse,
    EngagementReportReadResponse,
    EngagementReportSection,
    EngagementReportSectionBlock,
    EngagementReportSectionSourceRefs,
    EngagementReportSummary,
    EngagementReportingInputsResponse,
    ReportingInputTaskRow,
    ReportingSourceCounts,
    SourceWatermarkSnapshot,
    TaskClosureMemoBody,
    TaskClosureMemoHistoryResponse,
    TaskClosureMemoPrepareRequest,
    TaskClosureMemoPrepareResponse,
    TaskClosureMemoReadResponse,
    TaskClosureMemoSummary,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_inventory_response_accepts_task_row_contract() -> None:
    response = EngagementReportingInputsResponse(
        engagement_id=45,
        tasks=[
            ReportingInputTaskRow(
                task_id=123,
                task_name="External web enumeration",
                task_status="stopped",
                runtime_retired=True,
                is_reportable=True,
                is_preparable=True,
                memo_mode="supported",
                not_preparable_reason=None,
                input_state="not_prepared",
                current_memo=None,
                source_watermark=SourceWatermarkSnapshot(
                    last_chat_message_id=812,
                    last_turn_sequence=17,
                    latest_tool_execution_id="execution-uuid",
                    latest_evidence_created_at=_now(),
                    latest_knowledge_observed_at=_now(),
                ),
                counts=ReportingSourceCounts(
                    evidence=12,
                    canonical_findings=3,
                    candidate_findings=1,
                ),
                candidate_findings_require_explicit_inclusion=True,
            )
        ],
    )

    row = response.tasks[0]
    assert row.task_id == 123
    assert row.runtime_retired is True
    assert row.memo_mode == "supported"
    assert row.input_state == "not_prepared"
    assert row.counts.candidate_findings == 1
    assert row.latest_memo_attempt is None


def test_report_type_and_report_status_are_validated_from_contracts() -> None:
    report = EngagementReportReadResponse(
        id=uuid4(),
        schema_version="1",
        engagement_id=45,
        report_type="pentest",
        version=1,
        status="ready",
        is_current=True,
        title="ACME Pentest Report",
        created_at=_now(),
        updated_at=_now(),
    )

    response = CurrentEngagementReportResponse(
        engagement_id=45,
        report_type="pentest",
        report=report,
    )

    assert response.report is report

    with pytest.raises(ValidationError):
        CurrentEngagementReportResponse(
            engagement_id=45,
            report_type="unsupported",
            report=None,
        )

    with pytest.raises(ValidationError):
        EngagementReportSummary(
            id=uuid4(),
            engagement_id=45,
            report_type="pentest",
            version=1,
            status="draft",
            is_current=False,
            title="Draft",
            created_at=_now(),
            updated_at=_now(),
        )

    with pytest.raises(ValidationError):
        TaskClosureMemoSummary(
            id=uuid4(),
            version=1,
            status="complete",
            memo_mode="supported",
            is_current=False,
            source_watermark=SourceWatermarkSnapshot(),
            created_at=_now(),
            updated_at=_now(),
        )


def test_job_status_validates_mvp_job_lifecycle_values() -> None:
    response = EngagementReportJobStatusResponse(
        id=uuid4(),
        engagement_id=45,
        report_id=None,
        report_type="vulnerability_assessment",
        status="queued",
        generation_phase="sections",
        selected_task_memo_ids=[str(uuid4())],
        include_candidate_findings=False,
        current_section_id=None,
        completed_sections=[],
        total_sections=0,
        attempt_count=0,
        max_attempts=3,
        created_at=_now(),
        updated_at=_now(),
    )

    assert response.status == "queued"

    with pytest.raises(ValidationError):
        EngagementReportJobStatusResponse(
            id=uuid4(),
            engagement_id=45,
            report_id=None,
            report_type="vulnerability_assessment",
            status="running",
            generation_phase="sections",
            include_candidate_findings=False,
            total_sections=0,
            attempt_count=0,
            max_attempts=3,
            created_at=_now(),
            updated_at=_now(),
        )


def test_report_history_item_excludes_full_report_content_fields() -> None:
    report_id = uuid4()
    item = EngagementReportHistoryItem(
        report_id=report_id,
        engagement_id=45,
        report_type="pentest",
        version=1,
        status="ready",
        is_current=True,
        title="ACME Pentest Report",
        source_task_memo_ids=[str(uuid4())],
        source_knowledge_refs=[
            {
                "ref": "knowledge_finding:1",
                "task_id": 1,
                "record_type": "finding",
                "authoritative": True,
            }
        ],
        source_evidence_refs=[
            {
                "ref": "evidence_archive:1",
                "task_id": 1,
                "evidence_type": "service",
                "source_tool": "nmap",
            }
        ],
        created_at=_now(),
        updated_at=_now(),
    )

    payload = item.model_dump()
    assert payload["report_id"] == report_id
    assert payload["source_task_memo_ids"]
    assert "sections" not in payload
    assert "markdown_snapshot" not in payload


def test_report_generation_request_uses_selected_memo_ids_contract() -> None:
    memo_id = uuid4()

    request = EngagementReportGenerationRequest(
        report_type="pentest",
        selected_task_memo_ids=[memo_id],
    )

    assert request.selected_task_memo_ids == [memo_id]
    assert request.include_candidate_findings is False
    assert request.force_regenerate is False
    assert "selected_task_ids" not in EngagementReportGenerationRequest.model_fields

    with pytest.raises(ValidationError):
        EngagementReportGenerationRequest(report_type="pentest")

    with pytest.raises(ValidationError):
        EngagementReportGenerationRequest(
            report_type="pentest",
            selected_task_memo_ids=[],
        )

    with pytest.raises(ValidationError):
        EngagementReportGenerationRequest(
            report_type="pentest",
            selected_task_memo_ids=[uuid4() for _ in range(101)],
        )


def test_report_generation_response_accepts_job_or_ready_report_status() -> None:
    queued = EngagementReportGenerationResponse(
        job_id=uuid4(),
        status="queued",
    )
    ready = EngagementReportGenerationResponse(
        report_id=uuid4(),
        status="ready",
    )

    assert queued.job_id is not None
    assert queued.status == "queued"
    assert ready.report_id is not None
    assert ready.status == "ready"

    with pytest.raises(ValidationError):
        EngagementReportGenerationResponse(status="cancelled")


def test_report_sections_and_blocks_validate_required_contract_fields() -> None:
    source_refs = EngagementReportSectionSourceRefs(
        task_memo_ids=[str(uuid4())],
        knowledge_refs=["finding-uuid"],
        evidence_refs=["evidence-uuid"],
    )
    block = EngagementReportSectionBlock(
        block_id="finding-block-1",
        block_type="finding",
        title="Exposed Administrative Interface",
        severity="high",
        confidence="medium",
        affected_assets=["10.10.10.5"],
        content_markdown="Finding narrative.",
        impact_markdown="Business impact.",
        remediation_markdown="Remediation guidance.",
        source_refs=source_refs,
    )
    section = EngagementReportSection(
        schema_version="report_section.v1",
        section_id="detailed_findings",
        section_type="findings",
        title="Detailed Findings",
        status="ready",
        content_markdown="The following findings were identified.",
        blocks=[block],
        source_refs=source_refs,
        unsupported_notes=[],
        generation_notes=[],
    )

    assert section.blocks[0].source_refs.evidence_refs == ["evidence-uuid"]
    assert section.source_refs.task_memo_ids == [str(source_refs.task_memo_ids[0])]

    with pytest.raises(ValidationError):
        EngagementReportSection(
            schema_version="report_section.v1",
            section_id="executive_summary",
            section_type="narrative",
            title="Executive Summary",
            status="ready",
            content_markdown="Summary.",
            blocks=[],
            unsupported_notes=[],
            generation_notes=[],
        )

    with pytest.raises(ValidationError):
        EngagementReportSectionBlock(
            block_id="finding-block-1",
            block_type="unsupported",
            title="Unsupported block",
            severity="low",
            confidence="low",
            affected_assets=[],
            content_markdown="Finding narrative.",
            impact_markdown="Business impact.",
            remediation_markdown="Remediation guidance.",
            source_refs=source_refs,
        )


def test_direct_report_read_response_uses_structured_sections() -> None:
    report = EngagementReportReadResponse(
        id=uuid4(),
        schema_version="1",
        engagement_id=45,
        report_type="pentest",
        version=1,
        status="ready",
        is_current=True,
        title="ACME Internal Pentest Report",
        sections=[
            {
                "schema_version": "report_section.v1",
                "section_id": "executive_summary",
                "section_type": "narrative",
                "title": "Executive Summary",
                "status": "ready",
                "content_markdown": "Generated section content.",
                "blocks": [],
                "source_refs": {
                    "task_memo_ids": [str(uuid4())],
                    "knowledge_refs": ["finding-uuid"],
                    "evidence_refs": [],
                },
                "unsupported_notes": [],
                "generation_notes": [],
            }
        ],
        markdown_snapshot="# Executive Summary\n\nGenerated section content.",
        source_task_memo_ids=[str(uuid4())],
        source_knowledge_refs=[
            {
                "ref": "knowledge_finding:finding-uuid",
                "task_id": 1,
                "record_type": "finding",
                "authoritative": True,
            }
        ],
        source_evidence_refs=[],
        created_at=_now(),
        updated_at=_now(),
        generated_at=_now(),
    )

    assert report.sections[0].section_id == "executive_summary"
    assert report.sections[0].blocks == []


def _supported_memo_body() -> dict[str, object]:
    return {
        "task_name": "External web enumeration",
        "summary": "The task identified externally exposed services.",
        "include_in_report_recommendation": {
            "include": True,
            "reason": "The memo contains source-backed observations.",
        },
        "actions_performed": [
            {
                "text": "Ran service detection against the scoped host.",
                "source": "transcript",
            }
        ],
        "reportable_observations": [
            {
                "text": "The target exposed HTTP on TCP 80.",
                "confidence": "high",
                "evidence_refs": ["evidence-1"],
                "knowledge_refs": ["service-80"],
            }
        ],
        "possible_findings": [
            {
                "title": "Outdated web server version exposed",
                "severity_hint": "low",
                "confidence": "medium",
                "evidence_refs": ["evidence-1"],
                "knowledge_refs": ["finding-1"],
            }
        ],
        "limitations": [{"text": "Authenticated testing was not performed."}],
        "unsupported_notes": [
            {
                "text": "The transcript mentioned SQL injection, but no source confirmed it.",
            }
        ],
        "evidence_refs": ["evidence-1"],
        "knowledge_refs": ["service-80", "finding-1"],
    }


def test_task_closure_memo_body_accepts_supported_and_limited_shapes() -> None:
    supported = TaskClosureMemoBody.model_validate(_supported_memo_body())
    assert supported.reportable_observations[0].evidence_refs == ["evidence-1"]
    assert supported.possible_findings[0].knowledge_refs == ["finding-1"]

    limited = TaskClosureMemoBody(
        task_name="Manual review",
        summary="The task completed useful manual review without durable source refs.",
        include_in_report_recommendation={
            "include": False,
            "reason": "No source-backed reportable observations were produced.",
        },
        actions_performed=[
            {
                "text": "Reviewed the target scope and noted missing credentials.",
                "source": "transcript",
            }
        ],
        limitations=[{"text": "No authenticated evidence was collected."}],
        unsupported_notes=[{"text": "Transcript-only notes require follow-up."}],
    )

    assert limited.reportable_observations == []
    assert limited.possible_findings == []


def test_task_closure_memo_body_requires_refs_for_reportable_items() -> None:
    body = _supported_memo_body()
    body["reportable_observations"] = [
        {
            "text": "The target exposed HTTP on TCP 80.",
            "confidence": "high",
            "evidence_refs": [],
            "knowledge_refs": [],
        }
    ]

    with pytest.raises(ValidationError):
        TaskClosureMemoBody.model_validate(body)

    body = _supported_memo_body()
    body["possible_findings"] = [
        {
            "title": "Outdated web server version exposed",
            "severity_hint": "low",
            "confidence": "medium",
            "evidence_refs": [" "],
            "knowledge_refs": [],
        }
    ]

    with pytest.raises(ValidationError):
        TaskClosureMemoBody.model_validate(body)


def test_task_closure_memo_responses_serialize_attempt_rows_without_generation_metadata() -> (
    None
):
    row = SimpleNamespace(
        id=uuid4(),
        schema_version="1",
        engagement_id=45,
        task_id=123,
        version=1,
        status="ready",
        memo_mode="supported",
        is_current=True,
        source_watermark={
            "schema_version": "1",
            "sources": {"chat_messages": {"latest_id": 10}},
        },
        memo=_supported_memo_body(),
        generation_metadata={"provider": "example", "prompt": "must not be exposed"},
        error_message=None,
        created_at=_now(),
        updated_at=_now(),
        generated_at=_now(),
    )

    memo = TaskClosureMemoReadResponse.model_validate(row)
    assert memo.body is not None
    assert memo.body.summary.startswith("The task identified")
    assert not hasattr(memo, "generation_metadata")
    assert memo.model_dump()["body"]["evidence_refs"] == ["evidence-1"]

    response = TaskClosureMemoPrepareResponse(task_id=123, memo=memo)
    history = TaskClosureMemoHistoryResponse(task_id=123, items=[memo])

    assert response.memo.id == row.id
    assert history.items[0].version == 1


def test_task_closure_memo_request_and_row_status_values_are_validated() -> None:
    assert TaskClosureMemoPrepareRequest().regenerate is False
    assert TaskClosureMemoPrepareRequest(regenerate=True).regenerate is True

    with pytest.raises(ValidationError):
        TaskClosureMemoReadResponse(
            id=uuid4(),
            schema_version="1",
            engagement_id=45,
            task_id=123,
            version=1,
            status="complete",
            memo_mode="supported",
            is_current=False,
            created_at=_now(),
            updated_at=_now(),
        )

    with pytest.raises(ValidationError):
        TaskClosureMemoReadResponse(
            id=uuid4(),
            schema_version="1",
            engagement_id=45,
            task_id=123,
            version=1,
            status="ready",
            memo_mode="draft",
            is_current=False,
            created_at=_now(),
            updated_at=_now(),
        )
