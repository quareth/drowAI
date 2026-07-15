"""Runtime result builder helpers for cloud runner provider collaborators."""

from __future__ import annotations

from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)

from .error_codes import RUNNER_REMOTE_OPERATION_INVALID_REQUEST


class CloudRunnerResultBuilder:
    """Build provider-shaped cloud runner operation results."""

    def __init__(self, *, provider_name: str) -> None:
        self._provider_name = provider_name

    def _invalid_request_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        message: str,
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=RUNNER_REMOTE_OPERATION_INVALID_REQUEST,
            error_message=message,
            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
        )

    def _deferred_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        error_code: str,
        message: str,
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=error_code,
            error_message=message,
            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
        )
