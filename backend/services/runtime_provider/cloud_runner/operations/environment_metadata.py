"""Environment metadata operations for the cloud runner provider.

This module owns runner environment metadata allowlist policy, read/write/query
operation bodies, and metadata-specific rejection results. It delegates dispatch
and generic result polling to bounded collaborators and does not import the
provider facade or broader synchronous environment metadata helpers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime_shared.runner_protocol import RunnerMessageType

from ..dispatch.operation_waiter import (
    CloudRunnerOperationWaiter,
    _should_wait_for_operation_result,
)
from ..dispatch.remote_dispatcher import CloudRunnerRemoteDispatcher
from ..error_codes import (
    _RUNNER_ENV_METADATA_FILTER_UNSUPPORTED,
    _RUNNER_ENV_METADATA_KEY_UNSUPPORTED,
)
from ..normalization import _resolve_optional_text
from ..payload_codec import _coerce_transport_value, _prepare_transport_params
from ..result_builders import CloudRunnerResultBuilder
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)

_ALLOWED_ENV_METADATA_KEYS = frozenset(
    {
        "agent.version",
    }
)
_ALLOWED_ENV_METADATA_QUERY_FILTERS = frozenset(
    {
        "key_prefix",
    }
)


class CloudRunnerEnvironmentMetadataOperations:
    """Handles runner environment metadata provider operations."""

    def __init__(
        self,
        *,
        remote_dispatcher: CloudRunnerRemoteDispatcher,
        operation_waiter: CloudRunnerOperationWaiter,
        result_builder: CloudRunnerResultBuilder,
        provider_name: str,
    ) -> None:
        self._remote_dispatcher = remote_dispatcher
        self._operation_waiter = operation_waiter
        self._result_builder = result_builder
        self._provider_name = provider_name

    async def read_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        key = _resolve_optional_text(request.payload.get("key"))
        if key is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name="read_runtime_environment_metadata",
                message="`key` is required for read_runtime_environment_metadata.",
            )
        unsupported_key_result = self._unsupported_environment_metadata_key_result(
            request=request,
            operation_name="read_runtime_environment_metadata",
            key=key,
        )
        if unsupported_key_result is not None:
            return unsupported_key_result
        params = {"action": "read", "key": key}
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="read_runtime_environment_metadata",
            message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="read_runtime_environment_metadata",
            expected_message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        )

    async def write_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        key = _resolve_optional_text(request.payload.get("key"))
        if key is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name="write_runtime_environment_metadata",
                message="`key` is required for write_runtime_environment_metadata.",
            )
        if "value" not in request.payload:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name="write_runtime_environment_metadata",
                message="`value` is required for write_runtime_environment_metadata.",
            )
        unsupported_key_result = self._unsupported_environment_metadata_key_result(
            request=request,
            operation_name="write_runtime_environment_metadata",
            key=key,
        )
        if unsupported_key_result is not None:
            return unsupported_key_result
        params = {
            "action": "write",
            "key": key,
            "value": _coerce_transport_value(request.payload.get("value")),
        }
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="write_runtime_environment_metadata",
            message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="write_runtime_environment_metadata",
            expected_message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        )

    async def query_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        filters = self._build_query_filters(request.payload)
        unsupported_filters = sorted(
            key for key in filters if key not in _ALLOWED_ENV_METADATA_QUERY_FILTERS
        )
        if unsupported_filters:
            return self._unsupported_environment_metadata_filters_result(
                request=request,
                operation_name="query_runtime_environment_metadata",
                unsupported_filters=unsupported_filters,
            )
        if "key_prefix" in filters:
            key_prefix = _resolve_optional_text(filters.get("key_prefix")) or ""
            if key_prefix and not any(
                allowlisted_key.startswith(key_prefix)
                for allowlisted_key in _ALLOWED_ENV_METADATA_KEYS
            ):
                return self._unsupported_environment_metadata_filters_result(
                    request=request,
                    operation_name="query_runtime_environment_metadata",
                    unsupported_filters=[f"key_prefix:{key_prefix}"],
                )
            filters["key_prefix"] = key_prefix
        params = {"action": "query", "filters": filters}
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="query_runtime_environment_metadata",
            message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="query_runtime_environment_metadata",
            expected_message_type=RunnerMessageType.RUNTIME_ENVIRONMENT_METADATA,
        )

    def _unsupported_environment_metadata_key_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        key: str,
    ) -> RuntimeOperationResult | None:
        normalized_key = key.strip()
        if normalized_key in _ALLOWED_ENV_METADATA_KEYS:
            return None
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_ENV_METADATA_KEY_UNSUPPORTED,
            error_message=(
                "Environment metadata key is not supported for runner dispatch. "
                f"key={normalized_key!r}; allowlisted_keys={sorted(_ALLOWED_ENV_METADATA_KEYS)}"
            ),
            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
        )

    def _unsupported_environment_metadata_filters_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        unsupported_filters: list[str],
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_ENV_METADATA_FILTER_UNSUPPORTED,
            error_message=(
                "Environment metadata query filters are not supported for runner dispatch. "
                f"unsupported={unsupported_filters}; allowlisted_filters={sorted(_ALLOWED_ENV_METADATA_QUERY_FILTERS)}"
            ),
            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
        )

    def _build_query_filters(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_filters = payload.get("filters")
        if isinstance(raw_filters, Mapping):
            return dict(_prepare_transport_params(raw_filters))
        filters: dict[str, Any] = {}
        if "key_prefix" in payload:
            filters["key_prefix"] = str(payload.get("key_prefix") or "")
        return filters
