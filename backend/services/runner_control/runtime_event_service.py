"""Runner runtime-event ingest service for remote runtime / tooling plane / data plane backend-side effects.

Scope:
- Validates remote runtime event operation family against assigned runtime jobs.
- Validates tooling plane tool-result payload transitions against assigned tool jobs.
- Applies runtime-job transitions from runner-originated result events.
- Promotes accepted data plane `tool.result` payloads into provenance artifacts.
- Applies task-state transitions for lifecycle events when domain rules allow.
- Publishes best-effort browser-facing task events for accepted runtime events.

Boundaries:
- Consumes already-authenticated/channel-bound envelopes only.
- Does not manage websocket session auth, ack delivery, or outbound dispatch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task
from backend.models.provenance import ExecutionArtifact
from backend.models.runner_control import RuntimeJob
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from backend.services.artifact.runner_result_ingest_service import RunnerResultIngestService
from backend.services.runner_control.audit import RunnerControlAuditEmitter, RunnerControlAuditService
from backend.services.runner_control.terminal_frame_buffer import get_runner_terminal_frame_buffer
from backend.services.runner_control.runtime_job_service import RuntimeJobService, RuntimeJobServiceError
from backend.services.task.retirement_service import TaskRetirementService
from backend.services.task.state_service import TaskStateService
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.runner_protocol import (
    RUNNER_TERMINAL_FRAME_MAX_BYTES,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerRuntimeInputResultPayload,
    RunnerRuntimeLogsResultPayload,
    RunnerRuntimeMetricsResultPayload,
    RunnerRuntimeOperationResultPayload,
    RunnerRuntimeStartupProgressResultPayload,
    RunnerRuntimeStatusResultPayload,
    RunnerRuntimeVpnConfigResultPayload,
    RunnerRuntimeVpnRetryResultPayload,
    RunnerRuntimeVpnStatusResultPayload,
    RunnerToolResultPayload,
    RunnerTerminalFramePayload,
    RunnerTerminalResultPayload,
    is_completed_process_tool_result_status,
    sanitize_tool_result_payload_for_persistence,
)

logger = logging.getLogger(__name__)
_RUNNER_RESULT_PROMOTION_ERROR_CODE = "RUNNER_RESULT_PROMOTION_FAILED"
_RUNNER_ARTIFACT_PROMOTION_REQUIRED = "RUNNER_ARTIFACT_PROMOTION_REQUIRED"
_RUNNER_ARTIFACT_UPLOAD_FAILED = "RUNNER_ARTIFACT_UPLOAD_FAILED"
_RUNTIME_EVENT_PUBLISH_LOOP: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "runtime_event_publish_loop",
    default=None,
)

_DIRECT_EVENT_JOB_TYPE_MAP: dict[RunnerMessageType, str] = {
    RunnerMessageType.RUNTIME_INPUT: RunnerMessageType.RUNTIME_INPUT.value,
    RunnerMessageType.RUNTIME_STARTUP_PROGRESS: RunnerMessageType.RUNTIME_STARTUP_PROGRESS.value,
    RunnerMessageType.RUNTIME_STATUS: RunnerMessageType.RUNTIME_STATUS.value,
    RunnerMessageType.RUNTIME_LOGS: RunnerMessageType.RUNTIME_LOGS.value,
    RunnerMessageType.RUNTIME_METRICS: RunnerMessageType.RUNTIME_METRICS.value,
    RunnerMessageType.RUNTIME_INVENTORY: RunnerMessageType.RUNTIME_INVENTORY.value,
    RunnerMessageType.RUNTIME_WORKSPACE_QUERY: RunnerMessageType.RUNTIME_WORKSPACE_QUERY.value,
    RunnerMessageType.RUNTIME_WORKSPACE_READ: RunnerMessageType.RUNTIME_WORKSPACE_READ.value,
    RunnerMessageType.RUNTIME_WORKSPACE_WRITE: RunnerMessageType.RUNTIME_WORKSPACE_WRITE.value,
    RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP: RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP.value,
    RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA: RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA.value,
    RunnerMessageType.RUNTIME_VPN_STATUS: RunnerMessageType.RUNTIME_VPN_STATUS.value,
    RunnerMessageType.RUNTIME_VPN_RETRY: RunnerMessageType.RUNTIME_VPN_RETRY.value,
    RunnerMessageType.RUNTIME_VPN_CONFIG: RunnerMessageType.RUNTIME_VPN_CONFIG.value,
    RunnerMessageType.RUNTIME_STARTED: RunnerMessageType.TASK_START.value,
    RunnerMessageType.RUNTIME_PAUSED: RunnerMessageType.TASK_PAUSE.value,
    RunnerMessageType.RUNTIME_RESUMED: RunnerMessageType.TASK_RESUME.value,
    RunnerMessageType.RUNTIME_STOPPED: RunnerMessageType.TASK_STOP.value,
    RunnerMessageType.RUNTIME_RETIRED: RunnerMessageType.TASK_RETIRE.value,
}
_RUNTIME_FAILED_ALLOWED_JOB_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START.value,
        RunnerMessageType.TASK_STOP.value,
        RunnerMessageType.TASK_PAUSE.value,
        RunnerMessageType.TASK_RESUME.value,
        RunnerMessageType.TASK_RETIRE.value,
        RunnerMessageType.RUNTIME_INPUT.value,
        RunnerMessageType.RUNTIME_STARTUP_PROGRESS.value,
        RunnerMessageType.RUNTIME_STATUS.value,
        RunnerMessageType.RUNTIME_LOGS.value,
        RunnerMessageType.RUNTIME_METRICS.value,
        RunnerMessageType.RUNTIME_INVENTORY.value,
        RunnerMessageType.RUNTIME_WORKSPACE_QUERY.value,
        RunnerMessageType.RUNTIME_WORKSPACE_READ.value,
        RunnerMessageType.RUNTIME_WORKSPACE_WRITE.value,
        RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP.value,
        RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA.value,
        RunnerMessageType.RUNTIME_VPN_STATUS.value,
        RunnerMessageType.RUNTIME_VPN_RETRY.value,
        RunnerMessageType.RUNTIME_VPN_CONFIG.value,
        RunnerMessageType.TERMINAL_OPEN.value,
        RunnerMessageType.TERMINAL_INPUT.value,
        RunnerMessageType.TERMINAL_RESIZE.value,
        RunnerMessageType.TERMINAL_CLOSE.value,
    }
)
_RESULT_PAYLOAD_TYPES = (
    RunnerRuntimeOperationResultPayload,
    RunnerRuntimeInputResultPayload,
    RunnerRuntimeStartupProgressResultPayload,
    RunnerRuntimeStatusResultPayload,
    RunnerRuntimeLogsResultPayload,
    RunnerRuntimeMetricsResultPayload,
    RunnerRuntimeVpnStatusResultPayload,
    RunnerRuntimeVpnRetryResultPayload,
    RunnerRuntimeVpnConfigResultPayload,
    RunnerToolResultPayload,
    RunnerTerminalResultPayload,
)
_TASK_STATUS_BY_EVENT: dict[RunnerMessageType, str] = {
    RunnerMessageType.RUNTIME_STARTED: TaskStatus.RUNNING.value,
    RunnerMessageType.RUNTIME_PAUSED: TaskStatus.PAUSED.value,
    RunnerMessageType.RUNTIME_RESUMED: TaskStatus.RUNNING.value,
    RunnerMessageType.RUNTIME_STOPPED: TaskStatus.STOPPED.value,
    RunnerMessageType.RUNTIME_RETIRED: TaskStatus.STOPPED.value,
    RunnerMessageType.RUNTIME_FAILED: TaskStatus.FAILED.value,
}
_LIFECYCLE_JOB_TYPES = frozenset(
    {
        RunnerMessageType.TASK_START.value,
        RunnerMessageType.TASK_STOP.value,
        RunnerMessageType.TASK_PAUSE.value,
        RunnerMessageType.TASK_RESUME.value,
        RunnerMessageType.TASK_RETIRE.value,
    }
)
_LIFECYCLE_EVENTS_REQUIRING_RUNTIME_JOB = frozenset(
    {
        RunnerMessageType.RUNTIME_STARTED,
        RunnerMessageType.RUNTIME_PAUSED,
        RunnerMessageType.RUNTIME_RESUMED,
        RunnerMessageType.RUNTIME_STOPPED,
        RunnerMessageType.RUNTIME_RETIRED,
        RunnerMessageType.RUNTIME_FAILED,
    }
)


class RuntimeEventServiceError(RuntimeError):
    """Raised when a validated runner event fails backend runtime-event policy."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class RuntimeEventApplyResult:
    """Summary of backend side effects applied for one runtime event."""

    runtime_job_id: UUID | None
    runtime_job_status: str | None
    task_id: int | None
    task_status: str | None


