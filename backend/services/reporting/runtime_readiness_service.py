"""Compute durable task runtime readiness for reporting preparation.

This module reads task-local database rows to determine whether a task runtime
was retired and whether the task produced enough runtime execution signal to be
eligible for later reporting inventory steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.chat import AgentLog, ChatTurnEvent
from backend.models.core import Task, TaskHistory
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.runner_control import RunnerControlMessage, RuntimeJob
from backend.models.streaming import StreamEvent, SystemLog
from backend.services.reporting.contracts import (
    REASON_NO_USEFUL_RUNTIME_EXECUTION,
    REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED,
    REASON_TASK_NOT_STOPPED,
    ReportingReasonCode,
)

_RUNNER_RUNTIME_PLACEMENT = "runner"
_RUNTIME_STOP_JOB_TYPE = "task.stop"
_RUNTIME_RETIRE_JOB_TYPE = "task.retire"
_RUNTIME_STOPPED_EVENT_TYPE = "runtime.stopped"
_RUNTIME_RETIRED_EVENT_TYPE = "runtime.retired"
_RUNTIME_STOPPED_OUTCOME = "stopped"
_RUNTIME_RETIRED_OUTCOME = "retired"
_SUCCESSFUL_RUNTIME_JOB_STATUSES = frozenset({"succeeded"})
_SUCCESSFUL_CONTROL_MESSAGE_STATUSES = frozenset({"accepted", "succeeded"})
_CURRENT_RUNTIME_INTERVAL_START_STATUSES = frozenset(
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
_USEFUL_SYSTEM_EVENT_TYPES = frozenset(
    {
        "agent_log",
        "agent_message",
        "artifact",
        "chat_turn",
        "observation",
        "reasoning",
        "runtime.logs",
        "runtime.metrics",
        "runtime.output",
        "terminal.frame",
        "tool",
        "tool_result",
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    """Reporting readiness signals derived from durable runtime rows."""

    runtime_retired: bool
    useful_runtime_execution: bool
    not_preparable_reason: ReportingReasonCode | None


class RuntimeReadinessService:
    """Compute reporting runtime readiness without mutating task state."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute_for_task(self, *, tenant_id: int, task_id: int) -> RuntimeReadiness:
        """Return readiness for one tenant-scoped task."""

        task = (
            self._db.query(Task)
            .filter(Task.tenant_id == tenant_id, Task.id == task_id)
            .one_or_none()
        )
        if task is None:
            return RuntimeReadiness(
                runtime_retired=False,
                useful_runtime_execution=False,
                not_preparable_reason=REASON_TASK_NOT_STOPPED,
            )

        useful_runtime_execution = self._has_useful_runtime_execution(
            tenant_id=tenant_id,
            task_id=task_id,
        )
        runtime_retired = self._is_runtime_retired(task=task)

        return RuntimeReadiness(
            runtime_retired=runtime_retired,
            useful_runtime_execution=useful_runtime_execution,
            not_preparable_reason=self._not_preparable_reason(
                task=task,
                runtime_retired=runtime_retired,
                useful_runtime_execution=useful_runtime_execution,
            ),
        )

    def _is_runtime_retired(self, *, task: Task) -> bool:
        status = _normalize_text(getattr(task, "status", None))
        if status != TaskStatus.STOPPED.value:
            return False

        if _normalize_text(getattr(task, "runtime_placement_mode", None)) != _RUNNER_RUNTIME_PLACEMENT:
            return True

        return self._has_runner_retirement_signal(
            tenant_id=int(task.tenant_id),
            task_id=int(task.id),
        )

    def _has_runner_retirement_signal(self, *, tenant_id: int, task_id: int) -> bool:
        current_interval_started_at = self._latest_runtime_interval_started_at(
            tenant_id=tenant_id,
            task_id=task_id,
        )

        history_rows = (
            self._db.query(TaskHistory.change_metadata)
            .filter(TaskHistory.tenant_id == tenant_id, TaskHistory.task_id == task_id)
            .filter(
                TaskHistory.timestamp >= current_interval_started_at
                if current_interval_started_at is not None
                else True
            )
            .all()
        )
        if any(_contains_runtime_retirement_signal(row.change_metadata) for row in history_rows):
            return True

        runtime_jobs = (
            self._db.query(RuntimeJob)
            .filter(RuntimeJob.tenant_id == tenant_id, RuntimeJob.task_id == task_id)
            .filter(
                or_(
                    RuntimeJob.created_at >= current_interval_started_at,
                    RuntimeJob.updated_at >= current_interval_started_at,
                )
                if current_interval_started_at is not None
                else True
            )
            .all()
        )
        if any(_runtime_job_records_retirement(job) for job in runtime_jobs):
            return True

        control_messages = (
            self._db.query(RunnerControlMessage)
            .filter(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.task_id == task_id,
            )
            .filter(
                RunnerControlMessage.created_at >= current_interval_started_at
                if current_interval_started_at is not None
                else True
            )
            .all()
        )
        return any(_control_message_records_retirement(message) for message in control_messages)

    def _has_useful_runtime_execution(self, *, tenant_id: int, task_id: int) -> bool:
        running_started_at = self._latest_running_started_at(tenant_id=tenant_id, task_id=task_id)
        if running_started_at is not None:
            return True
        if self._has_tool_or_artifact_rows(tenant_id=tenant_id, task_id=task_id):
            return True
        return self._has_useful_task_events(tenant_id=tenant_id, task_id=task_id)

    def _latest_runtime_interval_started_at(self, *, tenant_id: int, task_id: int) -> Any | None:
        row = (
            self._db.query(TaskHistory.timestamp)
            .filter(
                TaskHistory.tenant_id == tenant_id,
                TaskHistory.task_id == task_id,
                TaskHistory.new_status.in_(_CURRENT_RUNTIME_INTERVAL_START_STATUSES),
            )
            .order_by(TaskHistory.timestamp.desc(), TaskHistory.id.desc())
            .first()
        )
        return row.timestamp if row is not None else None

    def _latest_running_started_at(self, *, tenant_id: int, task_id: int) -> Any | None:
        row = (
            self._db.query(TaskHistory.timestamp)
            .filter(
                TaskHistory.tenant_id == tenant_id,
                TaskHistory.task_id == task_id,
                TaskHistory.new_status == TaskStatus.RUNNING.value,
            )
            .order_by(TaskHistory.timestamp.desc(), TaskHistory.id.desc())
            .first()
        )
        return row.timestamp if row is not None else None

    def _has_tool_or_artifact_rows(self, *, tenant_id: int, task_id: int) -> bool:
        tool_execution = (
            self._db.query(ToolExecution.id)
            .filter(ToolExecution.tenant_id == tenant_id, ToolExecution.task_id == task_id)
            .first()
        )
        if tool_execution is not None:
            return True

        return (
            self._db.query(ExecutionArtifact.id)
            .filter(ExecutionArtifact.tenant_id == tenant_id, ExecutionArtifact.task_id == task_id)
            .first()
            is not None
        )

    def _has_useful_task_events(self, *, tenant_id: int, task_id: int) -> bool:
        chat_turn = (
            self._db.query(ChatTurnEvent.event_metadata)
            .filter(ChatTurnEvent.tenant_id == tenant_id, ChatTurnEvent.task_id == task_id)
            .all()
        )
        if any(_metadata_proves_post_start_event(row.event_metadata) for row in chat_turn):
            return True

        agent_event = (
            self._db.query(AgentLog.log_metadata)
            .filter(AgentLog.tenant_id == tenant_id, AgentLog.task_id == task_id)
            .filter(AgentLog.type.in_(_USEFUL_SYSTEM_EVENT_TYPES))
            .all()
        )
        if any(_metadata_proves_post_start_event(row.log_metadata) for row in agent_event):
            return True

        system_event = (
            self._db.query(SystemLog.log_metadata)
            .filter(SystemLog.tenant_id == tenant_id, SystemLog.task_id == task_id)
            .filter(SystemLog.type.in_(_USEFUL_SYSTEM_EVENT_TYPES))
            .all()
        )
        if any(_metadata_proves_post_start_event(row.log_metadata) for row in system_event):
            return True

        stream_event = (
            self._db.query(StreamEvent.payload)
            .filter(StreamEvent.tenant_id == tenant_id, StreamEvent.task_id == task_id)
            .filter(StreamEvent.event_type.in_(_USEFUL_SYSTEM_EVENT_TYPES))
            .all()
        )
        return any(_metadata_proves_post_start_event(row.payload) for row in stream_event)

    @staticmethod
    def _not_preparable_reason(
        *,
        task: Task,
        runtime_retired: bool,
        useful_runtime_execution: bool,
    ) -> ReportingReasonCode | None:
        if _normalize_text(getattr(task, "status", None)) != TaskStatus.STOPPED.value:
            return REASON_TASK_NOT_STOPPED
        if not runtime_retired:
            return REASON_RUNTIME_RETIREMENT_NOT_CONFIRMED
        if not useful_runtime_execution:
            return REASON_NO_USEFUL_RUNTIME_EXECUTION
        return None


