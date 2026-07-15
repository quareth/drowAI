"""Build bounded task-local evidence packets for memo preparation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution

EvidenceExcerptSource = Literal["inline_excerpt", "execution_artifact", "none"]

_DEFAULT_MAX_ITEMS = 80
_DEFAULT_MAX_EXCERPT_CHARACTERS = 1_500
_DEFAULT_MAX_TOTAL_CHARACTERS = 12_000
_UNKNOWN_TOOL = "unknown_tool"
_LINKED_ENTITY_TYPES = ("asset", "service", "finding")


@dataclass(frozen=True)
class EvidencePacketItem:
    """One bounded durable evidence archive item available to memo preparation."""

    ref: str
    evidence_id: str
    tenant_id: int
    user_id: int
    engagement_id: int
    task_id: int
    source_execution_id: str
    source_artifact_id: str | None
    observed_at: str | None
    created_at: str | None
    source_tool: str
    evidence_type: str
    target: str | None
    summary: str
    excerpt: str
    excerpt_source: EvidenceExcerptSource
    excerpt_truncated: bool
    linked_asset_refs: tuple[str, ...]
    linked_service_refs: tuple[str, ...]
    linked_finding_refs: tuple[str, ...]
    byte_size: int | None
    mime_type: str | None


@dataclass(frozen=True)
class EvidencePacket:
    """Bounded task-local evidence packet for one tenant-owned task."""

    task_id: int
    items: tuple[EvidencePacketItem, ...]
    item_count: int
    artifact_fallback_count: int
    total_excerpt_characters: int
    truncated: bool
    max_items: int
    max_excerpt_characters: int
    max_total_characters: int


class EvidencePacketBuilder:
    """Read durable task-local evidence archives into a memo-safe packet."""

    def __init__(
        self,
        db: Session,
        *,
        max_items: int = _DEFAULT_MAX_ITEMS,
        max_excerpt_characters: int = _DEFAULT_MAX_EXCERPT_CHARACTERS,
        max_total_characters: int = _DEFAULT_MAX_TOTAL_CHARACTERS,
    ) -> None:
        if max_items <= 0:
            raise ValueError("max_items must be greater than zero")
        if max_excerpt_characters <= 0:
            raise ValueError("max_excerpt_characters must be greater than zero")
        if max_total_characters <= 0:
            raise ValueError("max_total_characters must be greater than zero")
        self._db = db
        self._max_items = int(max_items)
        self._max_excerpt_characters = int(max_excerpt_characters)
        self._max_total_characters = int(max_total_characters)

    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> EvidencePacket:
        """Return bounded durable evidence items for the requested task scope."""

        evidence_rows = self._select_evidence(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if not evidence_rows:
            return self._empty_packet(task_id=task_id)

        evidence_ids = [str(row.id) for row in evidence_rows]
        artifact_rows = self._select_artifacts(
            tenant_id=tenant_id,
            task_id=task_id,
            evidence_rows=evidence_rows,
        )
        tool_names = self._select_tool_names(
            tenant_id=tenant_id,
            task_id=task_id,
            evidence_rows=evidence_rows,
        )
        provenance_by_evidence = self._select_linked_provenance(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            evidence_ids=evidence_ids,
        )

        raw_items = [
            self._map_item(
                row=row,
                artifact=artifact_rows.get(str(row.source_artifact_id)),
                tool_name=tool_names.get(str(row.source_execution_id)),
                provenance_rows=provenance_by_evidence.get(str(row.id), ()),
            )
            for row in evidence_rows
        ]
        bounded_items, truncated = self._bound_items(raw_items)
        return EvidencePacket(
            task_id=int(task_id),
            items=tuple(bounded_items),
            item_count=len(bounded_items),
            artifact_fallback_count=sum(
                1
                for item in bounded_items
                if item.excerpt_source == "execution_artifact"
            ),
            total_excerpt_characters=sum(len(item.excerpt) for item in bounded_items),
            truncated=truncated
            or len(raw_items) > len(bounded_items)
            or any(item.excerpt_truncated for item in bounded_items),
            max_items=self._max_items,
            max_excerpt_characters=self._max_excerpt_characters,
            max_total_characters=self._max_total_characters,
        )

    def _empty_packet(self, *, task_id: int) -> EvidencePacket:
        return EvidencePacket(
            task_id=int(task_id),
            items=(),
            item_count=0,
            artifact_fallback_count=0,
            total_excerpt_characters=0,
            truncated=False,
            max_items=self._max_items,
            max_excerpt_characters=self._max_excerpt_characters,
            max_total_characters=self._max_total_characters,
        )

    def _select_evidence(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> list[KnowledgeEvidenceArchive]:
        return (
            self._db.query(KnowledgeEvidenceArchive)
            .select_from(KnowledgeEvidenceArchive)
            .join(Task, Task.id == KnowledgeEvidenceArchive.task_id)
            .filter(
                KnowledgeEvidenceArchive.tenant_id == int(tenant_id),
                KnowledgeEvidenceArchive.user_id == int(user_id),
                KnowledgeEvidenceArchive.engagement_id == int(engagement_id),
                KnowledgeEvidenceArchive.task_id == int(task_id),
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
            .order_by(
                KnowledgeEvidenceArchive.created_at.asc(),
                KnowledgeEvidenceArchive.id.asc(),
            )
            .limit(self._max_items + 1)
            .all()
        )

    def _select_artifacts(
        self,
        *,
        tenant_id: int,
        task_id: int,
        evidence_rows: Sequence[KnowledgeEvidenceArchive],
    ) -> dict[str, ExecutionArtifact]:
        artifact_ids = _unique_strings(
            row.source_artifact_id
            for row in evidence_rows
            if not _has_usable_inline_excerpt(row.inline_excerpt)
        )
        if not artifact_ids:
            return {}
        rows = (
            self._db.query(ExecutionArtifact)
            .filter(
                ExecutionArtifact.tenant_id == int(tenant_id),
                ExecutionArtifact.task_id == int(task_id),
                ExecutionArtifact.id.in_(artifact_ids),
            )
            .all()
        )
        return {str(row.id): row for row in rows}

    def _select_tool_names(
        self,
        *,
        tenant_id: int,
        task_id: int,
        evidence_rows: Sequence[KnowledgeEvidenceArchive],
    ) -> dict[str, str]:
        execution_ids = _unique_strings(
            row.source_execution_id for row in evidence_rows
        )
        if not execution_ids:
            return {}
        rows = (
            self._db.query(ToolExecution.id, ToolExecution.tool_name)
            .filter(
                ToolExecution.tenant_id == int(tenant_id),
                ToolExecution.task_id == int(task_id),
                ToolExecution.id.in_(execution_ids),
            )
            .all()
        )
        return {str(execution_id): str(tool_name) for execution_id, tool_name in rows}

    def _select_linked_provenance(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        evidence_ids: Sequence[str],
    ) -> dict[str, tuple[KnowledgeEntityProvenance, ...]]:
        if not evidence_ids:
            return {}
        rows = (
            self._db.query(KnowledgeEntityProvenance)
            .select_from(KnowledgeEntityProvenance)
            .join(Task, Task.id == KnowledgeEntityProvenance.task_id)
            .filter(
                KnowledgeEntityProvenance.tenant_id == int(tenant_id),
                KnowledgeEntityProvenance.user_id == int(user_id),
                KnowledgeEntityProvenance.engagement_id == int(engagement_id),
                KnowledgeEntityProvenance.task_id == int(task_id),
                KnowledgeEntityProvenance.evidence_archive_id.in_(evidence_ids),
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
            )
            .order_by(
                KnowledgeEntityProvenance.observed_at.asc(),
                KnowledgeEntityProvenance.id.asc(),
            )
            .all()
        )
        grouped: dict[str, list[KnowledgeEntityProvenance]] = defaultdict(list)
        for row in rows:
            if _normalize_text(row.entity_type) in _LINKED_ENTITY_TYPES:
                grouped[str(row.evidence_archive_id)].append(row)
        return {evidence_id: tuple(items) for evidence_id, items in grouped.items()}

    def _map_item(
        self,
        *,
        row: KnowledgeEvidenceArchive,
        artifact: ExecutionArtifact | None,
        tool_name: str | None,
        provenance_rows: Sequence[KnowledgeEntityProvenance],
    ) -> EvidencePacketItem:
        lineage = (
            row.lineage_snapshot if isinstance(row.lineage_snapshot, Mapping) else {}
        )
        metadata = (
            row.archive_metadata if isinstance(row.archive_metadata, Mapping) else {}
        )
        source_tool = (
            _first_text(
                lineage.get("source_tool"),
                lineage.get("tool_name"),
                metadata.get("source_tool"),
                metadata.get("tool_name"),
                tool_name,
            )
            or _UNKNOWN_TOOL
        )
        evidence_type = (
            _first_text(
                metadata.get("type"),
                metadata.get("evidence_type"),
                lineage.get("artifact_kind"),
                artifact.artifact_kind if artifact is not None else None,
                row.storage_mode,
            )
            or "evidence"
        )
        target = _first_text(
            metadata.get("target"),
            metadata.get("subject_key"),
            lineage.get("target"),
            lineage.get("subject_key"),
            lineage.get("relative_path"),
        )
        observed_at = _datetime_to_json(
            min((item.observed_at for item in provenance_rows), default=None)
        ) or _first_text(lineage.get("observed_at"))
        excerpt, excerpt_source, excerpt_truncated = self._select_excerpt(
            row=row,
            artifact=artifact,
        )
        linked_refs = _linked_refs(provenance_rows)
        return EvidencePacketItem(
            ref=f"evidence_archive:{row.id}",
            evidence_id=str(row.id),
            tenant_id=int(row.tenant_id),
            user_id=int(row.user_id),
            engagement_id=int(row.engagement_id),
            task_id=int(row.task_id),
            source_execution_id=str(row.source_execution_id),
            source_artifact_id=str(row.source_artifact_id)
            if row.source_artifact_id is not None
            else None,
            observed_at=observed_at,
            created_at=_datetime_to_json(row.created_at),
            source_tool=source_tool,
            evidence_type=evidence_type,
            target=target,
            summary=self._summary(
                source_tool=source_tool,
                evidence_type=evidence_type,
                target=target,
                excerpt=excerpt,
            ),
            excerpt=excerpt,
            excerpt_source=excerpt_source,
            excerpt_truncated=excerpt_truncated,
            linked_asset_refs=linked_refs["asset"],
            linked_service_refs=linked_refs["service"],
            linked_finding_refs=linked_refs["finding"],
            byte_size=int(row.byte_size) if row.byte_size is not None else None,
            mime_type=_normalize_optional_text(row.mime_type),
        )

    def _select_excerpt(
        self,
        *,
        row: KnowledgeEvidenceArchive,
        artifact: ExecutionArtifact | None,
    ) -> tuple[str, EvidenceExcerptSource, bool]:
        inline_excerpt, inline_truncated = _compact_text_with_status(
            row.inline_excerpt,
            self._max_excerpt_characters,
        )
        if inline_excerpt:
            return inline_excerpt, "inline_excerpt", inline_truncated

        artifact_excerpt, artifact_truncated = _compact_text_with_status(
            artifact.content_text if artifact is not None else None,
            self._max_excerpt_characters,
        )
        if artifact_excerpt:
            return artifact_excerpt, "execution_artifact", artifact_truncated
        return "", "none", False

    def _summary(
        self,
        *,
        source_tool: str,
        evidence_type: str,
        target: str | None,
        excerpt: str,
    ) -> str:
        if source_tool == _UNKNOWN_TOOL:
            base = "Evidence archive"
        else:
            base = f"{source_tool} {evidence_type}"
        if target:
            base = f"{base} for {target}"
        if not excerpt:
            return base
        return _compact_text(f"{base}: {excerpt}", 260)

    def _bound_items(
        self,
        items: Sequence[EvidencePacketItem],
    ) -> tuple[list[EvidencePacketItem], bool]:
        bounded: list[EvidencePacketItem] = []
        total_characters = 0
        truncated = False
        for item in items[: self._max_items]:
            remaining = self._max_total_characters - total_characters
            if remaining <= 0:
                truncated = True
                break
            if len(item.excerpt) <= remaining:
                bounded.append(item)
                total_characters += len(item.excerpt)
                continue
            excerpt, _was_truncated = _compact_text_with_status(item.excerpt, remaining)
            bounded.append(
                EvidencePacketItem(
                    ref=item.ref,
                    evidence_id=item.evidence_id,
                    tenant_id=item.tenant_id,
                    user_id=item.user_id,
                    engagement_id=item.engagement_id,
                    task_id=item.task_id,
                    source_execution_id=item.source_execution_id,
                    source_artifact_id=item.source_artifact_id,
                    observed_at=item.observed_at,
                    created_at=item.created_at,
                    source_tool=item.source_tool,
                    evidence_type=item.evidence_type,
                    target=item.target,
                    summary=self._summary(
                        source_tool=item.source_tool,
                        evidence_type=item.evidence_type,
                        target=item.target,
                        excerpt=excerpt,
                    ),
                    excerpt=excerpt,
                    excerpt_source=item.excerpt_source,
                    excerpt_truncated=True,
                    linked_asset_refs=item.linked_asset_refs,
                    linked_service_refs=item.linked_service_refs,
                    linked_finding_refs=item.linked_finding_refs,
                    byte_size=item.byte_size,
                    mime_type=item.mime_type,
                )
            )
            truncated = True
            break
        if len(items) > len(bounded):
            truncated = True
        return bounded, truncated


def _linked_refs(
    provenance_rows: Sequence[KnowledgeEntityProvenance],
) -> dict[str, tuple[str, ...]]:
    refs: dict[str, list[str]] = {
        entity_type: [] for entity_type in _LINKED_ENTITY_TYPES
    }
    for row in provenance_rows:
        entity_type = _normalize_text(row.entity_type)
        if entity_type not in refs:
            continue
        ref = f"knowledge_{entity_type}:{row.entity_id}"
        if ref not in refs[entity_type]:
            refs[entity_type].append(ref)
    return {entity_type: tuple(values) for entity_type, values in refs.items()}


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _normalize_optional_text(value)
        if text:
            return text
    return None


def _has_usable_inline_excerpt(value: Any) -> bool:
    return bool(_compact_text(value, 1))


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return tuple(unique)


def _compact_text(value: Any, max_characters: int) -> str:
    text, _truncated = _compact_text_with_status(value, max_characters)
    return text


def _compact_text_with_status(value: Any, max_characters: int) -> tuple[str, bool]:
    if value is None or max_characters <= 0:
        return "", False
    text = " ".join(str(value).split())
    if len(text) <= max_characters:
        return text, False
    if max_characters <= 3:
        return text[:max_characters], True
    return text[: max_characters - 3].rstrip() + "...", True


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _datetime_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "EvidenceExcerptSource",
    "EvidencePacket",
    "EvidencePacketBuilder",
    "EvidencePacketItem",
]