def bind_runtime_event_publish_loop(loop: asyncio.AbstractEventLoop) -> Token[asyncio.AbstractEventLoop | None]:
    """Bind the main asyncio loop for runner-event side effects offloaded to threads."""
    return _RUNTIME_EVENT_PUBLISH_LOOP.set(loop)


def reset_runtime_event_publish_loop(token: Token[asyncio.AbstractEventLoop | None]) -> None:
    """Reset the runner-event publish loop binding after threaded processing."""
    _RUNTIME_EVENT_PUBLISH_LOOP.reset(token)


@dataclass(frozen=True, slots=True)
class ToolResultPromotionOutcome:
    """Promotion status for one accepted `tool.result` message."""

    execution_id: UUID | None
    promoted_artifact_ids: tuple[str, ...]
    promotion_error_code: str | None = None
    promotion_error_message: str | None = None


class RuntimeEventService:
    """Apply runtime-job/task side effects for one validated runner runtime event."""

    def __init__(
        self,
        db: Session,
        *,
        audit_service: RunnerControlAuditService | None = None,
        audit_emitter: RunnerControlAuditEmitter | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._db = db
        self._audit = audit_service or RunnerControlAuditService(emitter=audit_emitter)
        self._object_store = object_store or get_object_store()

    def apply_runtime_event(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        envelope: RunnerEnvelope,
    ) -> RuntimeEventApplyResult:
        """Validate event family and apply runtime-job/task transitions deterministically."""

        runtime_job = self._lookup_runtime_job(
            tenant_id=tenant_id,
            runtime_job_id=envelope.runtime_job_id,
        )
        self._validate_operation_family(
            tenant_id=tenant_id,
            envelope=envelope,
            runtime_job=runtime_job,
        )

        task_id = envelope.task_id if envelope.task_id is not None else (
            int(runtime_job.task_id) if runtime_job is not None and runtime_job.task_id is not None else None
        )
        task_id = self._resolve_existing_task_id(tenant_id=tenant_id, task_id=task_id)
        runtime_job_status = None
        tool_result_promotion: ToolResultPromotionOutcome | None = None
        if runtime_job is not None:
            runtime_job_status = self._transition_runtime_job_for_event(
                tenant_id=tenant_id,
                runtime_job=runtime_job,
                envelope=envelope,
            )
        tool_result_promotion = self._ingest_tool_result_execution(
            tenant_id=tenant_id,
            runtime_job=runtime_job,
            envelope=envelope,
            runtime_job_status=runtime_job_status,
        )

        task_status = self._transition_task_for_event(
            task_id=task_id,
            runtime_job=runtime_job,
            envelope=envelope,
        )
        self._publish_runtime_event(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            envelope=envelope,
            task_status=task_status,
            tool_result_promotion=tool_result_promotion,
        )

        self._audit.emit(
            event_type="runner.runtime_event.applied",
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            runtime_job_id=runtime_job.id if runtime_job is not None else None,
            correlation_id=envelope.correlation_id,
            metadata={
                "message_type": envelope.type,
                "runtime_job_status": runtime_job_status,
                "task_status": task_status,
                "promoted_artifact_ids": (
                    list(tool_result_promotion.promoted_artifact_ids) if tool_result_promotion is not None else []
                ),
                "artifact_promotion_error_code": (
                    tool_result_promotion.promotion_error_code if tool_result_promotion is not None else None
                ),
            },
        )
        return RuntimeEventApplyResult(
            runtime_job_id=runtime_job.id if runtime_job is not None else None,
            runtime_job_status=runtime_job_status,
            task_id=task_id,
            task_status=task_status,
        )

    def _resolve_existing_task_id(self, *, tenant_id: int, task_id: int | None) -> int | None:
        if task_id is None:
            return None
        exists = self._db.execute(
            select(Task.id).where(
                Task.tenant_id == tenant_id,
                Task.id == int(task_id),
            )
        ).scalar_one_or_none()
        return int(task_id) if exists is not None else None

    def _ingest_tool_result_execution(
        self,
        *,
        tenant_id: int,
        runtime_job: RuntimeJob | None,
        envelope: RunnerEnvelope,
        runtime_job_status: str | None,
    ) -> ToolResultPromotionOutcome | None:
        if envelope.message_type is not RunnerMessageType.TOOL_RESULT:
            return None
        if runtime_job is None or runtime_job.task_id is None:
            return None
        payload = envelope.payload
        if not isinstance(payload, RunnerToolResultPayload):
            return None
        if is_completed_process_tool_result_status(payload.status):
            return None
        try:
            execution = RunnerResultIngestService(self._db).ingest_tool_result(
                tenant_id=tenant_id,
                runtime_job=runtime_job,
                payload=payload,
                runtime_job_status=runtime_job_status,
            )
        except Exception as exc:
            message = f"Artifact/provenance promotion failed: {exc}"
            logger.exception(
                "runner_control.tool_result_promotion_failed tenant_id=%s runtime_job_id=%s command_id=%s",
                tenant_id,
                runtime_job.id,
                payload.command_id,
            )
            self._record_tool_result_promotion_failure(runtime_job=runtime_job, error_message=message)
            return ToolResultPromotionOutcome(
                execution_id=None,
                promoted_artifact_ids=(),
                promotion_error_code=_RUNNER_RESULT_PROMOTION_ERROR_CODE,
                promotion_error_message=message,
            )

        if _resolve_tool_result_artifact_state(payload=payload) == "pending":
            return ToolResultPromotionOutcome(
                execution_id=execution.id,
                promoted_artifact_ids=(),
            )

        artifact_ids = self._db.execute(
            select(ExecutionArtifact.id).where(ExecutionArtifact.execution_id == execution.id)
        ).scalars().all()
        self._trigger_knowledge_ingestion_after_promotion(
            runtime_job=runtime_job,
            payload=payload,
            execution_id=str(execution.id),
        )
        return ToolResultPromotionOutcome(
            execution_id=execution.id,
            promoted_artifact_ids=tuple(str(value) for value in artifact_ids),
        )

    def _trigger_knowledge_ingestion_after_promotion(
        self,
        *,
        runtime_job: RuntimeJob,
        payload: RunnerToolResultPayload,
        execution_id: str,
    ) -> None:
        task_id = int(runtime_job.task_id) if runtime_job.task_id is not None else None
        if task_id is None:
            return
        from backend.services.knowledge.ingestion_service import KnowledgeIngestionService
        from backend.services.knowledge.archive_service import KnowledgeArchiveService
        from backend.services.knowledge.evidence_storage_service import EvidenceStorageService

        compact_output = payload.result if isinstance(payload.result, Mapping) else None
        try:
            result = KnowledgeIngestionService(
                self._db,
                archive_service=KnowledgeArchiveService(
                    self._db,
                    evidence_storage_service=EvidenceStorageService(object_store=self._object_store),
                ),
            ).ingest_execution(
                task_id=task_id,
                source_execution_id=execution_id,
                tool_name_hint=str(payload.tool or ""),
                compact_output_hint=dict(compact_output) if compact_output is not None else None,
                raise_on_error=False,
            )
            if not bool(result.get("ok")):
                logger.warning(
                    "[KNOWLEDGE_INGESTION] Runner promotion ingestion did not succeed "
                    "(task_id=%s execution_id=%s status=%s error=%s).",
                    task_id,
                    execution_id,
                    result.get("status"),
                    result.get("error"),
                )
        except Exception:
            logger.exception(
                "[KNOWLEDGE_INGESTION] Runner promotion ingestion failed "
                "(task_id=%s execution_id=%s).",
                task_id,
                execution_id,
            )

    def _record_tool_result_promotion_failure(self, *, runtime_job: RuntimeJob, error_message: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        result_json = dict(runtime_job.result_json) if isinstance(runtime_job.result_json, Mapping) else {}
        result_json["artifact_promotion"] = {
            "status": "failed",
            "error_code": _RUNNER_RESULT_PROMOTION_ERROR_CODE,
            "error_message": str(
                mask_durable_secrets(
                    str(error_message).strip(),
                    source="runner_result_artifact_promotion_error",
                )
            ),
            "updated_at": now,
        }
        runtime_job.result_json = mask_durable_secrets(
            result_json,
            source="runtime_job_artifact_promotion",
        )
        self._db.flush()

    def _lookup_runtime_job(self, *, tenant_id: int, runtime_job_id: str | None) -> RuntimeJob | None:
        if runtime_job_id is None:
            return None
        normalized_runtime_job_id = str(runtime_job_id).strip()
        if not normalized_runtime_job_id:
            return None
        try:
            runtime_job_uuid = UUID(normalized_runtime_job_id)
        except ValueError as exc:
            raise RuntimeEventServiceError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Runtime job id is malformed.",
            ) from exc
        runtime_job = self._db.execute(
            select(RuntimeJob).where(
                RuntimeJob.id == runtime_job_uuid,
                RuntimeJob.tenant_id == tenant_id,
            )
        ).scalar_one_or_none()
        if runtime_job is None:
            raise RuntimeEventServiceError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message="Runtime job is not assigned to the authenticated tenant.",
            )
        return runtime_job

    def _validate_operation_family(
        self,
        *,
        tenant_id: int,
        envelope: RunnerEnvelope,
        runtime_job: RuntimeJob | None,
    ) -> None:
        if runtime_job is None and envelope.message_type in _LIFECYCLE_EVENTS_REQUIRING_RUNTIME_JOB:
            raise RuntimeEventServiceError(
                error_code="RUNTIME_JOB_NOT_ASSIGNED",
                message=f"Lifecycle runtime event `{envelope.type}` requires an assigned runtime job.",
            )
        if envelope.message_type is RunnerMessageType.TERMINAL_FRAME:
            payload = envelope.payload
            if not isinstance(payload, RunnerTerminalFramePayload):
                return
            runtime_scope_job_id = str(envelope.runtime_job_id or "").strip()
            task_id = (
                int(envelope.task_id)
                if envelope.task_id is not None
                else int(runtime_job.task_id) if runtime_job is not None and runtime_job.task_id is not None else None
            )
            if (
                task_id is None
                or not runtime_scope_job_id
                or not get_runner_terminal_frame_buffer().is_terminal_session_bound(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    runtime_job_id=runtime_scope_job_id,
                    session_id=payload.session_id,
                )
            ):
                raise RuntimeEventServiceError(
                    error_code="RUNNER_TERMINAL_SESSION_UNBOUND",
                    message="terminal.frame session_id is not bound to the task/runtime route.",
                )
        if runtime_job is None:
            return
        normalized_job_type = str(runtime_job.job_type or "").strip()
        if not normalized_job_type:
            raise RuntimeEventServiceError(
                error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                message="Runtime job operation type is missing.",
            )

        expected_job_type = _DIRECT_EVENT_JOB_TYPE_MAP.get(envelope.message_type)
        if expected_job_type is not None and normalized_job_type != expected_job_type:
            raise RuntimeEventServiceError(
                error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                message=(
                    f"Runner event `{envelope.type}` must resolve runtime job type "
                    f"`{expected_job_type}`, got `{normalized_job_type}`."
                ),
            )

        if envelope.message_type is RunnerMessageType.TERMINAL_RESULT:
            payload = envelope.payload
            if not isinstance(payload, RunnerTerminalResultPayload):
                return
            expected_terminal_type = f"terminal.{str(payload.terminal_operation or '').strip().lower()}"
            if expected_terminal_type not in {
                RunnerMessageType.TERMINAL_OPEN.value,
                RunnerMessageType.TERMINAL_INPUT.value,
                RunnerMessageType.TERMINAL_RESIZE.value,
                RunnerMessageType.TERMINAL_CLOSE.value,
            }:
                raise RuntimeEventServiceError(
                    error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                    message="terminal.result has unsupported terminal_operation value.",
                )
            if normalized_job_type != expected_terminal_type:
                raise RuntimeEventServiceError(
                    error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                    message=(
                        f"terminal.result operation `{expected_terminal_type}` does not match runtime job type "
                        f"`{normalized_job_type}`."
                    ),
                )
            expected_session_id = _expected_terminal_session_id_from_runtime_job(runtime_job=runtime_job)
            if expected_session_id is not None and payload.session_id != expected_session_id:
                raise RuntimeEventServiceError(
                    error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                    message=(
                        f"terminal.result session_id `{payload.session_id}` does not match runtime job request "
                        f"session_id `{expected_session_id}`."
                    ),
                )
            return

        if envelope.message_type is RunnerMessageType.RUNTIME_FAILED:
            if normalized_job_type not in _RUNTIME_FAILED_ALLOWED_JOB_TYPES:
                raise RuntimeEventServiceError(
                    error_code="RUNNER_EVENT_OPERATION_MISMATCH",
                    message=(
                        f"runtime.failed does not support runtime job type `{normalized_job_type}`."
                    ),
                )

    def _transition_runtime_job_for_event(
        self,
        *,
        tenant_id: int,
        runtime_job: RuntimeJob,
        envelope: RunnerEnvelope,
    ) -> str | None:
        next_status = _resolve_runtime_job_status(envelope=envelope)
        if next_status is None:
            if isinstance(envelope.payload, RunnerToolResultPayload):
                runtime_job.result_json = _build_runtime_job_result_json(envelope=envelope)
                self._db.flush()
            return None

        payload = envelope.payload
        result_json = _build_runtime_job_result_json(envelope=envelope)
        error_code = _payload_error_code(payload=payload)
        error_message = _payload_error_message(payload=payload)
        try:
            transitioned = RuntimeJobService(self._db).transition_runtime_job(
                tenant_id=tenant_id,
                runtime_job_id=runtime_job.id,
                next_status=next_status,
                result_json=result_json,
                error_code=error_code,
                error_message=error_message,
            )
        except RuntimeJobServiceError as exc:
            if exc.error_code in {"RUNTIME_JOB_TRANSITION_STALE", "RUNTIME_JOB_TRANSITION_INVALID"}:
                logger.info(
                    "runner_control.runtime_event_stale_transition runtime_job_id=%s message_type=%s error_code=%s",
                    runtime_job.id,
                    envelope.type,
                    exc.error_code,
                )
                return None
            raise RuntimeEventServiceError(
                error_code=exc.error_code,
                message=str(exc),
            ) from exc
        return str(transitioned.status or "").strip() or None

    def _transition_task_for_event(
        self,
        *,
        task_id: int | None,
        runtime_job: RuntimeJob | None,
        envelope: RunnerEnvelope,
    ) -> str | None:
        if task_id is None:
            return None
        target_status = _TASK_STATUS_BY_EVENT.get(envelope.message_type)
        if target_status is None:
            return None

        metadata: dict[str, Any] = {"runtime_event_type": envelope.type}
        reason = f"Runner runtime event `{envelope.type}`"
        lifecycle_outcome = _extract_lifecycle_outcome(payload=envelope.payload)
        if lifecycle_outcome is not None:
            metadata["runtime_event_lifecycle_outcome"] = lifecycle_outcome

        if envelope.message_type is RunnerMessageType.RUNTIME_FAILED:
            runtime_job_type = str(runtime_job.job_type or "").strip() if runtime_job is not None else ""
            if runtime_job_type and runtime_job_type not in _LIFECYCLE_JOB_TYPES:
                return None
        elif envelope.message_type is RunnerMessageType.RUNTIME_STOPPED and lifecycle_outcome == "cancelled":
            reason = "Runner lifecycle cancellation completed"
        elif envelope.message_type is RunnerMessageType.RUNTIME_RETIRED:
            reason = "Runner lifecycle retirement completed"
            self._schedule_retirement_cleanup(task_id=task_id)

        success, _, _ = TaskStateService(self._db).change_task_status(
            task_id=task_id,
            new_status=target_status,
            user_id=None,
            reason=reason,
            change_source="system",
            metadata=metadata,
        )
        if not success:
            return None
        return target_status

    @staticmethod
    def _schedule_retirement_cleanup(*, task_id: int) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(TaskRetirementService.cleanup_runtime_stream_state(task_id=task_id))
            return
        loop.create_task(TaskRetirementService.cleanup_runtime_stream_state(task_id=task_id))

    def _publish_runtime_event(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int | None,
        envelope: RunnerEnvelope,
        task_status: str | None,
        tool_result_promotion: ToolResultPromotionOutcome | None,
    ) -> None:
        if task_id is None:
            return
        event_metadata: dict[str, Any] = {
            "tenant_id": tenant_id,
            "runner_id": str(runner_id),
            "message_id": envelope.message_id,
            "message_type": envelope.type,
            "runtime_job_id": envelope.runtime_job_id,
            "task_id": task_id,
            "correlation_id": envelope.correlation_id,
            "task_status": task_status,
        }
        payload = envelope.payload
        if isinstance(payload, RunnerTerminalFramePayload):
            safe_frame_data = _bound_terminal_frame_data(payload.data)
            buffered = get_runner_terminal_frame_buffer().append_frame(
                tenant_id=tenant_id,
                task_id=task_id,
                runtime_job_id=str(envelope.runtime_job_id or ""),
                session_id=payload.session_id,
                sequence=payload.sequence,
                stream=payload.stream,
                data=safe_frame_data,
            )
            event_metadata["terminal"] = {
                "session_id": payload.session_id,
                "sequence": payload.sequence,
                "stream": payload.stream,
                "data": safe_frame_data,
                "buffered": buffered,
            }
        elif isinstance(payload, _RESULT_PAYLOAD_TYPES):
            event_metadata["operation_id"] = str(payload.operation_id)
            event_metadata["status"] = str(payload.status)
            event_metadata["error_code"] = _payload_error_code(payload=payload)
            event_metadata["error_message"] = _payload_error_message(payload=payload)
            if isinstance(payload, RunnerTerminalResultPayload):
                binding_runtime_job_id = _resolve_terminal_binding_runtime_job_id(
                    envelope=envelope,
                    payload=payload,
                )
                if (
                    payload.terminal_operation == "open"
                    and payload.status == "succeeded"
                    and binding_runtime_job_id is not None
                ):
                    get_runner_terminal_frame_buffer().bind_terminal_session(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        runtime_job_id=binding_runtime_job_id,
                        session_id=payload.session_id,
                    )
            if (
                isinstance(payload, RunnerTerminalResultPayload)
                and payload.terminal_operation == "close"
                and payload.status == "succeeded"
            ):
                get_runner_terminal_frame_buffer().clear_terminal_session(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    session_id=payload.session_id,
                )
                binding_runtime_job_id = _resolve_terminal_binding_runtime_job_id(
                    envelope=envelope,
                    payload=payload,
                )
                get_runner_terminal_frame_buffer().unbind_terminal_session(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    runtime_job_id=binding_runtime_job_id,
                    session_id=payload.session_id,
                )
            if isinstance(payload, RunnerToolResultPayload) and tool_result_promotion is not None:
                if tool_result_promotion.execution_id is not None:
                    event_metadata["execution_id"] = str(tool_result_promotion.execution_id)
                if tool_result_promotion.promoted_artifact_ids:
                    event_metadata["promoted_artifact_ids"] = list(tool_result_promotion.promoted_artifact_ids)
                if tool_result_promotion.promotion_error_code is not None:
                    event_metadata["artifact_promotion"] = {
                        "status": "failed",
                        "error_code": tool_result_promotion.promotion_error_code,
                        "error_message": tool_result_promotion.promotion_error_message,
                    }

        try:
            loop = asyncio.get_running_loop()
            schedule_task = loop.create_task
        except RuntimeError:
            loop = _RUNTIME_EVENT_PUBLISH_LOOP.get()
            if loop is None:
                return

            def schedule_task(coro):
                loop.call_soon_threadsafe(asyncio.create_task, coro)

        try:
            from backend.services.websocket.connection_manager import websocket_manager
        except Exception:
            return

        if task_status:
            schedule_task(
                websocket_manager.broadcast_to_task(
                    task_id,
                    {
                        "type": "status_update",
                        "status": task_status,
                        "source": "runner_event",
                    },
                )
            )


