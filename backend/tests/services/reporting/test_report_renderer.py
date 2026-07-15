"""Tests for engagement report Markdown snapshot rendering."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.services.reporting.contracts import (
    GENERATION_METADATA_RENDERER_VERSION_KEY,
    REPORT_SECTION_SCHEMA_VERSION,
)
from backend.services.reporting.report_renderer import (
    REPORT_RENDERER_VERSION,
    render_engagement_report_markdown,
)
from backend.services.reporting.report_evidence_timeline import (
    ReportEvidenceTimelineItem,
)


def test_renderer_outputs_title_and_sections_in_plan_order() -> None:
    sections = [
        _section(
            section_id="detailed_findings",
            section_type="findings",
            title="Detailed Findings",
            content_markdown="Detailed finding overview.",
        ),
        _section(
            section_id="executive_summary",
            section_type="summary",
            title="Executive Summary",
            content_markdown="Executive report summary.",
        ),
    ]

    result = render_engagement_report_markdown(
        title="Acme Engagement Report",
        report_type="pentest",
        sections=sections,
        evidence_timeline=(),
    )

    assert result.markdown_snapshot.startswith("# Acme Engagement Report\n")
    assert result.markdown_snapshot.index("## Executive Summary") < (
        result.markdown_snapshot.index("## Detailed Findings")
    )
    assert "Executive report summary." in result.markdown_snapshot
    assert "Detailed finding overview." in result.markdown_snapshot


def test_renderer_renders_finding_blocks_without_internal_refs() -> None:
    result = render_engagement_report_markdown(
        title="Acme Engagement Report",
        report_type="pentest",
        sections=[
            _section(
                section_id="detailed_findings",
                section_type="findings",
                title="Detailed Findings",
                blocks=[_finding_block()],
            )
        ],
        evidence_timeline=(),
    )

    markdown = result.markdown_snapshot
    assert "### Weak TLS configuration" in markdown
    assert "- Severity: medium" in markdown
    assert "- Confidence: high" in markdown
    assert "- Affected assets: `app.example.test`" in markdown
    assert "TLS configuration allows weak ciphers." in markdown
    assert "An attacker may downgrade transport security." in markdown
    assert "Disable weak ciphers and redeploy." in markdown
    assert "Source refs" not in markdown
    assert "memo-supported" not in markdown
    assert "finding:1" not in markdown
    assert "evidence:web:1" not in markdown


def test_renderer_renders_appendix_evidence_timeline_without_internal_refs() -> None:
    result = render_engagement_report_markdown(
        title="Acme Engagement Report",
        report_type="pentest",
        sections=[
            _section(
                section_id="detailed_findings",
                section_type="findings",
                title="Detailed Findings",
                blocks=[_finding_block()],
            ),
            _section(
                section_id="appendix_evidence_index",
                section_type="appendix",
                title="Appendix / Evidence Index",
                content_markdown="Sensitive credential marker should not render.",
                blocks=[
                    {
                        **_finding_block(),
                        "block_type": "appendix_note",
                        "title": "Sensitive credential marker",
                        "content_markdown": "Raw credential marker",
                    }
                ],
            )
        ],
        evidence_timeline=(
            _timeline_item(),
        ),
    )

    markdown = result.markdown_snapshot
    assert "## Appendix / Evidence Index" in markdown
    assert "### Evidence Index" in markdown
    assert "1. Evidence `abc` - Nmap against `app.example.test:443`" in markdown
    assert "Recorded: 2026-06-09 12:30 UTC" in markdown
    assert "Evidence type:" not in markdown
    assert "Result:" not in markdown
    assert "Output:" not in markdown
    assert "443/tcp open https" not in markdown
    assert "Source refs" not in markdown
    assert "memo-supported" not in markdown
    assert "finding:1" not in markdown
    assert "evidence_archive:" not in markdown
    assert "Sensitive credential marker" not in markdown
    assert "Raw credential marker" not in markdown


def test_renderer_records_version_metadata_and_is_deterministic() -> None:
    sections = [
        _section(
            section_id="executive_summary",
            section_type="summary",
            title="Executive Summary",
            content_markdown="Executive report summary.",
        )
    ]

    first = render_engagement_report_markdown(
        title="Acme Engagement Report",
        report_type="pentest",
        sections=deepcopy(sections),
        evidence_timeline=(_timeline_item(),),
    )
    second = render_engagement_report_markdown(
        title="Acme Engagement Report",
        report_type="pentest",
        sections=deepcopy(sections),
        evidence_timeline=(_timeline_item(),),
    )

    assert first.generation_metadata == {
        GENERATION_METADATA_RENDERER_VERSION_KEY: REPORT_RENDERER_VERSION
    }
    assert first.markdown_snapshot == second.markdown_snapshot
    assert first.generation_metadata == second.generation_metadata


def _section(
    *,
    section_id: str,
    section_type: str,
    title: str,
    content_markdown: str = "",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SECTION_SCHEMA_VERSION,
        "section_id": section_id,
        "section_type": section_type,
        "title": title,
        "status": "ready",
        "content_markdown": content_markdown,
        "blocks": blocks or [],
        "source_refs": _source_refs(),
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
        "source_refs": _source_refs(),
    }


def _source_refs() -> dict[str, list[str]]:
    return {
        "task_memo_ids": ["memo-supported"],
        "knowledge_refs": ["finding:1"],
        "evidence_refs": ["evidence_archive:abc"],
    }


def _timeline_item() -> ReportEvidenceTimelineItem:
    return ReportEvidenceTimelineItem(
        ref="evidence_archive:abc",
        order=1,
        observed_at="2026-06-09T12:30:00+00:00",
        created_at="2026-06-09T12:31:00+00:00",
        source_tool="Nmap",
        target="app.example.test:443",
        evidence_type="service",
        summary="nmap service scan identified exposed HTTPS.",
        excerpt="443/tcp open https",
    )
