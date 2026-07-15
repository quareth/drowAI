"""Build presentation-safe evidence timeline entries for rendered reports."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from backend.services.reporting.report_content_safety import sanitize_customer_text
from backend.services.reporting.report_context_builder import (
    ReportContext,
    ReportEvidenceRef,
)
from backend.services.reporting.report_tool_display import report_tool_display_name


@dataclass(frozen=True, slots=True)
class ReportEvidenceTimelineItem:
    """One management-facing evidence timeline row for report rendering."""

    ref: str
    order: int
    observed_at: str | None
    created_at: str | None
    source_tool: str
    target: str | None
    evidence_type: str
    summary: str
    excerpt: str


def build_report_evidence_timeline(
    context: ReportContext,
) -> tuple[ReportEvidenceTimelineItem, ...]:
    """Return deterministic presentation evidence rows from report context."""

    forbidden_refs = _forbidden_refs(context)
    ordered_refs = sorted(
        enumerate(context.compatible_evidence_refs),
        key=lambda item: _timeline_sort_key(item[0], item[1]),
    )
    return tuple(
        _timeline_item(
            evidence=evidence,
            order=order,
            forbidden_refs=forbidden_refs,
        )
        for order, (_index, evidence) in enumerate(ordered_refs, start=1)
    )


def _timeline_item(
    *,
    evidence: ReportEvidenceRef,
    order: int,
    forbidden_refs: tuple[str, ...],
) -> ReportEvidenceTimelineItem:
    return ReportEvidenceTimelineItem(
        ref=str(evidence.ref),
        order=int(order),
        observed_at=evidence.observed_at,
        created_at=evidence.created_at,
        source_tool=sanitize_customer_text(
            report_tool_display_name(evidence.source_tool),
            forbidden_refs=forbidden_refs,
        ),
        target=(
            sanitize_customer_text(evidence.target, forbidden_refs=forbidden_refs)
            if evidence.target
            else None
        ),
        evidence_type=sanitize_customer_text(
            evidence.evidence_type,
            forbidden_refs=forbidden_refs,
        ),
        summary=sanitize_customer_text(evidence.summary, forbidden_refs=forbidden_refs),
        excerpt=sanitize_customer_text(evidence.excerpt, forbidden_refs=forbidden_refs),
    )


def _timeline_sort_key(index: int, evidence: ReportEvidenceRef) -> tuple[bool, str, bool, str, int]:
    observed_at = _sort_text(evidence.observed_at)
    created_at = _sort_text(evidence.created_at)
    return (
        observed_at == "",
        observed_at,
        created_at == "",
        created_at,
        int(index),
    )


def _sort_text(value: Any) -> str:
    return str(value or "").strip()


def _forbidden_refs(context: ReportContext) -> tuple[str, ...]:
    return tuple(
        sorted(
            _unique(
                (
                    *context.allowed_task_memo_ids,
                    *context.allowed_knowledge_refs,
                    *context.allowed_evidence_refs,
                    context.source_watermark.hash,
                )
            )
        )
    )


def _unique(values: Iterable[Any]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return tuple(unique)


__all__ = [
    "ReportEvidenceTimelineItem",
    "build_report_evidence_timeline",
]
