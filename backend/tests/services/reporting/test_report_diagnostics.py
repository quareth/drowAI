"""Tests for safe reporting diagnostics log and metric projections."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from backend.services.reporting.report_diagnostics import ReportDiagnostics
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationIssue,
)


def test_section_validation_failed_logs_issue_code_and_path_only(
    caplog,
    monkeypatch,
) -> None:
    metrics: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "backend.services.reporting.report_diagnostics.safe_inc",
        lambda name, value=1: metrics.append((name, value)),
    )
    issue = ReportSectionValidationIssue(
        code="customer_markdown_internal_identifier",
        path="blocks.0.content_markdown",
        message=(
            "Generated text leaked evidence_archive:secret and "
            "memo 123e4567-e89b-12d3-a456-426614174000."
        ),
    )

    with caplog.at_level(
        logging.WARNING,
        logger="backend.services.reporting.report_diagnostics",
    ):
        ReportDiagnostics().section_validation_failed(
            job_id="job-1",
            report_id="report-1",
            engagement_id=17,
            report_type="vulnerability_assessment",
            section_id="appendix_evidence_index",
            section_order=8,
            issues=(issue,),
        )

    log_text = caplog.text
    assert "customer_markdown_internal_identifier" in log_text
    assert "blocks.0.content_markdown" in log_text
    assert "evidence_archive:secret" not in log_text
    assert "123e4567-e89b-12d3-a456-426614174000" not in log_text
    assert metrics == [
        ("reporting.report_generation.section_validation_failed_count", 1)
    ]


def test_context_built_logs_counts_not_source_identifiers(caplog) -> None:
    context = SimpleNamespace(
        selected_memos=("memo-secret-uuid",),
        selected_tasks=(1, 2),
        compatible_knowledge_refs=("knowledge_secret",),
        compatible_evidence_refs=("evidence_archive:secret",),
        candidate_policy=SimpleNamespace(include_candidate_findings=False),
    )

    with caplog.at_level(
        logging.INFO,
        logger="backend.services.reporting.report_diagnostics",
    ):
        ReportDiagnostics().context_built(
            job_id="job-1",
            report_id="report-1",
            engagement_id=17,
            report_type="pentest",
            context=context,
        )

    log_text = caplog.text
    assert "selected_memos=1" in log_text
    assert "knowledge_refs=1" in log_text
    assert "evidence_refs=1" in log_text
    assert "memo-secret-uuid" not in log_text
    assert "knowledge_secret" not in log_text
    assert "evidence_archive:secret" not in log_text
