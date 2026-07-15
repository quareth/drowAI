"""Build bounded report-generation context from selected task closure memos."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task
from backend.models.reporting import TaskClosureMemo
from backend.services.reporting.contracts import (
    MEMO_MODE_LIMITED,
    MEMO_MODE_SUPPORTED,
    MEMO_STATUS_READY,
    MemoMode,
    ReportType,
    validate_memo_mode,
    validate_report_type,
)
from backend.services.reporting.evidence_packet_builder import (
    EvidencePacket,
    EvidencePacketBuilder,
    EvidencePacketItem,
)
from backend.services.reporting.knowledge_packet_builder import (
    KnowledgePacket,
    KnowledgePacketBuilder,
    KnowledgePacketItem,
)
from backend.services.reporting.source_watermark_service import (
    ReportSourceMemoWatermarkInput,
    build_report_source_generation_metadata,
    build_report_source_watermark,
)

_REPORT_SOURCE_WATERMARK_SCHEMA_VERSION = 1
_DEFAULT_MAX_SELECTED_MEMOS = 100
_DEFAULT_MAX_MEMO_ITEMS = 100
_DEFAULT_MAX_MEMO_TEXT_CHARACTERS = 2_000


@dataclass(frozen=True, slots=True)
class ReportEngagementMetadata:
    """Stable engagement metadata available to report generation."""

    engagement_id: int
    tenant_id: int
    user_id: int
    name: str
    description: str | None
    status: str
    created_at: str | None


@dataclass(frozen=True, slots=True)
class ReportSelectedTaskMetadata:
    """Stable task metadata for a selected task closure memo."""

    task_id: int
    memo_id: str
    name: str
    description: str | None
    scope: str | None
    status: str
    created_at: str | None
    stopped_at: str | None


@dataclass(frozen=True, slots=True)
class ReportSelectedMemoBody:
    """Mode-constrained task closure memo body exposed to report generation."""

    actions_performed: tuple[Mapping[str, Any], ...]
    reportable_observations: tuple[Mapping[str, Any], ...]
    possible_findings: tuple[Mapping[str, Any], ...]
    limitations: tuple[Mapping[str, Any], ...]
    unsupported_notes: tuple[Mapping[str, Any], ...]
    evidence_refs: tuple[str, ...]
    knowledge_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportSelectedMemoContext:
    """Selected current memo material supplied to report section planning."""

    memo_id: str
    task_id: int
    version: int
    memo_mode: MemoMode
    generated_at: str | None
    summary: str | None
    body: ReportSelectedMemoBody
    source_watermark: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReportMemoPartitions:
    """Selected memos split by supported and limited reporting modes."""

    supported_memo_ids: tuple[str, ...]
    limited_memo_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportKnowledgeRef:
    """Bounded compatible knowledge reference for report grounding."""

    ref: str
    task_id: int
    record_type: str
    summary: str
    authoritative: bool
    source_execution_ids: tuple[str, ...]
    evidence_archive_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportEvidenceRef:
    """Bounded compatible evidence reference for report grounding."""

    ref: str
    task_id: int
    evidence_type: str
    summary: str
    excerpt: str
    source_tool: str
    target: str | None
    observed_at: str | None
    created_at: str | None
    linked_knowledge_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportCandidateFindingPolicy:
    """Controls whether candidate-only findings may be used by later stages."""

    include_candidate_findings: bool


@dataclass(frozen=True, slots=True)
class ReportMemoWatermark:
    """Selected memo source watermark details preserved without aggregation."""

    memo_id: str
    task_id: int
    version: int
    source_watermark: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReportSourceWatermark:
    """Report input watermark material for replay and currentness checks."""

    schema_version: int
    report_type: ReportType
    candidate_policy: ReportCandidateFindingPolicy
    selected_memos: tuple[ReportMemoWatermark, ...]
    hash_algorithm: str
    hash: str
    job_source_watermark: Mapping[str, Any]
    generation_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Immutable bounded context for deterministic report prompt rendering."""

    engagement: ReportEngagementMetadata
    report_type: ReportType
    selected_memos: tuple[ReportSelectedMemoContext, ...]
    memo_partitions: ReportMemoPartitions
    selected_tasks: tuple[ReportSelectedTaskMetadata, ...]
    compatible_knowledge_refs: tuple[ReportKnowledgeRef, ...]
    compatible_evidence_refs: tuple[ReportEvidenceRef, ...]
    candidate_policy: ReportCandidateFindingPolicy
    source_watermark: ReportSourceWatermark
    allowed_task_memo_ids: frozenset[str]
    allowed_knowledge_refs: frozenset[str]
    allowed_evidence_refs: frozenset[str]
    truncated: bool
    candidate_only_knowledge_refs: frozenset[str] = frozenset()