def _contains_runtime_retirement_signal(value: Any) -> bool:
    if isinstance(value, Mapping):
        if _normalize_text(value.get("message_type")) == _RUNTIME_RETIRED_EVENT_TYPE:
            return True
        if _normalize_text(value.get("runtime_event_type")) == _RUNTIME_RETIRED_EVENT_TYPE:
            return True
        if _normalize_text(value.get("message_type")) == _RUNTIME_STOPPED_EVENT_TYPE and (
            _normalize_text(value.get("lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME
            or _normalize_text(value.get("runtime_event_lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME
        ):
            return True
        if _normalize_text(value.get("runtime_event_type")) == _RUNTIME_STOPPED_EVENT_TYPE and (
            _normalize_text(value.get("lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME
            or _normalize_text(value.get("runtime_event_lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME
        ):
            return True
        if _normalize_text(value.get("lifecycle_outcome")) == _RUNTIME_RETIRED_OUTCOME:
            return True
        if _normalize_text(value.get("runtime_event_lifecycle_outcome")) == _RUNTIME_RETIRED_OUTCOME:
            return True
        return any(_contains_runtime_retirement_signal(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_contains_runtime_retirement_signal(item) for item in value)
    return False


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


def _runtime_job_records_retirement(job: RuntimeJob) -> bool:
    if _normalize_text(job.status) not in _SUCCESSFUL_RUNTIME_JOB_STATUSES:
        return False

    job_type = _normalize_text(job.job_type)
    if job_type in {_RUNTIME_RETIRE_JOB_TYPE, _RUNTIME_RETIRED_EVENT_TYPE}:
        return True
    if job_type in {_RUNTIME_STOP_JOB_TYPE, _RUNTIME_STOPPED_EVENT_TYPE} and (
        _contains_runtime_stop_signal(job.payload_json)
        or _contains_runtime_stop_signal(job.result_json)
    ):
        return True

    return _contains_runtime_retirement_signal(job.payload_json) or _contains_runtime_retirement_signal(job.result_json)


def _control_message_records_retirement(message: RunnerControlMessage) -> bool:
    if _normalize_text(message.status) not in _SUCCESSFUL_CONTROL_MESSAGE_STATUSES:
        return False
    return (
        _normalize_text(message.type) == _RUNTIME_RETIRED_EVENT_TYPE
        or (
            _normalize_text(message.type) == _RUNTIME_STOPPED_EVENT_TYPE
            and _contains_runtime_stop_signal(message.payload_json)
        )
        or _contains_runtime_retirement_signal(message.payload_json)
    )


def _contains_runtime_stop_signal(value: Any) -> bool:
    if isinstance(value, Mapping):
        if _normalize_text(value.get("lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME:
            return True
        if _normalize_text(value.get("runtime_event_lifecycle_outcome")) == _RUNTIME_STOPPED_OUTCOME:
            return True
        result = value.get("result")
        if result is not value and _contains_runtime_stop_signal(result):
            return True
        return any(_contains_runtime_stop_signal(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return any(_contains_runtime_stop_signal(item) for item in value)
    return False


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


__all__ = ["RuntimeReadiness", "RuntimeReadinessService"]
