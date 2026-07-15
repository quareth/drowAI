"""Provider-specific runtime-job queries for cloud runner collaborators.

This module owns read-only SQLAlchemy lookups used by the cloud runner provider.
It does not create, assign, transition, dispatch, or project runtime jobs.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import RunnerControlMessage, RuntimeJob

from ..constants import (
    _ACTIVE_START_RUNTIME_JOB_STATUSES,
    _TASK_START_JOB_TYPE,
    _TOOL_COMMAND_JOB_TYPE,
)
from ..normalization import _resolve_optional_text


class CloudRunnerRuntimeJobQueries:
    """Read cloud-runner runtime-job and outbound-message state."""

    @staticmethod
    def _find_existing_start_runtime_job_id(
        *,
        db: Session,
        tenant_id: int,
        task_id: int,
        runner_id: UUID,
    ) -> str | None:
        runtime_job_id = None
        if hasattr(db, "execute") and callable(db.execute):
            runtime_job_id = db.execute(
                select(RuntimeJob.id).where(
                    RuntimeJob.tenant_id == tenant_id,
                    RuntimeJob.task_id == task_id,
                    RuntimeJob.runner_id == runner_id,
                    RuntimeJob.job_type == _TASK_START_JOB_TYPE,
                    RuntimeJob.status.in_(_ACTIVE_START_RUNTIME_JOB_STATUSES),
                ).order_by(RuntimeJob.created_at.desc())
            ).scalar_one_or_none()

        if runtime_job_id is None:
            return None
        return str(runtime_job_id)

    @staticmethod
    def _find_active_task_start_runtime_job(
        *,
        db: Session,
        tenant_id: int,
        task_id: int,
        runner_id: UUID,
        workspace_id: str,
    ) -> RuntimeJob | None:
        candidates = db.execute(
            select(RuntimeJob).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.task_id == task_id,
                RuntimeJob.runner_id == runner_id,
                RuntimeJob.job_type == _TASK_START_JOB_TYPE,
                RuntimeJob.status.in_(_ACTIVE_START_RUNTIME_JOB_STATUSES),
            ).order_by(RuntimeJob.created_at.desc())
        ).scalars().all()
        for candidate in candidates:
            payload = candidate.payload_json if isinstance(candidate.payload_json, Mapping) else {}
            if str(payload.get("workspace_id") or "").strip() == workspace_id:
                return candidate
        return None

    @staticmethod
    def _find_active_task_start_runtime_job_for_task(
        *,
        db: Session,
        tenant_id: int,
        task_id: int,
        workspace_id: str,
    ) -> RuntimeJob | None:
        candidates = db.execute(
            select(RuntimeJob).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.task_id == task_id,
                RuntimeJob.runner_id.is_not(None),
                RuntimeJob.job_type == _TASK_START_JOB_TYPE,
                RuntimeJob.status.in_(_ACTIVE_START_RUNTIME_JOB_STATUSES),
            ).order_by(RuntimeJob.created_at.desc())
        ).scalars().all()
        for candidate in candidates:
            payload = candidate.payload_json if isinstance(candidate.payload_json, Mapping) else {}
            if str(payload.get("workspace_id") or "").strip() == workspace_id:
                return candidate
        return None

    @staticmethod
    def _find_existing_outbound_tool_command(
        *,
        db: Session,
        tenant_id: int,
        runner_id: UUID,
        runtime_job_id: UUID,
    ) -> RunnerControlMessage | None:
        return db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant_id,
                RunnerControlMessage.runner_id == runner_id,
                RunnerControlMessage.direction == "outbound",
                RunnerControlMessage.runtime_job_id == runtime_job_id,
                RunnerControlMessage.type == _TOOL_COMMAND_JOB_TYPE,
            ).order_by(RunnerControlMessage.created_at.desc())
        ).scalar_one_or_none()

    @staticmethod
    def _find_existing_tool_command_runtime_job_by_command_id(
        *,
        db: Session,
        tenant_id: int,
        command_id: str,
    ) -> RuntimeJob | None:
        candidates = db.execute(
            select(RuntimeJob).where(
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.job_type == _TOOL_COMMAND_JOB_TYPE,
            ).order_by(RuntimeJob.created_at.desc())
        ).scalars().all()
        for candidate in candidates:
            payload = candidate.payload_json if isinstance(candidate.payload_json, Mapping) else {}
            if _resolve_optional_text(payload.get("command_id")) == command_id:
                return candidate
        return None