def _resolve_runtime_job_status(*, envelope: RunnerEnvelope) -> str | None:
    payload = envelope.payload
    if isinstance(payload, RunnerTerminalFramePayload):
        return None
    if isinstance(payload, RunnerToolResultPayload):
        artifact_state = _resolve_tool_result_artifact_state(payload=payload)
        if artifact_state == "pending":
            return None
        if artifact_state == "failed":
            return "failed"
        payload_status = str(payload.status or "").strip().lower()
        if is_completed_process_tool_result_status(payload_status):
            return None
        if payload_status == "succeeded" and payload.success:
            return "succeeded"
        return "failed"

    if envelope.message_type is RunnerMessageType.RUNTIME_FAILED:
        return "failed"

    payload_status = _payload_status(payload=payload)
    if payload_status in {"failed", "error", "rejected"}:
        return "failed"
    if payload_status in {"cancelled", "canceled"}:
        return "cancelled"

    if envelope.message_type is RunnerMessageType.RUNTIME_STOPPED:
        lifecycle_outcome = _extract_lifecycle_outcome(payload=payload)
        if lifecycle_outcome == "cancelled":
            return "cancelled"
        return "succeeded"

    if envelope.message_type is RunnerMessageType.TERMINAL_FRAME:
        return None

    return "succeeded"


