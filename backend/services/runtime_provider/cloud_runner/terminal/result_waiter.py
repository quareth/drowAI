"""Terminal result waiting for cloud runner runtime operations.

This module owns wait/no-wait decisions and runtime-job polling/projection for
terminal operation results. It does not own public terminal operation bodies,
terminal stream attachment, artifacts, tool commands, or the provider facade.
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

from ..constants import _TERMINAL_PENDING_RUNTIME_JOB_STATUSES
from ..dispatch.operation_waiter import _should_wait_for_operation_result
from ..error_codes import (
    _RUNNER_DISPATCH_FAILED,
    _RUNNER_TERMINAL_RESULT_MISMATCH,
    _RUNNER_TERMINAL_RESULT_TIMEOUT,
)
from ..normalization import (
    _coerce_non_negative_float,
    _normalize_tenant_id,
    _resolve_optional_text,
)
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)


class CloudRunnerTerminalResultWaiter:
    """Polls runtime jobs and projects terminal operation results."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._provider_name = provider_name

    async def _wait_for_terminal_result(
        self,
        *,
        request: RuntimeOperationRequest,
        dispatch_result: RuntimeOperationResult,
        operation_name: str,
        expected_terminal_operation: str,
    ) -> RuntimeOperationResult:
        if not dispatch_result.accepted:
            return dispatch_result
        runtime_job_id_text = _resolve_optional_text(dispatch_result.metadata.get("runtime_job_id"))
        if runtime_job_id_text is None:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code=_RUNNER_DISPATCH_FAILED,
                error_message="Cloud runner dispatch did not return a runtime job id.",
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        timeout_seconds = _coerce_non_negative_float(
            request.metadata.get("wait_timeout_seconds")
            if "wait_timeout_seconds" in request.metadata
            else request.timeout_seconds,
            default=5.0,
        )
        poll_seconds = _coerce_non_negative_float(request.metadata.get("wait_poll_seconds"), default=0.05)
        try:
            runtime_job_uuid = UUID(runtime_job_id_text)
        except ValueError:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.FAILED,
                error_code=_RUNNER_DISPATCH_FAILED,
                error_message="Cloud runner dispatch returned a malformed runtime job id.",
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        deadline = monotonic() + timeout_seconds
        tenant_id = _normalize_tenant_id(request.tenant_id)

        latest_runtime_job_status = str(dispatch_result.metadata.get("runtime_job_status") or "")
        while monotonic() <= deadline:
            with self._session_factory() as db:
                runtime_job = db.execute(
                    select(RuntimeJob).where(
                        RuntimeJob.id == runtime_job_uuid,
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
                        error_message="Runtime job no longer exists while waiting for terminal result.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                latest_runtime_job_status = str(runtime_job.status or "").strip().lower()
                if latest_runtime_job_status in _TERMINAL_PENDING_RUNTIME_JOB_STATUSES:
                    pass
                else:
                    result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
                    terminal_operation = str(result_json.get("terminal_operation") or "").strip().lower()
                    if terminal_operation and terminal_operation != expected_terminal_operation:
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.REJECTED,
                            error_code=_RUNNER_TERMINAL_RESULT_MISMATCH,
                            error_message=(
                                f"Terminal result operation mismatch: expected `{expected_terminal_operation}`, "
                                f"received `{terminal_operation}`."
                            ),
                            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                        )

                    requested_session_id = _resolve_optional_text(
                        request.payload.get("session_id") or request.metadata.get("session_id")
                    )
                    result_session_id = _resolve_optional_text(result_json.get("session_id"))
                    if (
                        requested_session_id is not None
                        and result_session_id is not None
                        and result_session_id != requested_session_id
                    ):
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.REJECTED,
                            error_code=_RUNNER_TERMINAL_RESULT_MISMATCH,
                            error_message=(
                                f"Terminal result session mismatch: expected `{requested_session_id}`, "
                                f"received `{result_session_id}`."
                            ),
                            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                        )

                    delegate_session_id = result_session_id or requested_session_id
                    delegate_result: dict[str, Any] = {
                        "runtime_job_id": runtime_job_id_text,
                        "session_id": delegate_session_id,
                        "sequence": result_json.get("sequence"),
                    }
                    result_payload = result_json.get("result")
                    if isinstance(result_payload, Mapping):
                        delegate_result.update(
                            {
                                str(key): value
                                for key, value in result_payload.items()
                            }
                        )
                    if delegate_session_id is not None:
                        delegate_result.setdefault("exec_id", delegate_session_id)
                    delegate_result.setdefault(
                        "container_name",
                        f"drowai-task-{request.task_id}",
                    )

                    if latest_runtime_job_status == "succeeded":
                        metadata = dict(dispatch_result.metadata)
                        metadata["runtime_job_status"] = latest_runtime_job_status
                        metadata["delegate_result"] = delegate_result
                        return build_runtime_result(
                            request,
                            accepted=True,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.SUCCEEDED,
                            metadata=metadata,
                        )

                    error_code = str(runtime_job.error_code or "RUNNER_TERMINAL_OPERATION_FAILED")
                    error_message = str(runtime_job.error_message or "Runner terminal operation failed.")
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.FAILED,
                        error_code=error_code,
                        error_message=error_message,
                        metadata={
                            "protocol_domain": "remote_runtime",
                            "operation_name": operation_name,
                            "runtime_job_id": runtime_job_id_text,
                            "runtime_job_status": latest_runtime_job_status,
                            "delegate_result": delegate_result,
                        },
                    )
            await asyncio.sleep(max(0.01, poll_seconds))

        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_TERMINAL_RESULT_TIMEOUT,
            error_message=(
                "Timed out waiting for validated terminal.result event "
                f"for operation `{expected_terminal_operation}`."
            ),
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": operation_name,
                "runtime_job_id": runtime_job_id_text,
                "runtime_job_status": latest_runtime_job_status,
            },
        )


def _should_wait_for_terminal_result(request: RuntimeOperationRequest) -> bool:
    return _should_wait_for_operation_result(request)
