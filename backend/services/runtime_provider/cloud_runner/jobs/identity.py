"""Runtime-job identity and status helpers for cloud runner collaborators.

This module resolves provider runtime-job identities and validates runtime-job
bindings. It does not dispatch operations, orchestrate tool commands, or import
the provider facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.runner_control import RuntimeJob
from backend.services.runtime_provider.contracts import RuntimeOperationRequest
from runtime_shared.runner_protocol import RunnerMessageType

from ..normalization import _resolve_optional_text
from .queries import CloudRunnerRuntimeJobQueries


def _resolve_explicit_runtime_job_id(request: RuntimeOperationRequest) -> str | None:
    payload_runtime_job_id = _resolve_optional_text(request.payload.get("runtime_job_id"))
    if payload_runtime_job_id is not None:
        return payload_runtime_job_id
    return _resolve_optional_text(request.metadata.get("runtime_job_id"))


class RuntimeJobIdentityResolver:
    """Resolve outbound runtime-job identity values for runner messages."""

    def __init__(self, *, runtime_job_queries: CloudRunnerRuntimeJobQueries) -> None:
        self._runtime_job_queries = runtime_job_queries

    def _resolve_outbound_runtime_job_id(
        self,
        *,
        db: Session,
        request: RuntimeOperationRequest,
        message_type: RunnerMessageType,
        tenant_id: int,
        runner_id: UUID,
        control_runtime_job_id: UUID,
    ) -> str:
        explicit_runtime_job_id = _resolve_explicit_runtime_job_id(request)
        if explicit_runtime_job_id is not None:
            return explicit_runtime_job_id
        if message_type is RunnerMessageType.TASK_START:
            return str(control_runtime_job_id)

        existing_start_runtime_job_id = self._runtime_job_queries._find_existing_start_runtime_job_id(
            db=db,
            tenant_id=tenant_id,
            task_id=request.task_id,
            runner_id=runner_id,
        )
        if existing_start_runtime_job_id is not None:
            return existing_start_runtime_job_id
        return str(control_runtime_job_id)


def _runtime_job_binding_conflicts(
    *,
    runtime_job: RuntimeJob,
    command_id: str,
    task_runtime_job_id: str,
    workspace_id: str,
    runner_id: UUID,
    task_id: int,
) -> bool:
    if runtime_job.task_id != task_id:
        return True
    if runtime_job.runner_id != runner_id:
        return True
    payload = runtime_job.payload_json if isinstance(runtime_job.payload_json, Mapping) else {}
    payload_command_id = _resolve_optional_text(payload.get("command_id"))
    payload_task_runtime_job_id = _resolve_optional_text(payload.get("task_runtime_job_id"))
    payload_workspace_id = _resolve_optional_text(payload.get("workspace_id"))
    return (
        payload_command_id != command_id
        or payload_task_runtime_job_id != task_runtime_job_id
        or payload_workspace_id != workspace_id
    )


def _is_terminal_runtime_job_status(status: object) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {"succeeded", "failed", "cancelled", "lost", "expired"}
