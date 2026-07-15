"""Tool-command acknowledgment waiter for cloud runner dispatch.

This module owns polling for runner delivery acknowledgment of queued
tool.command jobs. It does not enqueue commands, wait for terminal tool
results, project tool results, or import the provider facade.
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
from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)

from ..constants import _TOOL_COMMAND_ACK_PENDING_RUNTIME_JOB_STATUSES
from ..error_codes import (
    _RUNNER_ACK_FAILED,
    _RUNNER_ACK_TIMEOUT,
    _RUNNER_DISPATCH_FAILED,
)
from ..normalization import (
    _coerce_non_negative_float,
    _normalize_tenant_id,
)

SessionFactory = Callable[[], Session]


class ToolCommandAckWaiter:
    """Polls runtime-job state until runner tool.command delivery is acknowledged."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._provider_name = provider_name

    async def wait_for_tool_command_ack(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job_id: UUID,
        metadata: Mapping[str, Any],
        wait_deadline: float | None = None,
    ) -> RuntimeOperationResult:
        timeout_seconds = _coerce_non_negative_float(
            request.metadata.get("ack_wait_timeout_seconds")
            if "ack_wait_timeout_seconds" in request.metadata
            else request.metadata.get("wait_timeout_seconds")
            if "wait_timeout_seconds" in request.metadata
            else request.timeout_seconds,
            default=5.0,
        )
        poll_seconds = _coerce_non_negative_float(
            request.metadata.get("ack_wait_poll_seconds")
            if "ack_wait_poll_seconds" in request.metadata
            else request.metadata.get("wait_poll_seconds"),
            default=0.05,
        )
        started_at = monotonic()
        deadline = started_at + max(0.0, timeout_seconds)
        if wait_deadline is not None:
            deadline = min(deadline, wait_deadline)
        tenant_id = _normalize_tenant_id(request.tenant_id)
        latest_runtime_job_status = str(metadata.get("runtime_job_status") or "").strip().lower()

        while True:
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
                        error_message="Runtime job no longer exists while waiting for runner ack.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                latest_runtime_job_status = str(runtime_job.status or "").strip().lower()
                if latest_runtime_job_status in _TOOL_COMMAND_ACK_PENDING_RUNTIME_JOB_STATUSES:
                    pass
                else:
                    ack_metadata = dict(metadata)
                    ack_metadata["runtime_job_status"] = latest_runtime_job_status
                    if latest_runtime_job_status == "failed":
                        result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
                        if result_json:
                            ack_metadata["runner_ack"] = dict(result_json)
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.FAILED,
                            error_code=str(runtime_job.error_code or _RUNNER_ACK_FAILED),
                            error_message=str(
                                runtime_job.error_message
                                or "Runner reported tool.command acknowledgment failure."
                            ),
                            metadata=ack_metadata,
                        )
                    return build_runtime_result(
                        request,
                        accepted=True,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.ACCEPTED,
                        metadata=ack_metadata,
                    )
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.01, poll_seconds), remaining))

        timeout_metadata = dict(metadata)
        timeout_metadata["runtime_job_status"] = latest_runtime_job_status
        if latest_runtime_job_status in _TOOL_COMMAND_ACK_PENDING_RUNTIME_JOB_STATUSES:
            timeout_metadata["ack_wait_timed_out"] = True
            return build_runtime_result(
                request,
                accepted=True,
                provider=self._provider_name,
                status=RuntimeOperationStatus.ACCEPTED,
                metadata=timeout_metadata,
            )
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_ACK_TIMEOUT,
            error_message="Timed out waiting for runner acknowledgment of tool.command delivery.",
            metadata=timeout_metadata,
        )
