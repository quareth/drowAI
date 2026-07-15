"""Persist and restore deterministic inputs for report finalization retries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.models.reporting import EngagementReport
from backend.services.reporting.report_context_builder import ReportContext
from backend.services.reporting.report_evidence_timeline import (
    ReportEvidenceTimelineItem,
    build_report_evidence_timeline,
)
from backend.services.reporting.report_section_plan import ReportSectionPlan


@dataclass(frozen=True, slots=True)
class ReportFinalizationCheckpoint:
    """Complete deterministic renderer input persisted after section generation."""

    sections: tuple[dict[str, Any], ...]
    section_metadata: tuple[dict[str, Any], ...]
    evidence_timeline: tuple[ReportEvidenceTimelineItem, ...]
    source_task_memo_ids: tuple[str, ...]
    source_knowledge_refs: tuple[dict[str, Any], ...]
    source_evidence_refs: tuple[dict[str, Any], ...]
    base_generation_metadata: Mapping[str, Any]


class ReportFinalizationCheckpointError(Exception):
    """Safe terminal failure raised for absent or invalid checkpoint data."""

    def __init__(self, safe_message: str, *, failure_class: str | None = None) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.failure_class = failure_class


def build_finalization_checkpoint(
    *,
    sections: Sequence[Mapping[str, Any]],
    section_metadata: Sequence[Mapping[str, Any]],
    context: ReportContext,
    section_plan: ReportSectionPlan,
) -> ReportFinalizationCheckpoint:
    """Build the renderer checkpoint from validated sections and bounded context."""

    return ReportFinalizationCheckpoint(
        sections=tuple(dict(section) for section in sections),
        section_metadata=tuple(dict(item) for item in section_metadata),
        evidence_timeline=build_report_evidence_timeline(context),
        source_task_memo_ids=tuple(str(memo.memo_id) for memo in context.selected_memos),
        source_knowledge_refs=tuple(
            {
                "ref": str(item.ref),
                "task_id": int(item.task_id),
                "record_type": str(item.record_type),
                "authoritative": bool(item.authoritative),
            }
            for item in context.compatible_knowledge_refs
        ),
        source_evidence_refs=tuple(
            {
                "ref": str(item.ref),
                "task_id": int(item.task_id),
                "evidence_type": str(item.evidence_type),
                "source_tool": str(item.source_tool),
            }
            for item in context.compatible_evidence_refs
        ),
        base_generation_metadata={
            **safe_report_metadata(context.source_watermark.generation_metadata),
            **safe_report_metadata(section_plan.generation_metadata()),
        },
    )


def checkpoint_generation_metadata(
    checkpoint: ReportFinalizationCheckpoint,
) -> dict[str, Any]:
    """Serialize a finalization checkpoint into report generation metadata."""

    return {
        "sections": [dict(item) for item in checkpoint.section_metadata],
        "finalization": {
            "evidence_timeline": [
                {
                    "ref": item.ref,
                    "order": item.order,
                    "observed_at": item.observed_at,
                    "created_at": item.created_at,
                    "source_tool": item.source_tool,
                    "target": item.target,
                    "evidence_type": item.evidence_type,
                    "summary": item.summary,
                    "excerpt": item.excerpt,
                }
                for item in checkpoint.evidence_timeline
            ],
            "source_task_memo_ids": list(checkpoint.source_task_memo_ids),
            "source_knowledge_refs": [
                dict(item) for item in checkpoint.source_knowledge_refs
            ],
            "source_evidence_refs": [
                dict(item) for item in checkpoint.source_evidence_refs
            ],
            "base_generation_metadata": safe_report_metadata(
                checkpoint.base_generation_metadata
            ),
        },
    }


def load_finalization_checkpoint(
    report: EngagementReport,
) -> ReportFinalizationCheckpoint:
    """Restore and validate a report's persisted finalization checkpoint."""

    metadata = report.generation_metadata
    finalization = (
        metadata.get("finalization") if isinstance(metadata, Mapping) else None
    )
    if not isinstance(finalization, Mapping):
        raise ReportFinalizationCheckpointError(
            "Report finalization checkpoint is unavailable."
        )
    try:
        timeline = tuple(
            ReportEvidenceTimelineItem(
                ref=str(item["ref"]),
                order=int(item["order"]),
                observed_at=_optional_string(item.get("observed_at")),
                created_at=_optional_string(item.get("created_at")),
                source_tool=str(item["source_tool"]),
                target=_optional_string(item.get("target")),
                evidence_type=str(item["evidence_type"]),
                summary=str(item["summary"]),
                excerpt=str(item["excerpt"]),
            )
            for item in _mapping_items(finalization.get("evidence_timeline"))
        )
        base_metadata = finalization.get("base_generation_metadata")
        if not isinstance(base_metadata, Mapping):
            raise ValueError("missing base generation metadata")
        return ReportFinalizationCheckpoint(
            sections=tuple(
                dict(section)
                for section in (report.sections or [])
                if isinstance(section, Mapping)
            ),
            section_metadata=tuple(existing_section_metadata(report)),
            evidence_timeline=timeline,
            source_task_memo_ids=tuple(
                str(item)
                for item in _sequence_items(finalization.get("source_task_memo_ids"))
            ),
            source_knowledge_refs=tuple(
                dict(item)
                for item in _mapping_items(finalization.get("source_knowledge_refs"))
            ),
            source_evidence_refs=tuple(
                dict(item)
                for item in _mapping_items(finalization.get("source_evidence_refs"))
            ),
            base_generation_metadata=dict(base_metadata),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReportFinalizationCheckpointError(
            "Report finalization checkpoint is invalid.",
            failure_class=exc.__class__.__name__,
        ) from exc


def final_generation_metadata(
    *,
    checkpoint: ReportFinalizationCheckpoint,
    renderer_metadata: Mapping[str, Any],
    llm_runtime_selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Build public-ready generation metadata after successful rendering."""

    return {
        **safe_report_metadata(checkpoint.base_generation_metadata),
        **safe_report_metadata(renderer_metadata),
        "llm_runtime_selection": {
            "provider": str(llm_runtime_selection.get("provider") or ""),
            "model": str(llm_runtime_selection.get("model") or ""),
            "reasoning_effort": (
                str(llm_runtime_selection["reasoning_effort"])
                if llm_runtime_selection.get("reasoning_effort") is not None
                else None
            ),
        },
        "sections": [
            safe_report_metadata(item) for item in checkpoint.section_metadata
        ],
    }


def existing_section_metadata(report: EngagementReport) -> list[dict[str, Any]]:
    """Return safe persisted section generation metadata."""

    metadata = report.generation_metadata
    if not isinstance(metadata, Mapping):
        return []
    sections = metadata.get("sections")
    if not isinstance(sections, Sequence) or isinstance(
        sections, (str, bytes, bytearray)
    ):
        return []
    return [dict(item) for item in sections if isinstance(item, Mapping)]


def safe_report_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize bounded operational metadata into JSON-safe values."""

    return {
        str(key): normalized
        for key, value in metadata.items()
        if (normalized := _safe_metadata_value(value)) is not None
    }


def _safe_metadata_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return safe_report_metadata(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            item
            for item in (_safe_metadata_value(item) for item in value)
            if item is not None
        ]
    return str(value)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _sequence_items(value) if isinstance(item, Mapping)]


def _sequence_items(value: Any) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(value)


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "ReportFinalizationCheckpoint",
    "ReportFinalizationCheckpointError",
    "build_finalization_checkpoint",
    "checkpoint_generation_metadata",
    "existing_section_metadata",
    "final_generation_metadata",
    "load_finalization_checkpoint",
    "safe_report_metadata",
]