def _build_runtime_job_result_json(*, envelope: RunnerEnvelope) -> Mapping[str, Any]:
    payload = envelope.payload
    result_payload: dict[str, Any] = {
        "source": "runner_event",
        "message_type": envelope.type,
    }
    if isinstance(payload, RunnerToolResultPayload):
        tool_result_payload = sanitize_tool_result_payload_for_persistence(
            {
                "operation_id": payload.operation_id,
                "command_id": payload.command_id,
                "tool": payload.tool,
                "status": payload.status,
                "success": payload.success,
                "exit_code": payload.exit_code,
                "stdout": payload.stdout,
                "stderr": payload.stderr,
                "artifacts": list(payload.artifacts),
                "error_code": payload.error_code,
                "error_message": payload.error_message,
                "result": dict(payload.result),
                "metadata": dict(payload.metadata),
            }
        )
        result_payload.update(tool_result_payload)
        return result_payload
    if isinstance(payload, _RESULT_PAYLOAD_TYPES):
        result_payload["operation_id"] = payload.operation_id
        result_payload["status"] = payload.status
        if isinstance(payload.result, Mapping):
            result_payload["result"] = dict(payload.result)
    lifecycle_outcome = _extract_lifecycle_outcome(payload=payload)
    if lifecycle_outcome is not None:
        result_payload["lifecycle_outcome"] = lifecycle_outcome
    if isinstance(payload, RunnerTerminalResultPayload):
        result_payload["terminal_operation"] = payload.terminal_operation
        result_payload["session_id"] = payload.session_id
        result_payload["sequence"] = payload.sequence
    return mask_durable_secrets(result_payload, source="runtime_job_runner_event_result")


