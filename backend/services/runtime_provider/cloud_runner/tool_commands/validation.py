"""Tool-command validation for cloud runner dispatch.

This module owns provider-side tool.command lane, capability, secret-bearing,
identity-field, and runtime-job binding validation. It does not build outbound
payloads, wait for results, enqueue messages, or import the provider facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agent.tool_runtime.backend_tool_policy import resolve_execution_lane
from backend.models.runner_control import RuntimeJob
from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)

from ..constants import (
    _RUNNER_REQUIRED_CHANNEL_CAPABILITY,
    _RUNNER_REQUIRED_TOOL_CAPABILITY,
)
from ..error_codes import (
    _RUNNER_TOOL_COMMAND_BINDING_CONFLICT,
    _RUNNER_TOOL_COMMAND_CAPABILITY_MISSING,
    _RUNNER_TOOL_COMMAND_LANE_UNSUPPORTED,
    _RUNNER_TOOL_COMMAND_PARAMS_IDENTITY_UNSUPPORTED,
    _RUNNER_TOOL_COMMAND_PROTOCOL_CAPABILITY_MISSING,
    _RUNNER_TOOL_COMMAND_ROUTE_METADATA_REQUIRED,
    _SECRET_BEARING_ENV_UNSUPPORTED,
    _SECRET_BEARING_PARAMS_UNSUPPORTED,
)
from ..jobs.identity import _runtime_job_binding_conflicts
from ..normalization import _resolve_optional_text
from ..payload_codec import (
    _collect_forbidden_tool_command_param_identity_keys,
    _contains_secret_bearing_args,
)
from .payloads import _resolve_route_policy_metadata


class ToolCommandValidator:
    """Builds validation failures for tool.command dispatch."""

    def __init__(self, *, provider_name: str) -> None:
        self._provider_name = provider_name

    def validate_lane(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        tool: str,
    ) -> tuple[str | None, RuntimeOperationResult | None]:
        selected_lane = _resolve_selected_lane(request)
        if selected_lane is None:
            return None, build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_ROUTE_METADATA_REQUIRED,
                error_message=(
                    "send_tool_command requires authoritative lane_dispatch metadata with "
                    "a selected lane for runner tool.command dispatch."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )

        canonical_lane = resolve_execution_lane(tool)
        if selected_lane != "container_scoped" or canonical_lane != "container_scoped":
            return selected_lane, build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_LANE_UNSUPPORTED,
                error_message=(
                    "send_tool_command accepts only container_scoped tools; "
                    f"received lane `{selected_lane}` for tool `{tool}` "
                    f"classified as `{canonical_lane}`."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "selected_lane": selected_lane,
                    "canonical_lane": canonical_lane,
                    "tool": tool,
                },
            )
        return selected_lane, None

    def validate_env_payload(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        command_id: str,
        env_payload: Mapping[str, Any],
    ) -> RuntimeOperationResult | None:
        if _contains_secret_bearing_args(env_payload):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_SECRET_BEARING_ENV_UNSUPPORTED,
                error_message=(
                    "Runner tool.command rejects secret-bearing env in tooling_plane; "
                    "do not send secret material or secret references."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "command_id": command_id,
                },
            )
        return None

    def validate_params_payload(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        command_id: str,
        params_payload: Mapping[str, Any],
    ) -> RuntimeOperationResult | None:
        forbidden_param_identity_keys = _collect_forbidden_tool_command_param_identity_keys(
            params_payload
        )
        if forbidden_param_identity_keys:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_PARAMS_IDENTITY_UNSUPPORTED,
                error_message=(
                    "Runner tool.command rejects runtime identity fields in params; "
                    "provider controls runtime job identity binding."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "command_id": command_id,
                    "rejected_param_keys": sorted(forbidden_param_identity_keys),
                },
            )
        if _contains_secret_bearing_args(params_payload):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_SECRET_BEARING_PARAMS_UNSUPPORTED,
                error_message=(
                    "Runner tool.command rejects secret-bearing params in tooling_plane; "
                    "do not send secret material or secret references."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "command_id": command_id,
                },
            )
        return None

    def validate_runner_capabilities(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        capabilities_json: object,
    ) -> RuntimeOperationResult | None:
        runner_capabilities = _runner_capabilities(capabilities_json)
        if _RUNNER_REQUIRED_TOOL_CAPABILITY not in runner_capabilities:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_CAPABILITY_MISSING,
                error_message=(
                    "Runner does not advertise required capability "
                    f"`{_RUNNER_REQUIRED_TOOL_CAPABILITY}`."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        if _RUNNER_REQUIRED_CHANNEL_CAPABILITY not in runner_capabilities:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_PROTOCOL_CAPABILITY_MISSING,
                error_message=(
                    "Runner channel does not advertise required capability "
                    f"`{_RUNNER_REQUIRED_CHANNEL_CAPABILITY}`."
                ),
                metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
            )
        return None

    def validate_existing_command_binding(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job: RuntimeJob | None,
        command_id: str,
        task_runtime_job_id: str,
        runner_id: UUID,
    ) -> RuntimeOperationResult | None:
        if (
            runtime_job is not None
            and _runtime_job_binding_conflicts(
                runtime_job=runtime_job,
                command_id=command_id,
                task_runtime_job_id=task_runtime_job_id,
                workspace_id=request.workspace_id,
                runner_id=runner_id,
                task_id=request.task_id,
            )
        ):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_BINDING_CONFLICT,
                error_message=(
                    "Tool command command_id is already bound to a different "
                    "task/runtime/workspace/runner identity."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "command_id": command_id,
                },
            )
        return None

    def validate_existing_runtime_job_binding(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job: RuntimeJob,
        command_id: str,
        task_runtime_job_id: str,
        runner_id: UUID,
    ) -> RuntimeOperationResult | None:
        if _runtime_job_binding_conflicts(
            runtime_job=runtime_job,
            command_id=command_id,
            task_runtime_job_id=task_runtime_job_id,
            workspace_id=request.workspace_id,
            runner_id=runner_id,
            task_id=request.task_id,
        ):
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_TOOL_COMMAND_BINDING_CONFLICT,
                error_message=(
                    "Tool command idempotency key conflicts with a different "
                    "runtime binding."
                ),
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": operation_name,
                    "command_id": command_id,
                },
            )
        return None


def _resolve_selected_lane(request: RuntimeOperationRequest) -> str | None:
    route_policy = _resolve_route_policy_metadata(request)
    return _resolve_optional_text(route_policy.get("selected_lane"))


def _runner_capabilities(raw_value: object) -> set[str]:
    if isinstance(raw_value, list):
        return {
            str(item).strip()
            for item in raw_value
            if str(item).strip()
        }
    if isinstance(raw_value, Mapping):
        return {
            str(key).strip()
            for key, value in raw_value.items()
            if str(key).strip() and bool(value)
        }
    return set()
