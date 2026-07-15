"""Runner-channel read models for protocol binding validation.

Purpose: query tenant-bound runtime-job bindings and task assignments needed by
runner websocket-channel protocol validation. Scope boundary: this module owns
read-only binding lookups only and must not perform runtime-job commands,
channel routing, audit, metrics, ledger writes, or websocket side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.runner_control import RuntimeJob
from backend.services.runner_control.protocol import RunnerRuntimeJobBinding


def _lookup_runtime_job_binding(
    db: Session,
    runtime_job_id: str,
) -> RunnerRuntimeJobBinding | None:
    normalized_runtime_job_id = str(runtime_job_id or "").strip()
    if not normalized_runtime_job_id:
        return None
    try:
        runtime_job_uuid = UUID(normalized_runtime_job_id)
    except ValueError:
        return None

    runtime_job = db.execute(
        select(RuntimeJob).where(RuntimeJob.id == runtime_job_uuid)
    ).scalar_one_or_none()
    if runtime_job is None or runtime_job.runner_id is None:
        return None
    payload_json = runtime_job.payload_json
    payload_map = payload_json if isinstance(payload_json, Mapping) else {}
    metadata = payload_map.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    params = payload_map.get("params")
    params_map = params if isinstance(params, Mapping) else {}
    return RunnerRuntimeJobBinding(
        runtime_job_id=str(runtime_job.id),
        tenant_id=str(runtime_job.tenant_id),
        runner_id=str(runtime_job.runner_id),
        task_id=runtime_job.task_id,
        job_type=_normalize_optional_text(runtime_job.job_type),
        workspace_id=_first_non_empty_text(
            payload_map.get("workspace_id"),
            metadata_map.get("workspace_id"),
            params_map.get("workspace_id"),
        ),
        command_id=_first_non_empty_text(
            payload_map.get("command_id"),
            metadata_map.get("command_id"),
            params_map.get("command_id"),
        ),
        task_runtime_job_id=_first_non_empty_text(
            payload_map.get("task_runtime_job_id"),
            metadata_map.get("task_runtime_job_id"),
            params_map.get("task_runtime_job_id"),
        ),
    )


def _is_task_assigned_to_runner(
    db: Session,
    tenant_id: str,
    runner_id: str,
    task_id: int,
) -> bool:
    normalized_runner_id = str(runner_id).strip().lower()
    if not normalized_runner_id:
        return False
    try:
        normalized_tenant_id = int(str(tenant_id).strip())
    except ValueError:
        return False
    assigned_task_id = db.execute(
        select(Task.id).where(
            Task.tenant_id == normalized_tenant_id,
            Task.id == int(task_id),
            func.lower(Task.runner_id) == normalized_runner_id,
        )
    ).scalar_one_or_none()
    return assigned_task_id is not None


def _first_non_empty_text(*values: object) -> str | None:
    for value in values:
        normalized = _normalize_optional_text(value)
        if normalized is not None:
            return normalized
    return None


def _normalize_optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