def _expected_terminal_session_id_from_runtime_job(*, runtime_job: RuntimeJob) -> str | None:
    payload_json = runtime_job.payload_json
    if not isinstance(payload_json, Mapping):
        return None
    params = payload_json.get("params")
    if not isinstance(params, Mapping):
        return None
    session_id = str(params.get("session_id") or "").strip()
    return session_id or None


def _resolve_terminal_binding_runtime_job_id(
    *,
    envelope: RunnerEnvelope,
    payload: RunnerTerminalResultPayload,
) -> str | None:
    runtime_scope_job_id = str(envelope.runtime_job_id or "").strip()
    result = payload.result
    if isinstance(result, Mapping):
        runtime_job_id_from_result = str(result.get("runtime_job_id") or "").strip()
        if runtime_job_id_from_result:
            return runtime_job_id_from_result
    return runtime_scope_job_id or None


def _extract_lifecycle_outcome(*, payload: object) -> str | None:
    if not isinstance(payload, _RESULT_PAYLOAD_TYPES):
        return None
    result = payload.result
    if not isinstance(result, Mapping):
        return None
    raw = str(result.get("lifecycle_outcome") or "").strip().lower()
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw:
        return raw
    return None


def _payload_status(*, payload: object) -> str:
    if isinstance(payload, _RESULT_PAYLOAD_TYPES):
        return str(payload.status or "").strip().lower()
    return ""


