"""Compute task-local reporting input inventory source signals.

This service reads durable database rows for engagement-owned tasks. It does
not inspect workspaces, call LLMs, generate memos/reports, or mutate reporting
state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Query, Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import ChatTurnEvent
from backend.models.core import Engagement, Task, TaskHistory, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeObservation,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.reporting import TaskClosureMemo
from backend.models.tenant import Tenant
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.schemas.reporting import (
    EngagementReportingInputsResponse,
    ReportingInputTaskRow,
    ReportingSourceCounts,
    SourceWatermarkSnapshot,
    TaskClosureMemoSummary,
)
from backend.services.reporting.contracts import (
    InputState,
    MEMO_MODE_LIMITED,
    MEMO_MODE_SUPPORTED,
    MemoMode,
    REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL,
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    REASON_TASK_NOT_STOPPED,
    ReportingReasonCode,
)
from backend.services.reporting.reporting_state_service import ReportingStateService
from backend.services.reporting.runtime_readiness_service import RuntimeReadiness
from backend.services.reporting.runtime_readiness_service import RuntimeReadinessService
from backend.services.reporting.source_watermark_service import SourceWatermarkService

_ACTIVE_TENANT_STATUS = "active"
_RUNTIME_START_STATUSES = frozenset(
    {
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
    }
)
_POST_START_METADATA_VALUES = frozenset(
    {
        "post_start",
        "runtime.running",
        "running",
    }
)
_FINDING_ENTITY_TYPE = "finding"
_CANDIDATE_ASSERTION_LEVEL = "candidate"


@dataclass(frozen=True, slots=True)
class _SourceCounts:
    """Task-local durable reporting source counts."""

    evidence_count: int
    canonical_finding_count: int
    candidate_finding_count: int

    @property
    def has_default_reportable_sources(self) -> bool:
        """Return whether counts include source material reportable by default."""

        return self.evidence_count > 0 or self.canonical_finding_count > 0


@dataclass(frozen=True, slots=True)
class _UsefulExecutionMarkers:
    """Task-local durable execution markers useful for limited input mode."""

    task_history_count: int
    tool_execution_count: int
    execution_artifact_count: int
    post_start_chat_turn_event_count: int

    @property
    def has_useful_runtime_execution(self) -> bool:
        """Return whether any durable execution marker exists."""

        return any(
            (
                self.task_history_count,
                self.tool_execution_count,
                self.execution_artifact_count,
                self.post_start_chat_turn_event_count,
            )
        )


@dataclass(frozen=True, slots=True)
class _InputEligibilityProjection:
    """Internal task row projection for future reporting input inventory."""

    task_id: int
    task_name: str
    task_status: str
    runtime_retired: bool
    is_preparable: bool
    is_reportable: bool
    memo_mode: MemoMode | None
    not_preparable_reason: ReportingReasonCode | None
    input_state: InputState
    current_memo: TaskClosureMemo | None
    latest_memo_attempt: TaskClosureMemo | None
    source_watermark: dict[str, Any]
    counts: _SourceCounts
    candidate_findings_require_explicit_inclusion: bool


class ReportingInputInventoryNotFoundError(ValueError):
    """Raised when an owned active engagement cannot be found for inventory reads."""


class InputInventoryService:
    """Read task-local source counts for future reporting input rows."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repository = TaskClosureMemoRepository(db)
        self._runtime_readiness = RuntimeReadinessService(db)
        self._source_watermarks = SourceWatermarkService(db)
        self._reporting_state = ReportingStateService(db, repository=self._repository)

    def list_engagement_inputs(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> EngagementReportingInputsResponse:
        """Return a typed reporting input inventory for one owned engagement."""

        engagement = self._get_owned_active_engagement_or_raise(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
        )
        task_rows = self._project_engagement_task_inputs(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=int(engagement.id),
        )
        return EngagementReportingInputsResponse(
            engagement_id=int(engagement.id),
            tasks=[_to_reporting_input_task_row(row) for row in task_rows],
        )

    def _get_owned_active_engagement_or_raise(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> Engagement:
        engagement = (
            self._db.query(Engagement)
            .join(Tenant, Tenant.id == Engagement.tenant_id)
            .join(User, User.id == Engagement.user_id)
            .filter(
                Engagement.id == int(engagement_id),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Tenant.id == int(tenant_id),
                Tenant.status == _ACTIVE_TENANT_STATUS,
                User.id == int(user_id),
                User.is_active.is_(True),
            )
            .one_or_none()
        )
        if engagement is None:
            raise ReportingInputInventoryNotFoundError("Engagement not found")
        return engagement

    def _project_engagement_task_inputs(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> list[_InputEligibilityProjection]:
        tasks = (
            self._db.query(Task)
            .join(Engagement, Engagement.id == Task.engagement_id)
            .join(Tenant, Tenant.id == Task.tenant_id)
            .join(User, User.id == Task.user_id)
            .filter(
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                Engagement.id == int(engagement_id),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Tenant.id == int(tenant_id),
                Tenant.status == _ACTIVE_TENANT_STATUS,
                User.id == int(user_id),
                User.is_active.is_(True),
            )
            .order_by(Task.created_at.asc(), Task.id.asc())
            .all()
        )
        return [
            self._project_task_input(
                task=task,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
            )
            for task in tasks
        ]

    def _project_task_input(
        self,
        *,
        task: Task,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
    ) -> _InputEligibilityProjection:
        counts = self._source_counts(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=int(task.id),
        )
        useful_markers = self._useful_execution_markers(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=int(task.id),
        )
        runtime_readiness = self._runtime_readiness.compute_for_task(
            tenant_id=tenant_id,
            task_id=int(task.id),
        )
        memo_mode = _memo_mode_for_sources(
            counts=counts,
            has_useful_runtime_execution=(
                runtime_readiness.useful_runtime_execution
                or useful_markers.has_useful_runtime_execution
            ),
        )
        not_preparable_reason = _not_preparable_reason_for_projection(
            task_status=task.status,
            memo_mode=memo_mode,
            runtime_readiness=runtime_readiness,
        )
        source_watermark = self._source_watermarks.compute_for_task(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=int(task.id),
        )
        input_state = self._reporting_state.project_memo_input_state(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=int(task.id),
            current_source_watermark=source_watermark,
        )

        return _InputEligibilityProjection(
            task_id=int(task.id),
            task_name=str(task.name or ""),
            task_status=str(task.status or ""),
            runtime_retired=runtime_readiness.runtime_retired,
            is_preparable=not_preparable_reason is None,
            is_reportable=(
                not_preparable_reason is None
                and memo_mode == MEMO_MODE_SUPPORTED
                and counts.has_default_reportable_sources
            ),
            memo_mode=memo_mode,
            not_preparable_reason=not_preparable_reason,
            input_state=input_state.input_state,
            current_memo=input_state.current_memo,
            latest_memo_attempt=input_state.latest_attempt,
            source_watermark=input_state.current_source_watermark,
            counts=counts,
            candidate_findings_require_explicit_inclusion=counts.candidate_finding_count > 0,
        )

    def _source_counts(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> _SourceCounts:
        return _SourceCounts(
            evidence_count=self._evidence_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            canonical_finding_count=self._canonical_finding_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            candidate_finding_count=self._candidate_finding_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
        )

    def _evidence_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        query = self._scoped_lineage_query(
            KnowledgeEvidenceArchive,
            func.count(KnowledgeEvidenceArchive.id),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        return _int_count(query.scalar())

    def _canonical_finding_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        findings = self._scoped_finding_provenance_query(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        ).all()
        return sum(1 for finding in findings if not _finding_is_candidate_only(finding))

    def _candidate_finding_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        candidate_keys: set[str] = set()

        observations = self._scoped_lineage_query(
            KnowledgeObservation,
            KnowledgeObservation.id,
            KnowledgeObservation.subject_key,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        ).filter(
            func.lower(KnowledgeObservation.assertion_level) == _CANDIDATE_ASSERTION_LEVEL,
            or_(
                func.lower(KnowledgeObservation.observation_type).like("finding%"),
                func.lower(KnowledgeObservation.subject_type).like("finding%"),
            ),
        )
        for observation_id, subject_key in observations.all():
            candidate_keys.add(_candidate_key("observation", subject_key, observation_id))

        findings = self._scoped_finding_provenance_query(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        ).all()
        for finding in findings:
            if _finding_is_candidate_only(finding):
                candidate_keys.add(_candidate_key("finding", finding.finding_key, finding.id))

        return len(candidate_keys)

    def _useful_execution_markers(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> _UsefulExecutionMarkers:
        return _UsefulExecutionMarkers(
            task_history_count=self._task_history_execution_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            tool_execution_count=self._tool_execution_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            execution_artifact_count=self._execution_artifact_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
            post_start_chat_turn_event_count=self._post_start_chat_turn_event_count(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
            ),
        )

    def _task_history_execution_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        query = self._scoped_lineage_query(
            TaskHistory,
            func.count(TaskHistory.id),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            require_lineage_owner_columns=False,
        ).filter(TaskHistory.new_status == TaskStatus.RUNNING.value)
        return _int_count(query.scalar())

    def _tool_execution_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        query = self._scoped_lineage_query(
            ToolExecution,
            func.count(ToolExecution.id),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            require_lineage_owner_columns=False,
        )
        return _int_count(query.scalar())

    def _execution_artifact_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        query = self._scoped_lineage_query(
            ExecutionArtifact,
            func.count(ExecutionArtifact.id),
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            require_lineage_owner_columns=False,
        )
        return _int_count(query.scalar())

    def _post_start_chat_turn_event_count(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> int:
        runtime_started_at = self._latest_runtime_started_at(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
        )
        query = self._scoped_lineage_query(
            ChatTurnEvent,
            ChatTurnEvent.id,
            ChatTurnEvent.event_metadata,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            require_lineage_owner_columns=False,
        )
        if runtime_started_at is not None:
            query = query.filter(ChatTurnEvent.created_at >= runtime_started_at)
            return len(query.all())
        return sum(1 for _event_id, metadata in query.all() if _metadata_proves_post_start_event(metadata))

    def _latest_runtime_started_at(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Any | None:
        row = (
            self._scoped_lineage_query(
                TaskHistory,
                TaskHistory.timestamp,
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=task_id,
                require_lineage_owner_columns=False,
            )
            .filter(TaskHistory.new_status.in_(_RUNTIME_START_STATUSES))
            .order_by(TaskHistory.timestamp.desc(), TaskHistory.id.desc())
            .first()
        )
        return row.timestamp if row is not None else None

    def _scoped_finding_provenance_query(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> Query:
        provenance_exists = exists().where(
            KnowledgeEntityProvenance.entity_type == _FINDING_ENTITY_TYPE,
            KnowledgeEntityProvenance.entity_id == KnowledgeFinding.id,
            KnowledgeEntityProvenance.tenant_id == KnowledgeFinding.tenant_id,
            KnowledgeEntityProvenance.user_id == KnowledgeFinding.user_id,
            KnowledgeEntityProvenance.tenant_id == int(tenant_id),
            KnowledgeEntityProvenance.user_id == int(user_id),
            KnowledgeEntityProvenance.engagement_id == int(engagement_id),
            KnowledgeEntityProvenance.task_id == int(task_id),
            Task.id == KnowledgeEntityProvenance.task_id,
            Task.id == int(task_id),
            Task.tenant_id == int(tenant_id),
            Task.user_id == int(user_id),
            Task.engagement_id == int(engagement_id),
            Engagement.id == int(engagement_id),
            Engagement.id == Task.engagement_id,
            Engagement.tenant_id == int(tenant_id),
            Engagement.user_id == int(user_id),
            Tenant.id == int(tenant_id),
            Tenant.id == Task.tenant_id,
            Tenant.status == _ACTIVE_TENANT_STATUS,
            User.id == int(user_id),
            User.id == Task.user_id,
            User.is_active.is_(True),
        )
        return self._db.query(KnowledgeFinding).filter(
            KnowledgeFinding.tenant_id == int(tenant_id),
            KnowledgeFinding.user_id == int(user_id),
            or_(
                KnowledgeFinding.engagement_id == int(engagement_id),
                KnowledgeFinding.engagement_id.is_(None),
            ),
            provenance_exists,
        )

    def _scoped_lineage_query(
        self,
        lineage_model: Any,
        *entities: Any,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        require_lineage_owner_columns: bool = True,
        query: Query | None = None,
    ) -> Query:
        scoped_query = query if query is not None else self._db.query(*entities).select_from(lineage_model)
        scoped_query = (
            scoped_query.join(Task, Task.id == lineage_model.task_id)
            .join(Engagement, Engagement.id == Task.engagement_id)
            .join(Tenant, Tenant.id == Task.tenant_id)
            .join(User, User.id == Task.user_id)
            .filter(
                lineage_model.tenant_id == int(tenant_id),
                lineage_model.task_id == int(task_id),
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
                Task.user_id == int(user_id),
                Task.engagement_id == int(engagement_id),
                Engagement.id == int(engagement_id),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Tenant.id == int(tenant_id),
                Tenant.status == _ACTIVE_TENANT_STATUS,
                User.id == int(user_id),
                User.is_active.is_(True),
            )
        )
        if require_lineage_owner_columns and hasattr(lineage_model, "user_id"):
            scoped_query = scoped_query.filter(lineage_model.user_id == int(user_id))
        if require_lineage_owner_columns and hasattr(lineage_model, "engagement_id"):
            scoped_query = scoped_query.filter(lineage_model.engagement_id == int(engagement_id))
        return scoped_query


def _finding_is_candidate_only(finding: KnowledgeFinding) -> bool:
    status = _normalize_text(finding.status)
    assertion_level = _normalize_text(finding.assertion_level)
    if status == _CANDIDATE_ASSERTION_LEVEL or assertion_level == _CANDIDATE_ASSERTION_LEVEL:
        return True

    metadata = dict(finding.finding_metadata or {})
    authority = dict(metadata.get("authority") or {})
    if bool(authority.get("candidate_only")):
        return True
    return _normalize_text(authority.get("source_kind")) == "llm_candidate"


def _candidate_key(prefix: str, logical_key: Any, fallback_id: Any) -> str:
    normalized_key = str(logical_key or "").strip()
    if normalized_key:
        return normalized_key
    return f"{prefix}:{fallback_id}"


def _metadata_proves_post_start_event(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _normalize_text(key)
            normalized_item = _normalize_text(item)
            if normalized_key in {
                "execution_phase",
                "lifecycle_phase",
                "message_type",
                "runtime_event_type",
                "runtime_phase",
                "runtime_status",
                "task_status",
            } and normalized_item in _POST_START_METADATA_VALUES:
                return True
            if _metadata_proves_post_start_event(item):
                return True
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_metadata_proves_post_start_event(item) for item in value)
    return False


def _memo_mode_for_sources(
    *,
    counts: _SourceCounts,
    has_useful_runtime_execution: bool,
) -> MemoMode | None:
    if counts.has_default_reportable_sources:
        return MEMO_MODE_SUPPORTED
    if has_useful_runtime_execution:
        return MEMO_MODE_LIMITED
    return None


def _not_preparable_reason_for_projection(
    *,
    task_status: Any,
    memo_mode: MemoMode | None,
    runtime_readiness: RuntimeReadiness,
) -> ReportingReasonCode | None:
    if _normalize_text(task_status) != TaskStatus.STOPPED.value:
        return runtime_readiness.not_preparable_reason or REASON_TASK_NOT_STOPPED
    if not runtime_readiness.runtime_retired:
        return runtime_readiness.not_preparable_reason or REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED
    if memo_mode is None:
        return REASON_NO_REPORTABLE_OR_LIMITED_SOURCE_MATERIAL
    return None


def _int_count(value: Any) -> int:
    return int(value or 0)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _to_reporting_input_task_row(row: _InputEligibilityProjection) -> ReportingInputTaskRow:
    return ReportingInputTaskRow(
        task_id=row.task_id,
        task_name=row.task_name,
        task_status=row.task_status,
        runtime_retired=row.runtime_retired,
        is_reportable=row.is_reportable,
        is_preparable=row.is_preparable,
        memo_mode=row.memo_mode,
        not_preparable_reason=row.not_preparable_reason,
        input_state=row.input_state,
        current_memo=_memo_summary(row.current_memo),
        latest_memo_attempt=_memo_summary(row.latest_memo_attempt),
        source_watermark=_watermark_snapshot(row.source_watermark),
        counts=ReportingSourceCounts(
            evidence=row.counts.evidence_count,
            canonical_findings=row.counts.canonical_finding_count,
            candidate_findings=row.counts.candidate_finding_count,
        ),
        candidate_findings_require_explicit_inclusion=(
            row.candidate_findings_require_explicit_inclusion
        ),
    )


def _memo_summary(memo: TaskClosureMemo | None) -> TaskClosureMemoSummary | None:
    if memo is None:
        return None
    return TaskClosureMemoSummary(
        id=memo.id,
        version=int(memo.version),
        status=str(memo.status),
        memo_mode=str(memo.memo_mode),
        is_current=bool(memo.is_current),
        source_watermark=_watermark_snapshot(memo.source_watermark or {}),
        error_message=memo.error_message,
        created_at=memo.created_at,
        updated_at=memo.updated_at,
        generated_at=memo.generated_at,
    )


def _watermark_snapshot(source_watermark: Mapping[str, Any]) -> SourceWatermarkSnapshot:
    sources = source_watermark.get("sources")
    if not isinstance(sources, Mapping):
        sources = {}

    chat_messages = _mapping_value(sources, "chat_messages")
    chat_turn_events = _mapping_value(sources, "chat_turn_events")
    tool_executions = _mapping_value(sources, "tool_executions")
    evidence = _mapping_value(sources, "knowledge_evidence_archives")
    observations = _mapping_value(sources, "knowledge_observations")
    provenance = _mapping_value(sources, "knowledge_entity_provenance")

    return SourceWatermarkSnapshot(
        last_chat_message_id=_int_or_none(chat_messages.get("latest_id")),
        last_turn_sequence=_latest_int_value(
            chat_messages.get("latest_turn_number"),
            chat_turn_events.get("latest_turn_number"),
        ),
        latest_tool_execution_id=_str_or_none(tool_executions.get("latest_id")),
        latest_evidence_created_at=evidence.get("latest_created_at"),
        latest_knowledge_observed_at=_latest_datetime_value(
            observations.get("latest_observed_at"),
            provenance.get("latest_observed_at"),
        ),
    )


def _mapping_value(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _latest_int_value(*values: Any) -> int | None:
    normalized = [_int_or_none(value) for value in values]
    present_values = [value for value in normalized if value is not None]
    if not present_values:
        return None
    return max(present_values)


def _latest_datetime_value(*values: Any) -> Any | None:
    present_values = [value for value in values if value is not None]
    if not present_values:
        return None
    return max(str(value) for value in present_values)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    return normalized or None


__all__ = ["InputInventoryService", "ReportingInputInventoryNotFoundError"]
