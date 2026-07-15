"""Lifecycle and simple remote operations for the cloud runner provider.

This module owns task lifecycle, VPN, status, log, metric, inventory, cleanup,
and explicit rejection operation bodies. It delegates dispatch and generic
operation-result polling to their bounded collaborators and does not import the
provider facade.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from runtime_shared.runner_protocol import RunnerMessageType

from ..dispatch.operation_waiter import (
    CloudRunnerOperationWaiter,
    _should_wait_for_operation_result,
)
from ..dispatch.remote_dispatcher import CloudRunnerRemoteDispatcher
from ..error_codes import (
    _RUNNER_LOCAL_ONLY,
    _RUNNER_TOOL_COMMAND_CALLABLE_UNSUPPORTED,
    _RUNNER_TOOL_COMMAND_COMPATIBILITY_ONLY,
)
from ..jobs.queries import CloudRunnerRuntimeJobQueries
from ..normalization import _normalize_optional_uuid, _normalize_tenant_id
from ..payload_codec import _prepare_transport_params
from ..result_builders import CloudRunnerResultBuilder
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)


class CloudRunnerLifecycleOperations:
    """Handles lifecycle and simple remote-operation provider behavior."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        remote_dispatcher: CloudRunnerRemoteDispatcher,
        operation_waiter: CloudRunnerOperationWaiter,
        result_builder: CloudRunnerResultBuilder,
        runtime_job_queries: CloudRunnerRuntimeJobQueries,
        provider_name: str,
    ) -> None:
        self._session_factory = session_factory
        self._remote_dispatcher = remote_dispatcher
        self._operation_waiter = operation_waiter
        self._result_builder = result_builder
        self._runtime_job_queries = runtime_job_queries
        self._provider_name = provider_name

    async def provision_task_runtime(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="provision_task_runtime",
            message_type=RunnerMessageType.TASK_START,
            params=self._build_task_start_params(request),
        )

    async def materialize_runtime_workspace(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=True,
            provider=self._provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": "materialize_runtime_workspace",
                "mode": "management_plane_noop",
                "workspace_id": request.workspace_id,
            },
        )

    async def pause_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="pause_task_runtime",
            message_type=RunnerMessageType.TASK_PAUSE,
            params=request.payload,
        )

    async def resume_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="resume_task_runtime",
            message_type=RunnerMessageType.TASK_RESUME,
            params=request.payload,
        )

    async def stop_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        lifecycle_intent = str(request.payload.get("lifecycle_intent", "stop")).strip().lower()
        if lifecycle_intent not in {"stop", "cancel"}:
            lifecycle_intent = "stop"
        params = dict(_prepare_transport_params(request.payload))
        params["lifecycle_intent"] = lifecycle_intent
        return self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="stop_task_runtime",
            message_type=RunnerMessageType.TASK_STOP,
            params=params,
        )

    async def retire_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        dispatch_request = self._resolve_retire_dispatch_request(request)
        if dispatch_request is None:
            return build_runtime_result(
                request,
                accepted=True,
                provider=self._provider_name,
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": "retire_task_runtime",
                    "mode": "already_retired",
                    "workspace_id": request.workspace_id,
                    "reason": "no_active_task_start_runtime_job",
                },
            )

        result = self._remote_dispatcher._dispatch_remote_operation(
            request=dispatch_request,
            operation_name="retire_task_runtime",
            message_type=RunnerMessageType.TASK_RETIRE,
            params=dispatch_request.payload,
        )
        if not _should_wait_for_operation_result(dispatch_request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=dispatch_request,
            dispatch_result=result,
            operation_name="retire_task_runtime",
            expected_message_type=RunnerMessageType.RUNTIME_RETIRED,
        )

    async def append_runtime_input(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="append_runtime_input",
            message_type=RunnerMessageType.RUNTIME_INPUT,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="append_runtime_input",
            expected_message_type=RunnerMessageType.RUNTIME_INPUT,
        )

    async def materialize_vpn_config(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="materialize_vpn_config",
            message_type=RunnerMessageType.RUNTIME_VPN_CONFIG,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="materialize_vpn_config",
            expected_message_type=RunnerMessageType.RUNTIME_VPN_CONFIG,
        )

    async def retry_vpn_connection(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="retry_vpn_connection",
            message_type=RunnerMessageType.RUNTIME_VPN_RETRY,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="retry_vpn_connection",
            expected_message_type=RunnerMessageType.RUNTIME_VPN_RETRY,
        )

    async def check_vpn_status(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="check_vpn_status",
            message_type=RunnerMessageType.RUNTIME_VPN_STATUS,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="check_vpn_status",
            expected_message_type=RunnerMessageType.RUNTIME_VPN_STATUS,
        )

    async def get_runtime_status(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="get_runtime_status",
            message_type=RunnerMessageType.RUNTIME_STATUS,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="get_runtime_status",
            expected_message_type=RunnerMessageType.RUNTIME_STATUS,
        )

    async def get_runtime_startup_progress(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="get_runtime_startup_progress",
            message_type=RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="get_runtime_startup_progress",
            expected_message_type=RunnerMessageType.RUNTIME_STARTUP_PROGRESS,
        )

    async def get_runtime_logs(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="get_runtime_logs",
            message_type=RunnerMessageType.RUNTIME_LOGS,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="get_runtime_logs",
            expected_message_type=RunnerMessageType.RUNTIME_LOGS,
        )

    async def get_runtime_metrics(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="get_runtime_metrics",
            message_type=RunnerMessageType.RUNTIME_METRICS,
            params=request.payload,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="get_runtime_metrics",
            expected_message_type=RunnerMessageType.RUNTIME_METRICS,
        )

    async def list_runtime_inventory(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        params = dict(_prepare_transport_params(request.payload))
        params.setdefault("scope", "task")
        filters = params.get("filters")
        if not isinstance(filters, Mapping):
            params["filters"] = {}
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="list_runtime_inventory",
            message_type=RunnerMessageType.RUNTIME_INVENTORY,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="list_runtime_inventory",
            expected_message_type=RunnerMessageType.RUNTIME_INVENTORY,
        )

    async def cleanup_runtime_workspace(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        params = dict(_prepare_transport_params(request.payload))
        cleanup_scope = str(params.get("cleanup_scope", "workspace")).strip().lower()
        if cleanup_scope not in {"workspace", "runtime", "all"}:
            cleanup_scope = "workspace"
        params["cleanup_scope"] = cleanup_scope
        params["retain_outputs"] = bool(params.get("retain_outputs", True))
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="cleanup_runtime_workspace",
            message_type=RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name="cleanup_runtime_workspace",
            expected_message_type=RunnerMessageType.RUNTIME_WORKSPACE_CLEANUP,
        )

    async def execute_runtime_command(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return self._result_builder._deferred_result(
            request=request,
            operation_name="execute_runtime_command",
            error_code=_RUNNER_LOCAL_ONLY,
            message=(
                "Runner-placement runtime command execution is local-only. "
                "Cloud provider will not fallback to backend Docker exec."
            ),
        )

    async def dispatch_tool_execution(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        dispatch_callable = request.payload.get("dispatch_callable")
        if callable(dispatch_callable):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_CALLABLE_UNSUPPORTED,
                error_message=(
                    "Cloud runner dispatch_tool_execution rejects callable payloads. "
                    "Use per-call lane routing and send_tool_command for "
                    "container_scoped runner tools."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": "dispatch_tool_execution"},
            )
        return self._result_builder._deferred_result(
            request=request,
            operation_name="dispatch_tool_execution",
            error_code=_RUNNER_TOOL_COMMAND_COMPATIBILITY_ONLY,
            message=(
                "dispatch_tool_execution is a compatibility surface only in cloud runner mode. "
                "Use per-call lane routing and send_tool_command for runner container-scoped "
                "tool execution."
            ),
        )

    def _resolve_retire_dispatch_request(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationRequest | None:
        """Return a runner-addressable retire request, or None when runtime is absent."""
        try:
            tenant_id = _normalize_tenant_id(request.tenant_id)
            requested_runner_id = _normalize_optional_uuid(request.runner_id)
        except ValueError:
            return request

        with self._session_factory() as db:
            start_job = self._runtime_job_queries._find_active_task_start_runtime_job_for_task(
                db=db,
                tenant_id=tenant_id,
                task_id=request.task_id,
                workspace_id=request.workspace_id,
            )

        if start_job is None or start_job.runner_id is None:
            return None

        runner_id = (
            str(requested_runner_id)
            if requested_runner_id is not None and requested_runner_id == start_job.runner_id
            else str(start_job.runner_id)
        )
        execution_site_id = (
            str(start_job.execution_site_id)
            if start_job.execution_site_id is not None
            else request.execution_site_id
        )
        payload = dict(request.payload)
        payload.setdefault("runtime_job_id", str(start_job.id))

        return RuntimeOperationRequest(
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            runtime_placement_mode=request.runtime_placement_mode,
            workspace_id=request.workspace_id,
            operation=request.operation,
            user_id=request.user_id,
            runner_id=runner_id,
            execution_site_id=execution_site_id,
            timeout_seconds=request.timeout_seconds,
            metadata=dict(request.metadata),
            payload=payload,
        )

    def _build_task_start_params(self, request: RuntimeOperationRequest) -> dict[str, Any]:
        params = dict(_prepare_transport_params(request.payload))
        params.setdefault("target", "127.0.0.1")
        return params