def _payload_error_code(*, payload: object) -> str | None:
    if isinstance(payload, RunnerToolResultPayload):
        artifact_state = _resolve_tool_result_artifact_state(payload=payload)
        if artifact_state == "failed":
            metadata = payload.metadata if isinstance(payload.metadata, Mapping) else {}
            upload = metadata.get("artifact_upload")
            if isinstance(upload, Mapping) and str(upload.get("status") or "").strip().lower() in {
                "failed",
                "partial",
            }:
                return _RUNNER_ARTIFACT_UPLOAD_FAILED
            return _RUNNER_ARTIFACT_PROMOTION_REQUIRED
    if isinstance(payload, _RESULT_PAYLOAD_TYPES):
        normalized = str(payload.error_code or "").strip()
        return normalized or None
    return None


def _payload_error_message(*, payload: object) -> str | None:
    if isinstance(payload, RunnerToolResultPayload):
        artifact_state = _resolve_tool_result_artifact_state(payload=payload)
        if artifact_state == "failed":
            metadata = payload.metadata if isinstance(payload.metadata, Mapping) else {}
            upload = metadata.get("artifact_upload")
            if isinstance(upload, Mapping):
                upload_status = str(upload.get("status") or "").strip().lower()
                if upload_status in {"failed", "partial"}:
                    return "Runner artifact upload did not promote every declared artifact."
            manifest = metadata.get("artifact_manifest")
            if isinstance(manifest, Mapping):
                manifest_status = str(manifest.get("status") or "").strip()
                return f"Runner artifact manifest did not produce uploadable artifacts: {manifest_status}."
            return "Runner artifact promotion failed before tool result finalization."
    if isinstance(payload, _RESULT_PAYLOAD_TYPES):
        normalized = str(payload.error_message or "").strip()
        return normalized or None
    return None


