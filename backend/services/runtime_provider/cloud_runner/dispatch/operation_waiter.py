"""Generic operation result waiter for cloud runner dispatches.

This module polls runtime-job state and projects generic remote operation
results. It does not own terminal-specific or tool-command-specific wait
semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from time import monotonic
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.runner_control import RuntimeJob
from runtime_shared.runner_protocol import RunnerMessageType

from ..constants import _RESULT_PENDING_RUNTIME_JOB_STATUSES
from ..error_codes import (
    _RUNNER_DISPATCH_FAILED,
    _RUNNER_OPERATION_RESULT_MISMATCH,
    _RUNNER_OPERATION_RESULT_TIMEOUT,
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


class CloudRunnerOperationWaiter:
    """Polls runtime jobs and projects generic remote operation results."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._provider_name = provider_name

    async def _wait_for_runtime_operation_result(
        self,
        *,
        request: RuntimeOperationRequest,
        dispatch_result: RuntimeOperationResult,
        operation_name: str,
        expected_message_type: RunnerMessageType,
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

        timeout_seconds = _coerce_non_negative_float(
            request.metadata.get("wait_timeout_seconds")
            if "wait_timeout_seconds" in request.metadata
            else request.timeout_seconds,
            default=5.0,
        )
        poll_seconds = _coerce_non_negative_float(request.metadata.get("wait_poll_seconds"), default=0.05)
        deadline = monotonic() + timeout_seconds
        tenant_id = _normalize_tenant_id(request.tenant_id)
        latest_runtime_job_status = str(dispatch_result.metadata.get("runtime_job_status") or "")

        has_polled_runtime_job = False
        while not has_polled_runtime_job or monotonic() <= deadline:
            has_polled_runtime_job = True
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
                        error_message="Runtime job no longer exists while waiting for operation result.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )
                latest_runtime_job_status = str(runtime_job.status or "").strip().lower()
                if latest_runtime_job_status in _RESULT_PENDING_RUNTIME_JOB_STATUSES:
                    pass
                else:
                    result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
                    result_message_type = str(result_json.get("message_type") or "").strip().lower()
                    expected_type = expected_message_type.value
                    if (
                        result_message_type
                        and result_message_type not in {expected_type, RunnerMessageType.RUNTIME_FAILED.value}
                    ):
                        return build_runtime_result(
                            request,
                            accepted=False,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.REJECTED,
                            error_code=_RUNNER_OPERATION_RESULT_MISMATCH,
                            error_message=(
                                f"Operation result mismatch: expected `{expected_type}`, "
                                f"received `{result_message_type}`."
                            ),
                            metadata={
                                "protocol_domain": "remote_runtime",
                                "operation_name": operation_name,
                                "runtime_job_id": runtime_job_id_text,
                                "runtime_job_status": latest_runtime_job_status,
                            },
                        )

                    delegate_result = result_json.get("result")
                    if not isinstance(delegate_result, Mapping):
                        delegate_result = {}
                    metadata = dict(dispatch_result.metadata)
                    metadata["runtime_job_status"] = latest_runtime_job_status
                    metadata["delegate_result"] = dict(delegate_result)
                    if latest_runtime_job_status == "succeeded":
                        return build_runtime_result(
                            request,
                            accepted=True,
                            provider=self._provider_name,
                            status=RuntimeOperationStatus.SUCCEEDED,
                            metadata=metadata,
                        )
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.FAILED,
                        error_code=str(runtime_job.error_code or "RUNNER_RUNTIME_OPERATION_FAILED"),
                        error_message=str(
                            runtime_job.error_message
                            or "Runner runtime operation failed while awaiting result."
                        ),
                        metadata=metadata,
                    )
            await asyncio.sleep(max(0.01, poll_seconds))

        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_OPERATION_RESULT_TIMEOUT,
            error_message=(
                "Timed out waiting for validated runtime result event "
                f"for operation `{expected_message_type.value}`."
            ),
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": operation_name,
                "runtime_job_id": runtime_job_id_text,
                "runtime_job_status": latest_runtime_job_status,
            },
        )


def _should_wait_for_operation_result(request: RuntimeOperationRequest) -> bool:
    raw = request.metadata.get("wait_for_result")
    if raw is None:
        raw = request.payload.get("wait_for_result")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False
