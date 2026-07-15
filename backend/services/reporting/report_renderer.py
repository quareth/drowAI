"""Render persisted engagement report sections into Markdown snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.services.reporting.contracts import (
    GENERATION_METADATA_RENDERER_VERSION_KEY,
    REPORT_SECTION_BLOCK_TYPE_FINDING,
    REPORT_SECTION_TYPE_APPENDIX,
)
from backend.services.reporting.report_evidence_timeline import (
    ReportEvidenceTimelineItem,
)
from backend.services.reporting.report_section_plan import get_report_section_plan

REPORT_RENDERER_VERSION = "engagement_report_markdown_renderer.v1"


@dataclass(frozen=True, slots=True)
class EngagementReportMarkdownRenderResult:
    """Rendered Markdown snapshot and metadata safe for report persistence."""

    markdown_snapshot: str
    generation_metadata: Mapping[str, str]


class EngagementReportMarkdownRenderer:
    """Render stored validated report sections without reading source material."""

    def render(
        self,
        *,
        title: str,
        report_type: str,
        sections: Sequence[Mapping[str, Any]],
        evidence_timeline: Sequence[ReportEvidenceTimelineItem],
    ) -> EngagementReportMarkdownRenderResult:
        """Return deterministic Markdown and renderer generation metadata."""

        ordered_sections = _sections_in_plan_order(
            report_type=report_type,
            sections=sections,
        )
        included_evidence_timeline = _included_evidence_timeline(
            evidence_timeline=evidence_timeline,
            cited_refs=_cited_evidence_refs(ordered_sections),
        )
        lines = [f"# {_line(title)}"]

        for section in ordered_sections:
            lines.extend(
                _render_section(
                    section,
                    evidence_timeline=included_evidence_timeline,
                )
            )

        return EngagementReportMarkdownRenderResult(
            markdown_snapshot=_normalize_markdown_lines(lines),
            generation_metadata={
                GENERATION_METADATA_RENDERER_VERSION_KEY: REPORT_RENDERER_VERSION
            },
        )


def render_engagement_report_markdown(
    *,
    title: str,
    report_type: str,
    sections: Sequence[Mapping[str, Any]],
    evidence_timeline: Sequence[ReportEvidenceTimelineItem],
) -> EngagementReportMarkdownRenderResult:
    """Render engagement report Markdown using the default snapshot renderer."""

    return EngagementReportMarkdownRenderer().render(
        title=title,
        report_type=report_type,
        sections=sections,
        evidence_timeline=evidence_timeline,
    )


def _sections_in_plan_order(
    *,
    report_type: str,
    sections: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    plan_order = {
        item.section_id: item.order
        for item in get_report_section_plan(report_type).sections
    }
    return sorted(
        sections,
        key=lambda section: (
            plan_order.get(str(section.get("section_id", "")), len(plan_order) + 1),
            str(section.get("section_id", "")),
        ),
    )


def _render_section(
    section: Mapping[str, Any],
    *,
    evidence_timeline: Sequence[ReportEvidenceTimelineItem],
) -> list[str]:
    title = _line(section.get("title"))
    section_type = str(section.get("section_type", ""))
    lines = ["", f"## {title}"]

    if section_type == REPORT_SECTION_TYPE_APPENDIX:
        lines.extend(_render_appendix_evidence_timeline(evidence_timeline))
        return lines

    content = _markdown(section.get("content_markdown"))
    if content:
        lines.extend(["", content])

    for block in _mapping_sequence(section.get("blocks")):
        lines.extend(_render_block(block))

    unsupported_notes = _string_sequence(section.get("unsupported_notes"))
    if unsupported_notes:
        lines.extend(["", "### Unsupported Notes"])
        lines.extend(f"- {_line(note)}" for note in unsupported_notes)

    return lines


def _render_block(block: Mapping[str, Any]) -> list[str]:
    if str(block.get("block_type", "")) == REPORT_SECTION_BLOCK_TYPE_FINDING:
        return _render_finding_block(block)

    lines = ["", f"### {_line(block.get('title'))}"]
    content = _markdown(block.get("content_markdown"))
    if content:
        lines.extend(["", content])
    return lines


def _render_finding_block(block: Mapping[str, Any]) -> list[str]:
    lines = ["", f"### {_line(block.get('title'))}"]
    severity = _optional_line_value(block.get("severity"))
    confidence = _optional_line_value(block.get("confidence"))
    affected_assets = _string_sequence(block.get("affected_assets"))

    if severity:
        lines.append(f"- Severity: {severity}")
    if confidence:
        lines.append(f"- Confidence: {confidence}")
    if affected_assets:
        lines.append(
            "- Affected assets: "
            + ", ".join(f"`{_inline(asset)}`" for asset in affected_assets)
        )

    for heading, field in (
        ("Details", "content_markdown"),
        ("Impact", "impact_markdown"),
        ("Remediation", "remediation_markdown"),
    ):
        content = _markdown(block.get(field))
        if content:
            lines.extend(["", f"#### {heading}", "", content])

    return lines


def _render_appendix_evidence_timeline(
    evidence_timeline: Sequence[ReportEvidenceTimelineItem],
) -> list[str]:
    lines = ["", "### Evidence Index"]
    if not evidence_timeline:
        return [*lines, "", "No cited tool evidence entries were included."]

    for item in evidence_timeline:
        target = _optional_line_value(item.target)
        tool = _optional_line_value(item.source_tool) or "Tool execution"
        timing = _display_time(item.observed_at or item.created_at)
        evidence_id = _display_evidence_id(item.ref)
        heading = f"{item.order}. Evidence `{_inline(evidence_id)}` - {_line(tool)}"
        if target:
            heading = f"{heading} against `{_inline(target)}`"
        lines.extend(["", heading])
        lines.append(f"   - Recorded: {timing}")

    return lines


def _included_evidence_timeline(
    *,
    evidence_timeline: Sequence[ReportEvidenceTimelineItem],
    cited_refs: set[str],
) -> list[ReportEvidenceTimelineItem]:
    if not cited_refs:
        return []
    included = [item for item in evidence_timeline if str(item.ref) in cited_refs]
    return [
        ReportEvidenceTimelineItem(
            ref=item.ref,
            order=index,
            observed_at=item.observed_at,
            created_at=item.created_at,
            source_tool=item.source_tool,
            target=item.target,
            evidence_type=item.evidence_type,
            summary=item.summary,
            excerpt=item.excerpt,
        )
        for index, item in enumerate(included, start=1)
    ]


def _cited_evidence_refs(sections: Sequence[Mapping[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for section in sections:
        if str(section.get("section_type", "")) == REPORT_SECTION_TYPE_APPENDIX:
            continue
        refs.update(_evidence_refs_from_source_refs(section.get("source_refs")))
        for block in _mapping_sequence(section.get("blocks")):
            refs.update(_evidence_refs_from_source_refs(block.get("source_refs")))
    return refs


def _evidence_refs_from_source_refs(value: object) -> set[str]:
    if not isinstance(value, Mapping):
        return set()
    refs = value.get("evidence_refs")
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        return set()
    return {str(ref).strip() for ref in refs if str(ref).strip()}


def _display_evidence_id(value: object) -> str:
    text = _line(value)
    prefix = "evidence_archive:"
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text or "unknown"


def _mapping_sequence(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_sequence(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [_line(item) for item in value if _line(item)]


def _markdown(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _line(value: object) -> str:
    return " ".join(str(value).strip().split()) if value is not None else ""


def _inline(value: object) -> str:
    return _line(value).replace("`", "\\`")


def _optional_line_value(value: object) -> str:
    return _line(value) if value is not None else ""


def _display_time(value: object) -> str:
    text = _line(value)
    if not text:
        return "time not recorded"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    timezone_name = parsed.tzname() or ""
    rendered = parsed.strftime("%Y-%m-%d %H:%M")
    return f"{rendered} {timezone_name}".strip()


def _normalize_markdown_lines(lines: Sequence[str]) -> str:
    rendered: list[str] = []
    previous_blank = False
    for line in lines:
        normalized = line.rstrip()
        is_blank = normalized == ""
        if is_blank and previous_blank:
            continue
        rendered.append(normalized)
        previous_blank = is_blank

    while rendered and rendered[-1] == "":
        rendered.pop()

    return "\n".join(rendered) + "\n"


__all__ = [
    "EngagementReportMarkdownRenderResult",
    "EngagementReportMarkdownRenderer",
    "REPORT_RENDERER_VERSION",
    "render_engagement_report_markdown",
]
