"""Tool-command result waiter for cloud runner dispatch.

This module owns polling for terminal tool.command results, timeout
terminalization, and artifact-promotion timeout handling. It does not enqueue
commands, validate payloads, finalize tool results, or import the provider
facade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import RuntimeJob
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
from backend.services.runtime_provider.tool_wait_policy import (
    resolve_tool_result_wait_timeout_seconds,
)

from ..constants import _RESULT_PENDING_RUNTIME_JOB_STATUSES
from ..error_codes import (
    RUNTIME_JOB_TRANSITION_STALE,
    _RUNNER_ARTIFACT_UPLOAD_TIMEOUT,
    _RUNNER_DISPATCH_FAILED,
    _RUNNER_TOOL_RESULT_CANCELLED,
    _RUNNER_TOOL_RESULT_TIMEOUT,
)
from ..normalization import (
    _coerce_non_negative_float,
    _normalize_tenant_id,
)
from .projection import (
    ToolCommandResultProjector,
    has_pending_artifact_promotion,
    tool_command_process_result_available,
)

SessionFactory = Callable[[], Session]
RuntimeJobServiceFactory = Callable[[Session], RuntimeJobService]
MonotonicClock = Callable[[], float]


class ToolCommandResultWaiter:
    """Polls runtime-job state until a tool.command result is terminal."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        runtime_job_service_factory: RuntimeJobServiceFactory,
        provider_name: str,
        projector: ToolCommandResultProjector,
        monotonic_clock: MonotonicClock = monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_job_service_factory = runtime_job_service_factory
        self._provider_name = provider_name
        self._projector = projector
        self._monotonic = monotonic_clock

    def resolve_wait_deadline(
        self,
        *,
        request: RuntimeOperationRequest,
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any],
    ) -> float:
        wait_timeout_seconds = resolve_tool_result_wait_timeout_seconds(
            request=request,
            timeout_seconds=timeout_seconds,
            timeout_policy=timeout_policy,
        )
        return self._monotonic() + max(0.0, wait_timeout_seconds)

    async def wait_for_tool_command_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job_id: UUID,
        metadata: Mapping[str, Any],
        tool: str,
        command_id: str,
        ack_result: RuntimeOperationResult,
        timeout_seconds: float,
        timeout_policy: Mapping[str, Any],
        wait_deadline: float | None = None,
    ) -> RuntimeOperationResult:
        wait_timeout_seconds = resolve_tool_result_wait_timeout_seconds(
            request=request,
            timeout_seconds=timeout_seconds,
            timeout_policy=timeout_policy,
        )
        poll_seconds = _coerce_non_negative_float(
            request.metadata.get("wait_poll_seconds"),
            default=0.05,
        )
        deadline = self._monotonic() + max(0.0, wait_timeout_seconds)
        if wait_deadline is not None:
            deadline = min(deadline, wait_deadline)
        tenant_id = _normalize_tenant_id(request.tenant_id)
        latest_runtime_job_status = str(
            (
                ack_result.metadata.get("runtime_job_status")
                if isinstance(ack_result.metadata, Mapping)
                else metadata.get("runtime_job_status")
            )
            or ""
        ).strip().lower()

        try:
            while self._monotonic() <= deadline:
                with self._session_factory() as db:
                    runtime_job = db.execute(
                        select(RuntimeJob).where(
                            RuntimeJob.id == runtime_job_id,
                            RuntimeJob.tenant_id == tenant_id,
                        )
                    ).scalar_one_or_none()
                    if runtime_job is None:
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.FAILED,
                            error_code=_RUNNER_DISPATCH_FAILED,
                            error_message="Runtime job no longer exists while waiting for tool.result.",
                            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                        )
                    latest_runtime_job_status = str(runtime_job.status or "").strip().lower()
                    result_json = (
                        runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
                    )
                    if latest_runtime_job_status in _RESULT_PENDING_RUNTIME_JOB_STATUSES:
                        if tool_command_process_result_available(result_json):
                            return self._projector.build_tool_command_availability_result(
                                request=request,
                                operation_name=operation_name,
                                runtime_job=runtime_job,
                                metadata=metadata,
                                command_id=command_id,
                                tool=tool,
                            )
                    else:
                        return self._projector.build_tool_command_terminal_result(
                            request=request,
                            operation_name=operation_name,
                            runtime_job=runtime_job,
                            metadata=metadata,
                            command_id=command_id,
                            tool=tool,
                        )
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(max(0.01, poll_seconds), remaining))
        except asyncio.CancelledError:
            return self._terminalize_tool_command_wait_outcome(
                request=request,
                operation_name=operation_name,
                runtime_job_id=runtime_job_id,
                metadata=metadata,
                command_id=command_id,
                tool=tool,
                terminal_status="cancelled",
                error_code=_RUNNER_TOOL_RESULT_CANCELLED,
                error_message="Tool command result waiter was cancelled before terminal result.",
                delegate_result={
                    "command_id": command_id,
                    "tool": tool,
                    "success": False,
                    "status": "cancelled",
                    "exit_code": 130,
                    "stdout": "",
                    "stderr": "Tool command result waiter was cancelled.",
                    "error_code": _RUNNER_TOOL_RESULT_CANCELLED,
                    "error_message": "Tool command result waiter was cancelled.",
                    "artifacts": [],
                    "result": {},
                    "metadata": {"source": "cloud_waiter"},
                    "operation_id": None,
                },
                latest_runtime_job_status=latest_runtime_job_status,
            )

        artifact_timeout = self._artifact_promotion_timeout_result(
            request=request,
            runtime_job_id=runtime_job_id,
            command_id=command_id,
            tool=tool,
        )
        if artifact_timeout is not None:
            timeout_error_code, timeout_message, timeout_delegate = artifact_timeout
        else:
            timeout_error_code = _RUNNER_TOOL_RESULT_TIMEOUT
            timeout_message = (
                "Timed out waiting for terminal tool.result after runner delivery acknowledgment."
            )
            timeout_delegate = {
                "command_id": command_id,
                "tool": tool,
                "success": False,
                "status": "timed_out",
                "exit_code": 124,
                "stdout": "",
                "stderr": timeout_message,
                "error_code": timeout_error_code,
                "error_message": timeout_message,
                "artifacts": [],
                "result": {},
                "metadata": {"source": "cloud_waiter"},
                "operation_id": None,
            }
        return self._terminalize_tool_command_wait_outcome(
            request=request,
            operation_name=operation_name,
            runtime_job_id=runtime_job_id,
            metadata=metadata,
            command_id=command_id,
            tool=tool,
            terminal_status="failed",
            error_code=timeout_error_code,
            error_message=timeout_message,
            delegate_result=timeout_delegate,
            latest_runtime_job_status=latest_runtime_job_status,
            wait_timeout_seconds=wait_timeout_seconds,
        )

    def _artifact_promotion_timeout_result(
        self,
        *,
        request: RuntimeOperationRequest,
        runtime_job_id: UUID,
        command_id: str,
        tool: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        tenant_id = _normalize_tenant_id(request.tenant_id)
        with self._session_factory() as db:
            runtime_job = db.execute(
                select(RuntimeJob).where(
                    RuntimeJob.id == runtime_job_id,
                    RuntimeJob.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if runtime_job is None:
                return None
            result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
        metadata = result_json.get("metadata")
        if not has_pending_artifact_promotion(metadata):
            return None
        message = "Timed out waiting for runner artifact upload promotion after tool execution."
        delegate = self._projector.project_delegate_result(
            runtime_job_status="failed",
            runtime_job_error_code=_RUNNER_ARTIFACT_UPLOAD_TIMEOUT,
            runtime_job_error_message=message,
            raw_result=result_json,
            command_id=command_id,
            tool=tool,
        )
        delegate.update(
            {
                "success": False,
                "status": "failed",
                "stderr": message,
                "error_code": _RUNNER_ARTIFACT_UPLOAD_TIMEOUT,
                "error_message": message,
            }
        )
        return _RUNNER_ARTIFACT_UPLOAD_TIMEOUT, message, delegate

    def _terminalize_tool_command_wait_outcome(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job_id: UUID,
        metadata: Mapping[str, Any],
        command_id: str,
        tool: str,
        terminal_status: str,
        error_code: str,
        error_message: str,
        delegate_result: Mapping[str, Any],
        latest_runtime_job_status: str,
        wait_timeout_seconds: float | None = None,
    ) -> RuntimeOperationResult:
        tenant_id = _normalize_tenant_id(request.tenant_id)
        delegate_result_copy = dict(delegate_result)
        with self._session_factory() as db:
            runtime_job = db.execute(
                select(RuntimeJob).where(
                    RuntimeJob.id == runtime_job_id,
                    RuntimeJob.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if runtime_job is None:
                return build_runtime_result(
                    request,
                    accepted=False,
                    provider=self._provider_name,
                    status=RuntimeOperationStatus.FAILED,
                    error_code=_RUNNER_DISPATCH_FAILED,
                    error_message="Runtime job no longer exists while finalizing tool wait outcome.",
                    metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                )

            runtime_job_status = str(runtime_job.status or "").strip().lower()
            if runtime_job_status in _RESULT_PENDING_RUNTIME_JOB_STATUSES:
                try:
                    runtime_job = self._runtime_job_service_factory(db).transition_runtime_job(
                        tenant_id=tenant_id,
                        runtime_job_id=runtime_job_id,
                        next_status=terminal_status,
                        result_json=delegate_result_copy,
                        error_code=error_code,
                        error_message=error_message,
                    )
                except RuntimeJobServiceError as exc:
                    if exc.error_code not in {
                        RUNTIME_JOB_TRANSITION_STALE,
                        "RUNTIME_JOB_TRANSITION_INVALID",
                    }:
                        raise
                    runtime_job = db.execute(
                        select(RuntimeJob).where(
                            RuntimeJob.id == runtime_job_id,
                            RuntimeJob.tenant_id == tenant_id,
                        )
                    ).scalar_one_or_none()
                    if runtime_job is None:
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.FAILED,
                            error_code=_RUNNER_DISPATCH_FAILED,
                            error_message="Runtime job no longer exists while finalizing tool wait outcome.",
                            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                        )

            terminal_metadata = dict(metadata)
            terminal_metadata.setdefault("runtime_job_id", str(runtime_job_id))
            terminal_metadata["runtime_job_status"] = (
                str(runtime_job.status or "").strip().lower() or latest_runtime_job_status
            )
            if wait_timeout_seconds is not None:
                terminal_metadata["wait_timeout_seconds"] = wait_timeout_seconds
            return self._projector.build_tool_command_terminal_result(
                request=request,
                operation_name=operation_name,
                runtime_job=runtime_job,
                metadata=terminal_metadata,
                command_id=command_id,
                tool=tool,
            )
