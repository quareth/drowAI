"""Tool-command result finalization for cloud runner dispatch.

This module owns finalize_tool_command_result request validation, runtime-job
binding checks, provenance ingestion, terminalization, and final delegate
projection. It does not dispatch tool commands, wait for results, move
ingestion responsibility into the ingestion service, or import the provider
facade.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import RuntimeJob
from backend.services.artifact.runner_result_ingest_service import RunnerResultIngestService
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobService,
    RuntimeJobServiceError,
)
from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)
from runtime_shared.runner_protocol import (
    RunnerToolResultPayload,
    sanitize_tool_result_payload_for_persistence,
)

from ..error_codes import (
    RUNNER_TOOL_COMMAND_ID_MISMATCH,
    RUNNER_WORKSPACE_MISMATCH,
    RUNTIME_JOB_BINDING_INVALID,
    RUNTIME_JOB_NOT_FOUND,
    RUNTIME_JOB_TRANSITION_STALE,
    _RUNNER_DISPATCH_FAILED,
)
from ..normalization import (
    _normalize_optional_uuid,
    _normalize_tenant_id,
    _resolve_optional_text,
)
from ..result_builders import CloudRunnerResultBuilder
from .projection import ToolCommandResultProjector

SessionFactory = Callable[[], Session]


class ToolCommandResultFinalizer:
    """Finalizes runner-produced tool.command results into canonical records."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        result_builder: CloudRunnerResultBuilder,
        provider_name: str,
        projector: ToolCommandResultProjector,
    ) -> None:
        self._session_factory = session_factory
        self._result_builder = result_builder
        self._provider_name = provider_name
        self._projector = projector

    async def finalize_tool_command_result(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Persist the canonical tool verdict for a completed cloud tool.command job."""
        operation_name = "finalize_tool_command_result"
        tool_command_runtime_job_id = _resolve_optional_text(
            request.payload.get("tool_command_runtime_job_id")
            or request.metadata.get("tool_command_runtime_job_id")
        )
        command_id = _resolve_optional_text(request.payload.get("command_id"))
        workspace_id = _resolve_optional_text(
            request.payload.get("workspace_id") or request.workspace_id
        )
        tool = _resolve_optional_text(
            request.payload.get("tool") or request.payload.get("tool_id")
        )
        canonical_status = _resolve_optional_text(request.payload.get("canonical_status"))
        if tool_command_runtime_job_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`tool_command_runtime_job_id` is required for finalize_tool_command_result.",
            )
        if command_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`command_id` is required for finalize_tool_command_result.",
            )
        if workspace_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`workspace_id` is required for finalize_tool_command_result.",
            )
        if canonical_status not in {"succeeded", "failed"}:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`canonical_status` must be `succeeded` or `failed`.",
            )

        try:
            tenant_id = _normalize_tenant_id(request.tenant_id)
            runner_id = _normalize_optional_uuid(request.runner_id)
            runtime_job_uuid = UUID(str(tool_command_runtime_job_id).strip())
        except ValueError as exc:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message=str(exc),
            )
        if runner_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`runner_id` is required for finalize_tool_command_result.",
            )

        canonical_success = bool(request.payload.get("canonical_success"))
        try:
            canonical_exit_code = int(
                request.payload.get("canonical_exit_code")
                if request.payload.get("canonical_exit_code") is not None
                else (0 if canonical_success else 1)
            )
        except (TypeError, ValueError):
            canonical_exit_code = 0 if canonical_success else 1

        artifacts_raw = request.payload.get("artifacts")
        artifacts: tuple[str, ...] = ()
        if isinstance(artifacts_raw, (list, tuple)):
            artifacts = tuple(str(item).strip() for item in artifacts_raw if str(item).strip())

        metadata: dict[str, Any] = {
            "workspace_id": workspace_id,
            "command_id": command_id,
            "artifact_scope": "cloud_data_plane",
        }
        task_runtime_job_id = _resolve_optional_text(
            request.payload.get("task_runtime_job_id")
        )
        if task_runtime_job_id is not None:
            metadata["task_runtime_job_id"] = task_runtime_job_id
        tool_call_id = _resolve_optional_text(request.payload.get("tool_call_id"))
        if tool_call_id is not None:
            metadata["tool_call_id"] = tool_call_id
        tool_batch_id = _resolve_optional_text(request.payload.get("tool_batch_id"))
        if tool_batch_id is not None:
            metadata["tool_batch_id"] = tool_batch_id
        if request.payload.get("process_success") is not None:
            metadata["process_success"] = request.payload.get("process_success")
        if request.payload.get("process_exit_code") is not None:
            metadata["process_exit_code"] = request.payload.get("process_exit_code")
        enriched_metadata = request.payload.get("metadata")
        if isinstance(enriched_metadata, Mapping):
            for key, value in enriched_metadata.items():
                if key not in metadata:
                    metadata[key] = value

        stdout = str(request.payload.get("stdout") or "")
        stderr = str(request.payload.get("stderr") or "")
        terminal_status = canonical_status

        try:
            with self._session_factory() as db:
                runtime_job = db.execute(
                    select(RuntimeJob).where(
                        RuntimeJob.id == runtime_job_uuid,
                        RuntimeJob.tenant_id == tenant_id,
                        RuntimeJob.task_id == request.task_id,
                        RuntimeJob.runner_id == runner_id,
                    )
                ).scalar_one_or_none()
                if runtime_job is None:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=RUNTIME_JOB_NOT_FOUND,
                        error_message="tool.command runtime job binding not found for tenant/runner/task.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                if str(runtime_job.job_type or "").strip().lower() != "tool.command":
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=RUNTIME_JOB_BINDING_INVALID,
                        error_message="runtime_job_id must reference a tool.command runtime job.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                command_payload = (
                    runtime_job.payload_json
                    if isinstance(runtime_job.payload_json, Mapping)
                    else {}
                )
                bound_command_id = _resolve_optional_text(command_payload.get("command_id"))
                bound_workspace_id = _resolve_optional_text(command_payload.get("workspace_id"))
                if bound_command_id != command_id:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=RUNNER_TOOL_COMMAND_ID_MISMATCH,
                        error_message="command_id does not match bound tool.command runtime job.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )
                if bound_workspace_id != workspace_id:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=RUNNER_WORKSPACE_MISMATCH,
                        error_message="workspace_id does not match bound tool.command runtime job.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                resolved_tool = tool or _resolve_optional_text(command_payload.get("tool")) or "runner.tool_result"
                tool_result_payload = RunnerToolResultPayload(
                    operation_id=command_id,
                    command_id=command_id,
                    tool=resolved_tool,
                    status=canonical_status,
                    success=canonical_success,
                    exit_code=canonical_exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    artifacts=artifacts,
                    error_code=None,
                    error_message=None,
                    result={},
                    metadata=metadata,
                )
                execution = RunnerResultIngestService(db).ingest_tool_result(
                    tenant_id=tenant_id,
                    runtime_job=runtime_job,
                    payload=tool_result_payload,
                    runtime_job_status=terminal_status,
                )

                result_json = sanitize_tool_result_payload_for_persistence(
                    {
                        "source": "control_plane_finalize",
                        "operation_id": command_id,
                        "command_id": command_id,
                        "tool": resolved_tool,
                        "status": canonical_status,
                        "success": canonical_success,
                        "exit_code": canonical_exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "artifacts": list(artifacts),
                        "error_code": None,
                        "error_message": None,
                        "result": {},
                        "metadata": dict(metadata),
                    }
                )

                current_status = str(runtime_job.status or "").strip().lower()
                if current_status not in {"succeeded", "failed", "cancelled", "lost", "expired"}:
                    try:
                        RuntimeJobService(db).transition_runtime_job(
                            tenant_id=tenant_id,
                            runtime_job_id=runtime_job.id,
                            next_status=terminal_status,
                            result_json=result_json,
                        )
                    except RuntimeJobServiceError as exc:
                        if exc.error_code != RUNTIME_JOB_TRANSITION_STALE:
                            raise
                elif current_status == terminal_status:
                    runtime_job.result_json = result_json
                    db.flush()
                else:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=RUNTIME_JOB_TRANSITION_STALE,
                        error_message="tool.command runtime job already has a conflicting terminal status.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                db.commit()
                refreshed_job = db.execute(
                    select(RuntimeJob).where(RuntimeJob.id == runtime_job.id)
                ).scalar_one()
                delegate_result = self._projector.project_delegate_result(
                    runtime_job_status=str(refreshed_job.status or terminal_status),
                    runtime_job_error_code=refreshed_job.error_code,
                    runtime_job_error_message=refreshed_job.error_message,
                    raw_result=result_json,
                    command_id=command_id,
                    tool=resolved_tool,
                )
                return build_runtime_result(
                    request,
                    accepted=True,
                    provider=self._provider_name,
                    status=RuntimeOperationStatus.SUCCEEDED,
                    metadata={
                        "protocol_domain": "remote_runtime",
                        "operation_name": operation_name,
                        "runtime_job_status": str(refreshed_job.status or terminal_status),
                        "delegate_result": delegate_result,
                        "execution_id": str(execution.id),
                    },
                )
        except RuntimeJobServiceError as exc:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=exc.error_code,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        except Exception as exc:  # pragma: no cover - defensive provider boundary fallback
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code=_RUNNER_DISPATCH_FAILED,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