def _resolve_tool_result_artifact_state(*, payload: RunnerToolResultPayload) -> str:
    """Return runner artifact promotion state for a tool result payload."""
    metadata = payload.metadata if isinstance(payload.metadata, Mapping) else {}

    upload = metadata.get("artifact_upload")
    if isinstance(upload, Mapping):
        upload_status = str(upload.get("status") or "").strip().lower()
        if upload_status == "promoted":
            return "ready"
        if upload_status in {"failed", "partial"}:
            return "failed"
        if upload_status in {"pending", "upload_pending"}:
            return "pending"

    promotion = metadata.get("artifact_promotion")
    if isinstance(promotion, Mapping):
        promotion_status = str(promotion.get("status") or "").strip().lower()
    else:
        promotion_status = str(metadata.get("artifact_promotion_status") or "").strip().lower()
    if promotion_status == "ready":
        return "ready"
    if promotion_status in {"upload_failed", "failed"}:
        return "failed"
    if promotion_status in {"upload_pending", "pending"}:
        return "pending"

    manifest = metadata.get("artifact_manifest")
    if isinstance(manifest, Mapping):
        manifest_status = str(manifest.get("status") or "").strip().lower()
        declared_count = _coerce_non_negative_int(manifest.get("declared_count"))
        accepted_count = _coerce_non_negative_int(manifest.get("accepted_count"))
        if manifest_status == "ready_for_upload_request":
            return "pending"
        if manifest_status in {"skipped_invalid_workspace", "no_uploadable_artifacts"}:
            return "failed" if declared_count > 0 else "none"
        if manifest_status == "none":
            return "none"
        if manifest_status and declared_count > 0 and accepted_count == 0:
            return "failed"

    return "none"


def _coerce_non_negative_int(value: object) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, coerced)


def _bound_terminal_frame_data(data: str) -> str:
    encoded = str(data or "").encode("utf-8", errors="replace")
    if len(encoded) <= RUNNER_TERMINAL_FRAME_MAX_BYTES:
        return str(data or "")
    return encoded[:RUNNER_TERMINAL_FRAME_MAX_BYTES].decode("utf-8", errors="replace")
