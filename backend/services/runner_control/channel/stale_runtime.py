"""Runner websocket-channel stale-runtime reconciliation helpers.

Purpose: detect stale runner-reported runtime jobs from heartbeat capacity and
enqueue retire commands for those stale runtimes. Scope boundary: this module
owns heartbeat capacity stale-runtime reconciliation only; it does not own
heartbeat persistence, presence updates, outbound dispatch, ACK handling, or
broader runner lifecycle behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task
from backend.services.runner_control.channel.types import RunnerChannelSession
from backend.services.runner_control.coordination import RunnerCoordinationStore
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobCreateRequest,
    RuntimeJobService,
    RuntimeJobServiceError,
)
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine
from runtime_shared.runner_protocol import RunnerMessageType

logger = logging.getLogger("backend.services.runner_control.channel_manager")

_RUNNER_ACTIVE_RUNTIME_STATUSES = frozenset({"starting", "running", "paused", "stopping"})
_TASK_STALE_RUNTIME_STATUSES = frozenset(TaskStatus.get_terminal_statuses())


def _reconcile_stale_runner_runtime_jobs(
    *,
    db: Session,
    coordination_store: RunnerCoordinationStore,
    session: RunnerChannelSession,
    capacity: Mapping[str, object],
) -> None:
    summaries = capacity.get("active_runtime_jobs")
    if not isinstance(summaries, list):
        return
    for summary in summaries:
        if not isinstance(summary, Mapping):
            continue
        stale_reason = _stale_runner_runtime_reason(
            db=db,
            tenant_id=session.tenant_id,
            summary=summary,
        )
        if stale_reason is None:
            continue
        _enqueue_stale_runner_runtime_retire(
            db=db,
            coordination_store=coordination_store,
            session=session,
            summary=summary,
            capacity=capacity,
            stale_reason=stale_reason,
        )


def _stale_runner_runtime_reason(
    *,
    db: Session,
    tenant_id: int,
    summary: Mapping[str, object],
) -> str | None:
    status = str(summary.get("status") or "").strip().lower()
    if status not in _RUNNER_ACTIVE_RUNTIME_STATUSES:
        return None
    task_id = _coerce_int(summary.get("task_id"))
    if task_id is None:
        return "runner_task_id_invalid"
    task = db.execute(
        select(Task).where(
            Task.tenant_id == tenant_id,
            Task.id == task_id,
        )
    ).scalar_one_or_none()
    if task is None:
        return "backend_task_missing"
    task_status = str(getattr(task, "status", "") or "").strip().lower()
    if task_status in _TASK_STALE_RUNTIME_STATUSES:
        return f"backend_task_{task_status}"
    return None


def _enqueue_stale_runner_runtime_retire(
    *,
    db: Session,
    coordination_store: RunnerCoordinationStore,
    session: RunnerChannelSession,
    summary: Mapping[str, object],
    capacity: Mapping[str, object],
    stale_reason: str,
) -> None:
    runner_runtime_job_id = str(summary.get("runtime_job_id") or "").strip()
    task_id = _coerce_int(summary.get("task_id"))
    workspace_id = str(summary.get("workspace_id") or "").strip()
    if not runner_runtime_job_id or task_id is None:
        return
    if not workspace_id:
        workspace_id = f"task-{task_id}"

    idempotency_key = f"stale-runtime-retire:{session.runner_id}:{runner_runtime_job_id}"
    runtime_job_service = RuntimeJobService(db)
    try:
        runtime_job = runtime_job_service.create_runtime_job(
            RuntimeJobCreateRequest(
                tenant_id=session.tenant_id,
                task_id=None,
                job_type=RunnerMessageType.TASK_RETIRE.value,
                idempotency_key=idempotency_key,
                payload_json={
                    "operation_name": "retire_task_runtime",
                    "message_type": RunnerMessageType.TASK_RETIRE.value,
                    "workspace_id": workspace_id,
                    "operation_id": f"stale-runtime-retire-{uuid_lib.uuid4().hex}",
                    "params": {
                        "runtime_job_id": runner_runtime_job_id,
                        "tenant_id": str(session.tenant_id),
                        "task_id": str(task_id),
                        "workspace_id": workspace_id,
                        "stale_reason": stale_reason,
                    },
                },
                correlation_id=idempotency_key,
            )
        )
        assigned = runtime_job_service.assign_runtime_job(
            tenant_id=session.tenant_id,
            runtime_job_id=runtime_job.id,
            runner_id=session.runner_id,
        )
    except RuntimeJobServiceError as exc:
        if exc.error_code == "RUNTIME_JOB_IDEMPOTENCY_CONFLICT":
            return
        logger.warning(
            "runner_control.stale_runtime_retire_job_failed tenant_id=%s runner_id=%s task_id=%s runtime_job_id=%s error_code=%s",
            session.tenant_id,
            session.runner_id,
            task_id,
            runner_runtime_job_id,
            exc.error_code,
        )
        return

    outbound_payload = {
        "runtime_job_id": str(assigned.id),
        "operation_id": f"stale-runtime-retire-{assigned.id}",
        "workspace_id": workspace_id,
        "runtime_image": _runner_runtime_image_from_capacity(capacity),
        "operation": RunnerMessageType.TASK_RETIRE.value,
        "task_id": task_id,
        "params": {
            "runtime_job_id": runner_runtime_job_id,
            "tenant_id": str(session.tenant_id),
            "task_id": str(task_id),
            "workspace_id": workspace_id,
            "stale_reason": stale_reason,
        },
        "delivery_policy": {"max_attempts": 3, "timeout_seconds": 10.0, "offline": "queue"},
    }
    coordination_store.enqueue_outbound_message(
        tenant_id=session.tenant_id,
        runner_id=session.runner_id,
        message_id=f"stale-runtime-retire-{uuid_lib.uuid4().hex}",
        message_type=RunnerMessageType.TASK_RETIRE.value,
        payload_json=outbound_payload,
        idempotency_key=f"remote_runtime:{RunnerMessageType.TASK_RETIRE.value}:{assigned.id}",
        runtime_job_id=assigned.id,
        task_id=None,
        correlation_id=idempotency_key,
    )
    db.flush()


def _runner_runtime_image_from_capacity(capacity: Mapping[str, object]) -> str:
    runtime_image = str(capacity.get("runtime_image") or "").strip()
    return runtime_image or default_runtime_image_for_machine()


def _coerce_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
