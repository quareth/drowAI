"""Tool-command dispatch shell for the cloud runner provider.

This module owns provider-side send_tool_command orchestration while delegating
validation, payload construction, and ack waiting to focused collaborators. It
does not import the provider facade.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.feature_flags import is_runner_tool_command_enabled
from backend.models.runner_control import Runner, RuntimeJob
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.runtime_job_service import (
    RuntimeJobCreateRequest,
    RuntimeJobService,
    RuntimeJobServiceError,
)
from backend.services.runtime_provider.contracts import (
    RuntimePlacementMode,
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)
from runtime_shared.runner_protocol import (
    RunnerMessageType,
)
from runtime_shared.workspace_files import RuntimeWorkspaceFileError

from ..constants import (
    _TOOL_COMMAND_JOB_TYPE,
)
from ..dispatch.operation_waiter import _should_wait_for_operation_result
from ..error_codes import (
    _RUNNER_ASSIGNMENT_REQUIRED,
    _RUNNER_DISPATCH_FAILED,
    _RUNNER_IDENTITY_INVALID,
    _RUNNER_RUNTIME_PLACEMENT_UNSUPPORTED,
    _RUNNER_TASK_RUNTIME_REQUIRED,
    _RUNNER_TOOL_COMMAND_DISABLED,
)
from ..jobs.identity import (
    _is_terminal_runtime_job_status,
)
from ..jobs.queries import CloudRunnerRuntimeJobQueries
from ..normalization import (
    _normalize_optional_uuid,
    _normalize_tenant_id,
    _resolve_optional_text,
)
from ..result_builders import CloudRunnerResultBuilder
from .ack_waiter import ToolCommandAckWaiter
from .payloads import ToolCommandPayloadBuilder
from .projection import ToolCommandResultProjector
from .result_waiter import ToolCommandResultWaiter
from .validation import ToolCommandValidator

SessionFactory = Callable[[], Session]
RuntimeJobServiceFactory = Callable[[Session], RuntimeJobService]
CoordinationStoreFactory = Callable[[Session], DBRunnerCoordinationStore]


class CloudRunnerToolCommandDispatcher:
    """Dispatch cloud runner tool.command requests through runner-control."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        runtime_job_service_factory: RuntimeJobServiceFactory,
        coordination_store_factory: CoordinationStoreFactory,
        runtime_job_queries: CloudRunnerRuntimeJobQueries,
        result_builder: CloudRunnerResultBuilder,
        provider_name: str,
        projector: ToolCommandResultProjector,
        result_waiter: ToolCommandResultWaiter,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_job_service_factory = runtime_job_service_factory
        self._coordination_store_factory = coordination_store_factory
        self._runtime_job_queries = runtime_job_queries
        self._result_builder = result_builder
        self._provider_name = provider_name
        self._projector = projector
        self._result_waiter = result_waiter
        self._payload_builder = ToolCommandPayloadBuilder()
        self._validator = ToolCommandValidator(provider_name=provider_name)
        self._ack_waiter = ToolCommandAckWaiter(
            session_factory=session_factory,
            provider_name=provider_name,
        )

    async def send_tool_command(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        operation_name = "send_tool_command"
        if not is_runner_tool_command_enabled():
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_DISABLED,
                error_message="Runner tool.command dispatch is disabled by feature flag.",
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        try:
            tenant_id = _normalize_tenant_id(request.tenant_id)
            runner_id = _normalize_optional_uuid(request.runner_id)
            execution_site_id = _normalize_optional_uuid(request.execution_site_id)
        except ValueError as exc:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_IDENTITY_INVALID,
                error_message=str(exc),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        if request.runtime_placement_mode is not RuntimePlacementMode.RUNNER:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_RUNTIME_PLACEMENT_UNSUPPORTED,
                error_message=(
                    "send_tool_command requires runner placement and rejects "
                    f"`{request.runtime_placement_mode.value}` routing."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        if runner_id is None:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_ASSIGNMENT_REQUIRED,
                error_message=(
                    "Runner-placement runtime operation requires an assigned runner_id."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        tool = _resolve_optional_text(request.payload.get("tool"))
        if tool is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`tool` is required for send_tool_command.",
            )
        _, lane_error = self._validator.validate_lane(
            request=request,
            operation_name=operation_name,
            tool=tool,
        )
        if lane_error is not None:
            return lane_error

        command_id = _resolve_optional_text(request.payload.get("command_id"))
        if command_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`command_id` is required for send_tool_command.",
            )
        if "args" in request.payload:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`args` is not accepted for send_tool_command; send prepared `command`.",
            )

        command = _resolve_optional_text(request.payload.get("command"))
        if command is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`command` is required for send_tool_command.",
            )
        try:
            prepared_payloads = self._payload_builder.build_prepared_payloads(
                request=request,
                operation_name=operation_name,
            )
        except RuntimeWorkspaceFileError as exc:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message=f"workspace preparation is invalid: {exc}",
            )
        env_error = self._validator.validate_env_payload(
            request=request,
            operation_name=operation_name,
            command_id=command_id,
            env_payload=prepared_payloads.env_payload,
        )
        if env_error is not None:
            return env_error
        params_error = self._validator.validate_params_payload(
            request=request,
            operation_name=operation_name,
            command_id=command_id,
            params_payload=prepared_payloads.params_payload,
        )
        if params_error is not None:
            return params_error

        if prepared_payloads.timeout_seconds <= 0:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`timeout_seconds` must be greater than zero for send_tool_command.",
            )

        try:
            with self._session_factory() as db:
                runner = db.execute(
                    select(Runner).where(
                        Runner.tenant_id == tenant_id,
                        Runner.id == runner_id,
                    )
                ).scalar_one_or_none()
                if runner is None:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=_RUNNER_ASSIGNMENT_REQUIRED,
                        error_message="Runner assignment is missing or no longer valid.",
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                capability_error = self._validator.validate_runner_capabilities(
                    request=request,
                    operation_name=operation_name,
                    capabilities_json=runner.capabilities_json,
                )
                if capability_error is not None:
                    return capability_error

                task_runtime_job = self._runtime_job_queries._find_active_task_start_runtime_job(
                    db=db,
                    tenant_id=tenant_id,
                    task_id=request.task_id,
                    runner_id=runner_id,
                    workspace_id=request.workspace_id,
                )
                if task_runtime_job is None:
                    return build_runtime_result(
                        request,
                        accepted=False,
                        provider=self._provider_name,
                        status=RuntimeOperationStatus.REJECTED,
                        error_code=_RUNNER_TASK_RUNTIME_REQUIRED,
                        error_message=(
                            "send_tool_command requires an active task.start runtime job "
                            "for the same tenant/task/runner/workspace."
                        ),
                        metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
                    )

                task_runtime_job_id = str(task_runtime_job.id)
                command_idempotency_key = self._payload_builder.resolve_tool_command_idempotency_key(
                    request=request,
                    runner_id=runner_id,
                    task_runtime_job_id=task_runtime_job_id,
                    command_id=command_id,
                )

                existing_runtime_job = db.execute(
                    select(RuntimeJob).where(
                        RuntimeJob.tenant_id == tenant_id,
                        RuntimeJob.job_type == _TOOL_COMMAND_JOB_TYPE,
                        RuntimeJob.idempotency_key == command_idempotency_key,
                    )
                ).scalar_one_or_none()
                existing_command_runtime_job = (
                    self._runtime_job_queries._find_existing_tool_command_runtime_job_by_command_id(
                        db=db,
                        tenant_id=tenant_id,
                        command_id=command_id,
                    )
                )
                command_binding_error = self._validator.validate_existing_command_binding(
                    request=request,
                    operation_name=operation_name,
                    runtime_job=existing_command_runtime_job,
                    command_id=command_id,
                    task_runtime_job_id=task_runtime_job_id,
                    runner_id=runner_id,
                )
                if command_binding_error is not None:
                    return command_binding_error
                if existing_runtime_job is None and existing_command_runtime_job is not None:
                    existing_runtime_job = existing_command_runtime_job

                runtime_job_service = self._runtime_job_service_factory(db)
                if existing_runtime_job is not None:
                    runtime_job_binding_error = self._validator.validate_existing_runtime_job_binding(
                        request=request,
                        operation_name=operation_name,
                        runtime_job=existing_runtime_job,
                        command_id=command_id,
                        task_runtime_job_id=task_runtime_job_id,
                        runner_id=runner_id,
                    )
                    if runtime_job_binding_error is not None:
                        return runtime_job_binding_error
                    runtime_job = existing_runtime_job
                else:
                    runtime_job_payload = self._payload_builder.build_runtime_job_payload(
                        request=request,
                        operation_name=operation_name,
                        prepared=prepared_payloads,
                        execution_site_id=execution_site_id,
                        tool=tool,
                        command=command,
                        command_id=command_id,
                        task_runtime_job_id=task_runtime_job_id,
                    )
                    runtime_job = runtime_job_service.create_runtime_job(
                        RuntimeJobCreateRequest(
                            tenant_id=tenant_id,
                            task_id=request.task_id,
                            job_type=_TOOL_COMMAND_JOB_TYPE,
                            idempotency_key=command_idempotency_key,
                            payload_json=runtime_job_payload,
                            correlation_id=prepared_payloads.correlation_id,
                        )
                    )
                    runtime_job = runtime_job_service.assign_runtime_job(
                        tenant_id=tenant_id,
                        runtime_job_id=runtime_job.id,
                        runner_id=runner_id,
                    )

                raw_runtime_job_payload = getattr(runtime_job, "payload_json", {})
                runtime_job_payload = (
                    raw_runtime_job_payload if isinstance(raw_runtime_job_payload, Mapping) else {}
                )
                operation_id = (
                    _resolve_optional_text(runtime_job_payload.get("operation_id"))
                    or prepared_payloads.operation_id
                )
                runtime_image = (
                    _resolve_optional_text(runtime_job_payload.get("runtime_image"))
                    or prepared_payloads.runtime_image
                )
                task_runtime_job_id = (
                    _resolve_optional_text(runtime_job_payload.get("task_runtime_job_id"))
                    or task_runtime_job_id
                )
                command_id = _resolve_optional_text(runtime_job_payload.get("command_id")) or command_id

                runtime_job_id = str(runtime_job.id)
                message_id = None
                existing_outbound = self._runtime_job_queries._find_existing_outbound_tool_command(
                    db=db,
                    tenant_id=tenant_id,
                    runner_id=runner_id,
                    runtime_job_id=runtime_job.id,
                )
                if existing_outbound is not None:
                    message_id = str(existing_outbound.message_id)

                if _is_terminal_runtime_job_status(runtime_job.status):
                    terminal_metadata: dict[str, Any] = {
                        "protocol_domain": "remote_runtime",
                        "operation_name": operation_name,
                        "runtime_job_id": runtime_job_id,
                        "runner_runtime_job_id": runtime_job_id,
                        "task_runtime_job_id": task_runtime_job_id,
                        "runtime_job_status": str(runtime_job.status),
                        "runner_id": str(runtime_job.runner_id) if runtime_job.runner_id else None,
                        "runner_id_assigned": str(runtime_job.runner_id)
                        if runtime_job.runner_id
                        else None,
                        "message_id": message_id,
                        "control_message_id": message_id,
                        "control_message_type": RunnerMessageType.TOOL_COMMAND.value,
                        "operation_id": operation_id,
                        "runtime_image": runtime_image,
                        "command_id": command_id,
                    }
                    return self._projector.build_tool_command_terminal_result(
                        request=request,
                        operation_name=operation_name,
                        runtime_job=runtime_job,
                        metadata=terminal_metadata,
                        command_id=command_id,
                        tool=tool,
                    )

                if existing_outbound is None:
                    tool_command_payload = self._payload_builder.build_tool_command_payload(
                        request=request,
                        prepared=prepared_payloads,
                        operation_id=operation_id,
                        task_runtime_job_id=task_runtime_job_id,
                        runtime_image=runtime_image,
                        tool=tool,
                        command=command,
                        command_id=command_id,
                    )
                    outbound_payload = self._payload_builder.build_outbound_payload(
                        tool_command_payload
                    )
                    queued = self._coordination_store_factory(db).enqueue_outbound_message(
                        tenant_id=tenant_id,
                        runner_id=runner_id,
                        message_id=self._payload_builder.resolve_outbound_message_id(
                            runtime_job_id=runtime_job_id
                        ),
                        message_type=RunnerMessageType.TOOL_COMMAND.value,
                        payload_json=outbound_payload,
                        idempotency_key=self._payload_builder.resolve_outbound_idempotency_key(
                            runtime_job_id=runtime_job.id
                        ),
                        runtime_job_id=runtime_job.id,
                        task_id=request.task_id,
                        correlation_id=prepared_payloads.correlation_id,
                    )
                    message_id = queued.message_id

                db.commit()
                dispatch_metadata = {
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "runtime_job_id": runtime_job_id,
                    "runner_runtime_job_id": runtime_job_id,
                    "task_runtime_job_id": task_runtime_job_id,
                    "runtime_job_status": str(runtime_job.status),
                    "runner_id": str(runtime_job.runner_id) if runtime_job.runner_id else None,
                    "runner_id_assigned": str(runtime_job.runner_id) if runtime_job.runner_id else None,
                    "message_id": message_id,
                    "control_message_id": message_id,
                    "control_message_type": RunnerMessageType.TOOL_COMMAND.value,
                    "operation_id": operation_id,
                    "runtime_image": runtime_image,
                    "command_id": command_id,
                }

            wait_deadline: float | None = None
            should_wait_for_result = _should_wait_for_operation_result(request)
            if should_wait_for_result:
                wait_deadline = self._result_waiter.resolve_wait_deadline(
                    request=request,
                    timeout_seconds=prepared_payloads.timeout_seconds,
                    timeout_policy=prepared_payloads.timeout_policy,
                )

            ack_result = await self._ack_waiter.wait_for_tool_command_ack(
                request=request,
                operation_name=operation_name,
                runtime_job_id=runtime_job.id,
                metadata=dispatch_metadata,
                wait_deadline=wait_deadline,
            )
            if not should_wait_for_result:
                return ack_result
            return await self._result_waiter.wait_for_tool_command_result(
                request=request,
                operation_name=operation_name,
                runtime_job_id=runtime_job.id,
                metadata=dispatch_metadata,
                tool=tool,
                command_id=command_id,
                ack_result=ack_result,
                timeout_seconds=prepared_payloads.timeout_seconds,
                timeout_policy=prepared_payloads.timeout_policy,
                wait_deadline=wait_deadline,
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