class ReportContextBuilder:
    """Compose report context from selected current task closure memo rows."""

    def __init__(
        self,
        db: Session,
        *,
        max_selected_memos: int = _DEFAULT_MAX_SELECTED_MEMOS,
        max_memo_items: int = _DEFAULT_MAX_MEMO_ITEMS,
        max_memo_text_characters: int = _DEFAULT_MAX_MEMO_TEXT_CHARACTERS,
        knowledge_builder: KnowledgePacketBuilder | None = None,
        evidence_builder: EvidencePacketBuilder | None = None,
    ) -> None:
        if max_selected_memos <= 0:
            raise ValueError("max_selected_memos must be greater than zero")
        if max_memo_items <= 0:
            raise ValueError("max_memo_items must be greater than zero")
        if max_memo_text_characters <= 0:
            raise ValueError("max_memo_text_characters must be greater than zero")
        self._db = db
        self._max_selected_memos = int(max_selected_memos)
        self._max_memo_items = int(max_memo_items)
        self._max_memo_text_characters = int(max_memo_text_characters)
        self._knowledge = knowledge_builder or KnowledgePacketBuilder(db)
        self._evidence = evidence_builder or EvidencePacketBuilder(db)

    def build(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
        selected_memos: Sequence[TaskClosureMemo],
        include_candidate_findings: bool = False,
    ) -> ReportContext:
        """Return bounded context from selected current task closure memos."""

        validated_report_type = validate_report_type(report_type)
        ordered_memos = self._ordered_selected_memos(
            selected_memos=selected_memos,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
        )
        engagement = self._load_engagement(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
        )
        tasks_by_id = self._load_selected_tasks(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_ids=[int(memo.task_id) for memo in ordered_memos],
        )
        selected_task_metadata = tuple(
            _task_metadata(task=tasks_by_id[int(memo.task_id)], memo=memo)
            for memo in ordered_memos
        )
        selected_memo_contexts = tuple(
            self._memo_context(memo) for memo in ordered_memos
        )
        candidate_policy = ReportCandidateFindingPolicy(
            include_candidate_findings=bool(include_candidate_findings)
        )
        supported_task_ids = tuple(
            memo.task_id
            for memo in selected_memo_contexts
            if memo.memo_mode == MEMO_MODE_SUPPORTED
        )
        knowledge_packets, evidence_packets = self._source_packets(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_ids=supported_task_ids,
        )
        compatible_knowledge = _compatible_knowledge_refs(
            knowledge_packets=knowledge_packets,
            include_candidate_findings=candidate_policy.include_candidate_findings,
        )
        candidate_only_knowledge_refs = _candidate_only_knowledge_refs(knowledge_packets)
        compatible_evidence = _compatible_evidence_refs(
            evidence_packets=evidence_packets,
            candidate_only_knowledge_refs=candidate_only_knowledge_refs,
            include_candidate_findings=candidate_policy.include_candidate_findings,
        )
        source_watermark = _report_source_watermark(
            report_type=validated_report_type,
            candidate_policy=candidate_policy,
            selected_memos=ordered_memos,
        )
        return ReportContext(
            engagement=_engagement_metadata(engagement),
            report_type=validated_report_type,
            selected_memos=selected_memo_contexts,
            memo_partitions=_memo_partitions(selected_memo_contexts),
            selected_tasks=selected_task_metadata,
            compatible_knowledge_refs=compatible_knowledge,
            compatible_evidence_refs=compatible_evidence,
            candidate_policy=candidate_policy,
            source_watermark=source_watermark,
            allowed_task_memo_ids=frozenset(memo.memo_id for memo in selected_memo_contexts),
            allowed_knowledge_refs=frozenset(ref.ref for ref in compatible_knowledge),
            allowed_evidence_refs=frozenset(ref.ref for ref in compatible_evidence),
            truncated=(
                len(selected_memos) > len(ordered_memos)
                or any(packet.truncated for packet in knowledge_packets.values())
                or any(packet.truncated for packet in evidence_packets.values())
            ),
            candidate_only_knowledge_refs=candidate_only_knowledge_refs,
        )

    def _ordered_selected_memos(
        self,
        *,
        selected_memos: Sequence[TaskClosureMemo],
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> tuple[TaskClosureMemo, ...]:
        for memo in selected_memos:
            _validate_selected_memo_scope(
                memo=memo,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
            )
        if len(selected_memos) > self._max_selected_memos:
            raise ValueError("selected memo count exceeds the report context limit")
        memos = sorted(
            selected_memos,
            key=lambda memo: (int(memo.task_id), int(memo.version), str(memo.id)),
        )
        return tuple(memos)

    def _load_engagement(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> Engagement:
        engagement = (
            self._db.query(Engagement)
            .filter(
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == int(engagement_id),
            )
            .one_or_none()
        )
        if engagement is None:
            raise ValueError("engagement does not belong to the requested scope")
        return engagement

    def _load_selected_tasks(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Task]:
        unique_task_ids = sorted({int(task_id) for task_id in task_ids})
        if not unique_task_ids:
            return {}
        rows = (
            self._db.query(Task)
            .filter(
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                Task.status == TaskStatus.STOPPED.value,
                Task.id.in_(unique_task_ids),
            )
            .order_by(Task.id.asc())
            .all()
        )
        tasks_by_id = {int(row.id): row for row in rows}
        if set(unique_task_ids) != set(tasks_by_id):
            raise ValueError(
                "selected memo task must belong to the requested scope and be stopped"
            )
        return tasks_by_id

    def _source_packets(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_ids: Sequence[int],
    ) -> tuple[dict[int, KnowledgePacket], dict[int, EvidencePacket]]:
        knowledge_packets: dict[int, KnowledgePacket] = {}
        evidence_packets: dict[int, EvidencePacket] = {}
        for task_id in sorted({int(task_id) for task_id in task_ids}):
            knowledge_packets[task_id] = self._knowledge.build_for_task(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
            evidence_packets[task_id] = self._evidence.build_for_task(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            )
        return knowledge_packets, evidence_packets

    def _memo_context(self, memo: TaskClosureMemo) -> ReportSelectedMemoContext:
        body = memo.memo if isinstance(memo.memo, Mapping) else {}
        memo_mode = validate_memo_mode(str(memo.memo_mode))
        body_view = _memo_body_view(
            body=body,
            memo_mode=memo_mode,
            max_items=self._max_memo_items,
            max_text_characters=self._max_memo_text_characters,
        )
        summary = _compact_text(body.get("summary"), self._max_memo_text_characters)
        return ReportSelectedMemoContext(
            memo_id=str(memo.id),
            task_id=int(memo.task_id),
            version=int(memo.version),
            memo_mode=memo_mode,
            generated_at=_datetime_to_json(memo.generated_at),
            summary=summary or None,
            body=body_view,
            source_watermark=_freeze_json_mapping(
                memo.source_watermark
                if isinstance(memo.source_watermark, Mapping)
                else {}
            ),
        )


def _report_source_watermark(
    *,
    report_type: ReportType,
    candidate_policy: ReportCandidateFindingPolicy,
    selected_memos: Sequence[TaskClosureMemo],
) -> ReportSourceWatermark:
    memo_watermarks = tuple(
        ReportMemoWatermark(
            memo_id=str(memo.id),
            task_id=int(memo.task_id),
            version=int(memo.version),
            source_watermark=_freeze_json_mapping(
                memo.source_watermark if isinstance(memo.source_watermark, Mapping) else {}
            ),
        )
        for memo in selected_memos
    )
    job_source_watermark = build_report_source_watermark(
        report_type=report_type,
        selected_memos=tuple(
            ReportSourceMemoWatermarkInput(
                memo_id=watermark.memo_id,
                version=watermark.version,
                source_watermark=watermark.source_watermark,
            )
            for watermark in memo_watermarks
        ),
        include_candidate_findings=candidate_policy.include_candidate_findings,
    )
    return ReportSourceWatermark(
        schema_version=int(
            job_source_watermark.get(
                "schema_version", _REPORT_SOURCE_WATERMARK_SCHEMA_VERSION
            )
        ),
        report_type=report_type,
        candidate_policy=candidate_policy,
        selected_memos=memo_watermarks,
        hash_algorithm=str(job_source_watermark["hash_algorithm"]),
        hash=str(job_source_watermark["hash"]),
        job_source_watermark=job_source_watermark,
        generation_metadata=build_report_source_generation_metadata(job_source_watermark),
    )


def _validate_selected_memo_scope(
    *,
    memo: TaskClosureMemo,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
) -> None:
    if (
        int(memo.tenant_id) != int(tenant_id)
        or int(memo.user_id) != int(user_id)
        or int(memo.engagement_id) != int(engagement_id)
    ):
        raise ValueError("selected memo does not belong to the requested scope")
    if str(memo.status) != MEMO_STATUS_READY or not bool(memo.is_current):
        raise ValueError("selected memo must be current and ready")
    validate_memo_mode(str(memo.memo_mode))


def _memo_body_view(
    *,
    body: Mapping[str, Any],
    memo_mode: MemoMode,
    max_items: int,
    max_text_characters: int,
) -> ReportSelectedMemoBody:
    actions = _bounded_mapping_items(
        body.get("actions_performed"),
        max_items=max_items,
        max_text_characters=max_text_characters,
    )
    limitations = _bounded_mapping_items(
        body.get("limitations"),
        max_items=max_items,
        max_text_characters=max_text_characters,
    )
    unsupported_notes = _bounded_mapping_items(
        body.get("unsupported_notes"),
        max_items=max_items,
        max_text_characters=max_text_characters,
    )
    if memo_mode == MEMO_MODE_LIMITED:
        return ReportSelectedMemoBody(
            actions_performed=(),
            reportable_observations=(),
            possible_findings=(),
            limitations=limitations,
            unsupported_notes=unsupported_notes,
            evidence_refs=(),
            knowledge_refs=(),
        )
    return ReportSelectedMemoBody(
        actions_performed=actions,
        reportable_observations=_bounded_mapping_items(
            body.get("reportable_observations"),
            max_items=max_items,
            max_text_characters=max_text_characters,
        ),
        possible_findings=_bounded_mapping_items(
            body.get("possible_findings"),
            max_items=max_items,
            max_text_characters=max_text_characters,
        ),
        limitations=limitations,
        unsupported_notes=unsupported_notes,
        evidence_refs=_source_refs(body, key="evidence_refs"),
        knowledge_refs=_source_refs(body, key="knowledge_refs"),
    )


def _compatible_knowledge_refs(
    *,
    knowledge_packets: Mapping[int, KnowledgePacket],
    include_candidate_findings: bool,
) -> tuple[ReportKnowledgeRef, ...]:
    refs: list[ReportKnowledgeRef] = []
    for task_id, packet in sorted(knowledge_packets.items()):
        for item in packet.items:
            if not include_candidate_findings and not item.authoritative:
                continue
            refs.append(_knowledge_ref(task_id=task_id, item=item))
    return tuple(sorted(refs, key=lambda ref: (ref.task_id, ref.ref)))


def _compatible_evidence_refs(
    evidence_packets: Mapping[int, EvidencePacket],
    candidate_only_knowledge_refs: frozenset[str],
    include_candidate_findings: bool,
) -> tuple[ReportEvidenceRef, ...]:
    refs: list[ReportEvidenceRef] = []
    for task_id, packet in sorted(evidence_packets.items()):
        for item in packet.items:
            ref = _evidence_ref(task_id=task_id, item=item)
            if (
                not include_candidate_findings
                and ref.linked_knowledge_refs
                and set(ref.linked_knowledge_refs).issubset(
                    candidate_only_knowledge_refs
                )
            ):
                continue
            refs.append(ref)
    return tuple(sorted(refs, key=lambda ref: (ref.task_id, ref.ref)))


def _candidate_only_knowledge_refs(
    knowledge_packets: Mapping[int, KnowledgePacket],
) -> frozenset[str]:
    return frozenset(
        item.ref
        for packet in knowledge_packets.values()
        for item in packet.items
        if not item.authoritative
    )


def _knowledge_ref(*, task_id: int, item: KnowledgePacketItem) -> ReportKnowledgeRef:
    return ReportKnowledgeRef(
        ref=item.ref,
        task_id=int(task_id),
        record_type=str(item.record_type),
        summary=item.summary,
        authoritative=bool(item.authoritative),
        source_execution_ids=tuple(item.source_execution_ids),
        evidence_archive_refs=tuple(
            f"evidence_archive:{ref}"
            if not str(ref).startswith("evidence_archive:")
            else str(ref)
            for ref in item.evidence_archive_refs
        ),
    )


def _evidence_ref(*, task_id: int, item: EvidencePacketItem) -> ReportEvidenceRef:
    linked_refs = (
        *item.linked_asset_refs,
        *item.linked_service_refs,
        *item.linked_finding_refs,
    )
    return ReportEvidenceRef(
        ref=item.ref,
        task_id=int(task_id),
        evidence_type=item.evidence_type,
        summary=item.summary,
        excerpt=item.excerpt,
        source_tool=item.source_tool,
        target=item.target,
        observed_at=item.observed_at,
        created_at=item.created_at,
        linked_knowledge_refs=tuple(sorted(linked_refs)),
    )


def _memo_partitions(
    selected_memos: Sequence[ReportSelectedMemoContext],
) -> ReportMemoPartitions:
    return ReportMemoPartitions(
        supported_memo_ids=tuple(
            memo.memo_id for memo in selected_memos if memo.memo_mode == MEMO_MODE_SUPPORTED
        ),
        limited_memo_ids=tuple(
            memo.memo_id for memo in selected_memos if memo.memo_mode == MEMO_MODE_LIMITED
        ),
    )


def _engagement_metadata(engagement: Engagement) -> ReportEngagementMetadata:
    return ReportEngagementMetadata(
        engagement_id=int(engagement.id),
        tenant_id=int(engagement.tenant_id),
        user_id=int(engagement.user_id),
        name=str(engagement.name),
        description=_optional_text(engagement.description),
        status=str(engagement.status),
        created_at=_datetime_to_json(engagement.created_at),
    )


def _task_metadata(*, task: Task, memo: TaskClosureMemo) -> ReportSelectedTaskMetadata:
    return ReportSelectedTaskMetadata(
        task_id=int(task.id),
        memo_id=str(memo.id),
        name=str(task.name),
        description=_optional_text(task.description),
        scope=_optional_text(task.scope),
        status=str(task.status),
        created_at=_datetime_to_json(task.created_at),
        stopped_at=_datetime_to_json(task.stopped_at),
    )


def _bounded_mapping_items(
    value: Any,
    *,
    max_items: int,
    max_text_characters: int,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list | tuple):
        return ()
    items: list[Mapping[str, Any]] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            items.append(_freeze_json_mapping(_compact_mapping(item, max_text_characters)))
    return tuple(items)


def _compact_mapping(value: Mapping[str, Any], max_text_characters: int) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str):
            compact[str(key)] = _compact_text(item, max_text_characters)
        elif isinstance(item, int | float | bool) or item is None:
            compact[str(key)] = item
        elif isinstance(item, list | tuple):
            compact[str(key)] = [
                _compact_json_value(child, max_text_characters)
                for child in item[:_DEFAULT_MAX_MEMO_ITEMS]
            ]
        elif isinstance(item, Mapping):
            compact[str(key)] = _compact_mapping(item, max_text_characters)
    return compact


def _compact_json_value(value: Any, max_text_characters: int) -> Any:
    if isinstance(value, str):
        return _compact_text(value, max_text_characters)
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, Mapping):
        return _compact_mapping(value, max_text_characters)
    if isinstance(value, list | tuple):
        return [_compact_json_value(item, max_text_characters) for item in value[:20]]
    return _compact_text(value, max_text_characters)


def _source_refs(body: Mapping[str, Any], *, key: str) -> tuple[str, ...]:
    return tuple(sorted(set(_iter_source_refs(body, key=key))))


def _iter_source_refs(value: Any, *, key: str) -> Iterable[str]:
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            if item_key == key and isinstance(item_value, list | tuple):
                for ref in item_value:
                    text = _optional_text(ref)
                    if text:
                        yield text
            else:
                yield from _iter_source_refs(item_value, key=key)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_source_refs(item, key=key)


def _freeze_json_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {str(key): _freeze_json_value(item) for key, item in value.items()}
    )


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _compact_text(value: Any, max_characters: int) -> str:
    text = _optional_text(value)
    if not text or max_characters <= 0:
        return ""
    text = " ".join(text.split())
    if len(text) <= max_characters:
        return text
    if max_characters <= 3:
        return text[:max_characters]
    return text[: max_characters - 3].rstrip() + "..."


def _datetime_to_json(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "ReportCandidateFindingPolicy",
    "ReportContext",
    "ReportContextBuilder",
    "ReportEngagementMetadata",
    "ReportEvidenceRef",
    "ReportKnowledgeRef",
    "ReportMemoPartitions",
    "ReportMemoWatermark",
    "ReportSelectedMemoBody",
    "ReportSelectedMemoContext",
    "ReportSelectedTaskMetadata",
    "ReportSourceWatermark",
]
