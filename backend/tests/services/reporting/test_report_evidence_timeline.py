"""Tests for presentation-safe report evidence timeline construction."""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.reporting.report_context_builder import ReportEvidenceRef
from backend.services.reporting.report_evidence_timeline import (
    build_report_evidence_timeline,
)


def test_timeline_orders_by_observed_time_created_time_and_input_order() -> None:
    context = _context(
        [
            _evidence(
                ref="evidence_archive:late",
                observed_at="2026-06-09T12:40:00+00:00",
                created_at="2026-06-09T12:41:00+00:00",
                summary="Late evidence",
            ),
            _evidence(
                ref="evidence_archive:first",
                observed_at="2026-06-09T12:20:00+00:00",
                created_at="2026-06-09T12:30:00+00:00",
                summary="First evidence",
            ),
            _evidence(
                ref="evidence_archive:same-a",
                observed_at=None,
                created_at="2026-06-09T12:50:00+00:00",
                summary="Same timestamp A",
            ),
            _evidence(
                ref="evidence_archive:same-b",
                observed_at=None,
                created_at="2026-06-09T12:50:00+00:00",
                summary="Same timestamp B",
            ),
        ]
    )

    timeline = build_report_evidence_timeline(context)

    assert [item.ref for item in timeline] == [
        "evidence_archive:first",
        "evidence_archive:late",
        "evidence_archive:same-a",
        "evidence_archive:same-b",
    ]
    assert [item.order for item in timeline] == [1, 2, 3, 4]


def test_timeline_removes_internal_refs_from_presentation_text() -> None:
    context = _context(
        [
            _evidence(
                ref="evidence_archive:secret",
                observed_at="2026-06-09T12:20:00+00:00",
                created_at="2026-06-09T12:21:00+00:00",
                summary="Observed evidence_archive:secret for knowledge_finding:42",
                excerpt="source_watermark_hash should not appear",
            )
        ]
    )

    item = build_report_evidence_timeline(context)[0]
    presentation = " ".join(
        [
            item.source_tool,
            item.target or "",
            item.evidence_type,
            item.summary,
            item.excerpt,
        ]
    )

    assert "evidence_archive:" not in presentation
    assert "knowledge_finding:" not in presentation
    assert "source_watermark" not in presentation
    assert item.source_tool == "Nmap"


def test_timeline_translates_internal_tool_identifier() -> None:
    context = _context(
        [
            _evidence(
                ref="evidence_archive:tool",
                observed_at="2026-06-09T12:20:00+00:00",
                created_at="2026-06-09T12:21:00+00:00",
                summary="Discovery evidence",
                source_tool="information_gathering.network_discovery.fping",
            )
        ]
    )

    item = build_report_evidence_timeline(context)[0]

    assert item.source_tool == "fping"
    assert "information_gathering" not in item.source_tool


def _context(evidence_refs: list[ReportEvidenceRef]) -> SimpleNamespace:
    return SimpleNamespace(
        compatible_evidence_refs=tuple(evidence_refs),
        allowed_task_memo_ids=frozenset({"memo-1"}),
        allowed_knowledge_refs=frozenset({"knowledge_finding:42"}),
        allowed_evidence_refs=frozenset(item.ref for item in evidence_refs),
        source_watermark=SimpleNamespace(hash="source-watermark-hash"),
    )


def _evidence(
    *,
    ref: str,
    observed_at: str | None,
    created_at: str | None,
    summary: str,
    excerpt: str = "443/tcp open https",
    source_tool: str = "nmap",
) -> ReportEvidenceRef:
    return ReportEvidenceRef(
        ref=ref,
        task_id=10,
        evidence_type="service",
        summary=summary,
        excerpt=excerpt,
        source_tool=source_tool,
        target="app.example.test:443",
        observed_at=observed_at,
        created_at=created_at,
        linked_knowledge_refs=("knowledge_finding:42",),
    )
