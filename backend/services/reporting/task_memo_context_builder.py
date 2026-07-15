"""Compose task-local memo preparation context from durable reporting sources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.reporting import TaskClosureMemo
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    MEMO_MODE_LIMITED,
    MEMO_MODE_SUPPORTED,
    REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    MemoMode,
    ReportingReasonCode,
)
from backend.services.reporting.evidence_packet_builder import (
    EvidencePacket,
    EvidencePacketBuilder,
)
from backend.services.reporting.knowledge_packet_builder import (
    KnowledgePacket,
    KnowledgePacketBuilder,
)
from backend.services.reporting.runtime_readiness_service import (
    RuntimeReadiness,
    RuntimeReadinessService,
)
from backend.services.reporting.source_watermark_service import SourceWatermarkService
from backend.services.reporting.transcript_context_builder import (
    TranscriptContext,
    TranscriptContextBuilder,
)

_PREVIOUS_SUMMARY_MAX_CHARACTERS = 2_000


@dataclass(frozen=True, slots=True)
class TaskMemoTaskMetadata:
    """Stable task metadata supplied to memo preparation."""

    task_id: int
    tenant_id: int
    user_id: int
    engagement_id: int
    name: str
    description: str | None
    scope: str | None
    status: str
    created_at: str | None
    stopped_at: str | None


@dataclass(frozen=True, slots=True)
class PreviousTaskMemoContext:
    """Scoped current memo material supplied only for regeneration."""

    memo_id: str
    version: int
    memo_mode: str
    generated_at: str | None
    summary: str | None
    body: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TaskMemoContext:
    """Immutable task-local source context for task closure memo preparation."""

    task: TaskMemoTaskMetadata
    source_watermark: Mapping[str, Any]
    transcript: TranscriptContext
    knowledge: KnowledgePacket
    evidence: EvidencePacket
    previous_memo: PreviousTaskMemoContext | None
    runtime_readiness: RuntimeReadiness
    memo_mode: MemoMode | None
    not_preparable_reason: ReportingReasonCode | None
    allowed_evidence_refs: frozenset[str]
    allowed_knowledge_refs: frozenset[str]

    @property
    def is_preparable(self) -> bool:
        """Return whether context has a supported or limited memo mode."""

        return self.memo_mode is not None

    @property
    def has_reportable_source_refs(self) -> bool:
        """Return whether evidence or knowledge refs can ground reportable facts."""

        return bool(self.allowed_evidence_refs or self.allowed_knowledge_refs)


class TaskMemoContextBuilder:
    """Compose side-effect-free memo context for one tenant-owned task."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repository = TaskClosureMemoRepository(db)
        self._source_watermarks = SourceWatermarkService(db)
        self._transcripts = TranscriptContextBuilder(db)
        self._knowledge = KnowledgePacketBuilder(db)
        self._evidence = EvidencePacketBuilder(db)
        self._runtime_readiness = RuntimeReadinessService(db)

    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        regenerate: bool = False,
    ) -> TaskMemoContext:
        """Return immutable context for the requested task scope."""

        task = self._repository.get_task_for_memo_preparation(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if task is None:
            raise ValueError("task does not belong to the requested reporting scope")

        source_watermark = self._source_watermarks.compute_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        transcript = self._transcripts.build_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        knowledge = self._knowledge.build_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        evidence = self._evidence.build_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        runtime_readiness = self._runtime_readiness.compute_for_task(
            tenant_id=tenant_id,
            task_id=task_id,
        )

        allowed_evidence_refs = frozenset(item.ref for item in evidence.items)
        allowed_knowledge_refs = frozenset(item.ref for item in knowledge.items)
        memo_mode = _derive_memo_mode(
            allowed_evidence_refs=allowed_evidence_refs,
            allowed_knowledge_refs=allowed_knowledge_refs,
            runtime_readiness=runtime_readiness,
        )

        return TaskMemoContext(
            task=_task_metadata(task),
            source_watermark=_freeze_json_mapping(source_watermark),
            transcript=transcript,
            knowledge=knowledge,
            evidence=evidence,
            previous_memo=self._previous_memo_context(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                regenerate=regenerate,
            ),
            runtime_readiness=runtime_readiness,
            memo_mode=memo_mode,
            not_preparable_reason=_not_preparable_reason(
                memo_mode=memo_mode,
                runtime_readiness=runtime_readiness,
            ),
            allowed_evidence_refs=allowed_evidence_refs,
            allowed_knowledge_refs=allowed_knowledge_refs,
        )

    def _previous_memo_context(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        regenerate: bool,
    ) -> PreviousTaskMemoContext | None:
        if not regenerate:
            return None
        memo = self._repository.get_current_ready_memo(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        if memo is None:
            return None
        return _previous_memo(memo)


def _derive_memo_mode(
    *,
    allowed_evidence_refs: frozenset[str],
    allowed_knowledge_refs: frozenset[str],
    runtime_readiness: RuntimeReadiness,
) -> MemoMode | None:
    if allowed_evidence_refs or allowed_knowledge_refs:
        return MEMO_MODE_SUPPORTED
    if runtime_readiness.useful_runtime_execution:
        return MEMO_MODE_LIMITED
    return None


def _not_preparable_reason(
    *,
    memo_mode: MemoMode | None,
    runtime_readiness: RuntimeReadiness,
) -> ReportingReasonCode | None:
    if memo_mode is not None:
        return None
    return (
        runtime_readiness.not_preparable_reason
        or REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL
    )


def _task_metadata(task: Task) -> TaskMemoTaskMetadata:
    return TaskMemoTaskMetadata(
        task_id=int(task.id),
        tenant_id=int(task.tenant_id),
        user_id=int(task.user_id),
        engagement_id=int(task.engagement_id),
        name=str(task.name),
        description=_optional_text(task.description),
        scope=_optional_text(task.scope),
        status=str(task.status),
        created_at=_datetime_to_json(task.created_at),
        stopped_at=_datetime_to_json(task.stopped_at),
    )


def _previous_memo(memo: TaskClosureMemo) -> PreviousTaskMemoContext:
    body = memo.memo if isinstance(memo.memo, Mapping) else {}
    summary = _compact_text(body.get("summary"), _PREVIOUS_SUMMARY_MAX_CHARACTERS)
    return PreviousTaskMemoContext(
        memo_id=str(memo.id),
        version=int(memo.version),
        memo_mode=str(memo.memo_mode),
        generated_at=_datetime_to_json(memo.generated_at),
        summary=summary or None,
        body=_freeze_json_mapping(body),
    )


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
    if value is None or max_characters <= 0:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= max_characters:
        return text
    if max_characters <= 3:
        return text[:max_characters]
    return text[: max_characters - 3].rstrip() + "..."


def _datetime_to_json(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "PreviousTaskMemoContext",
    "TaskMemoContext",
    "TaskMemoContextBuilder",
    "TaskMemoTaskMetadata",
]
