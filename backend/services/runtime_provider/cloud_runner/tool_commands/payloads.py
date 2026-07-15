"""Tool-command payload builders for cloud runner dispatch.

This module owns provider-side tool.command transport payload construction and
idempotency-key formatting. It does not validate runner state, wait for
results, enqueue messages, or import the provider facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from backend.services.runtime_provider.contracts import RuntimeOperationRequest
from runtime_shared.runner_protocol import (
    RunnerMessageType,
    RunnerToolCommandPayload,
)
from runtime_shared.tool_command_transport import normalize_tool_command_transport
from runtime_shared.workspace_files import (
    normalize_runtime_workspace_directories,
    normalize_runtime_workspace_files,
    runtime_workspace_directories_to_payload,
    runtime_workspace_files_to_payload,
)

from ..dispatch.remote_dispatcher import (
    _resolve_delivery_policy,
    _resolve_operation_id,
    _resolve_runtime_image,
)
from ..normalization import (
    _coerce_non_negative_float,
    _resolve_optional_int,
    _resolve_optional_text,
)
from ..payload_codec import _prepare_transport_params


@dataclass(frozen=True)
class ToolCommandPreparedPayloads:
    """Transport payload fields resolved before runtime-job persistence."""

    workspace_files_payload: Any
    workspace_directories_payload: Any
    cwd: str
    env_payload: dict[str, str]
    params_payload: dict[str, Any]
    timeout_seconds: float
    tool_call_id: str | None
    tool_batch_id: str | None
    execution_strategy: str | None
    runtime_image: str
    correlation_id: str | None
    operation_id: str
    timeout_policy: dict[str, Any]
    route_policy: dict[str, Any]
    delivery_policy: dict[str, Any]


class ToolCommandPayloadBuilder:
    """Builds tool.command runtime-job and outbound transport payloads."""

    def build_prepared_payloads(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
    ) -> ToolCommandPreparedPayloads:
        workspace_files_payload = runtime_workspace_files_to_payload(
            normalize_runtime_workspace_files(request.payload.get("workspace_files", ()))
        )
        workspace_directories_payload = runtime_workspace_directories_to_payload(
            normalize_runtime_workspace_directories(
                request.payload.get("workspace_directories", ())
            )
        )
        cwd = _resolve_optional_text(request.payload.get("cwd")) or "/workspace"
        raw_env = request.payload.get("env")
        env_payload = (
            {str(key): str(value) for key, value in raw_env.items()}
            if isinstance(raw_env, Mapping)
            else {}
        )
        params_payload = _prepare_transport_params(
            request.payload.get("params")
            if isinstance(request.payload.get("params"), Mapping)
            else {}
        )
        transport = _resolve_tool_command_transport(request)
        if transport is not None:
            params_payload["transport"] = transport
        session_name = _resolve_optional_text(request.payload.get("session_name"))
        if session_name is not None:
            params_payload["session_name"] = session_name
        if "cleanup_session" in request.payload:
            params_payload["cleanup_session"] = bool(request.payload.get("cleanup_session"))
        artifact_stamp = _resolve_optional_int(request.payload.get("artifact_stamp"))
        if artifact_stamp is not None:
            params_payload["artifact_stamp"] = artifact_stamp

        timeout_seconds = _coerce_non_negative_float(
            request.payload.get("timeout_seconds")
            if "timeout_seconds" in request.payload
            else request.timeout_seconds,
            default=30.0,
        )
        tool_call_id = _resolve_optional_text(request.payload.get("tool_call_id"))
        tool_batch_id = _resolve_optional_text(request.payload.get("tool_batch_id"))
        execution_strategy = _resolve_optional_text(request.payload.get("execution_strategy"))
        runtime_image = _resolve_runtime_image(request=request, params=request.payload)
        correlation_id = _resolve_optional_text(
            request.metadata.get("correlation_id") or request.payload.get("correlation_id")
        )
        operation_id = _resolve_operation_id(request=request, operation_name=operation_name)
        raw_timeout_policy = (
            request.payload.get("timeout_policy")
            if isinstance(request.payload.get("timeout_policy"), Mapping)
            else {}
        )
        timeout_policy = _prepare_transport_params(raw_timeout_policy)
        route_policy = _resolve_route_policy_metadata(request)
        delivery_policy = _resolve_delivery_policy(request)
        return ToolCommandPreparedPayloads(
            workspace_files_payload=workspace_files_payload,
            workspace_directories_payload=workspace_directories_payload,
            cwd=cwd,
            env_payload=env_payload,
            params_payload=params_payload,
            timeout_seconds=timeout_seconds,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch_id,
            execution_strategy=execution_strategy,
            runtime_image=runtime_image,
            correlation_id=correlation_id,
            operation_id=operation_id,
            timeout_policy=timeout_policy,
            route_policy=route_policy,
            delivery_policy=delivery_policy,
        )

    def build_runtime_job_payload(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        prepared: ToolCommandPreparedPayloads,
        execution_site_id: UUID | None,
        tool: str,
        command: str,
        command_id: str,
        task_runtime_job_id: str,
    ) -> dict[str, Any]:
        runtime_job_payload: dict[str, Any] = {
            "operation_name": operation_name,
            "message_type": RunnerMessageType.TOOL_COMMAND.value,
            "workspace_id": request.workspace_id,
            "operation_id": prepared.operation_id,
            "runtime_image": prepared.runtime_image,
            "runtime_placement_mode": request.runtime_placement_mode.value,
            "tool": tool,
            "command": command,
            "cwd": prepared.cwd,
            "env": prepared.env_payload,
            "command_id": command_id,
            "tool_call_id": prepared.tool_call_id,
            "tool_batch_id": prepared.tool_batch_id,
            "execution_strategy": prepared.execution_strategy,
            "task_runtime_job_id": task_runtime_job_id,
            "timeout_seconds": prepared.timeout_seconds,
            "timeout_policy": prepared.timeout_policy,
            "route_policy": prepared.route_policy,
            "delivery_policy": prepared.delivery_policy,
            "params": prepared.params_payload,
            "workspace_files": prepared.workspace_files_payload,
            "workspace_directories": prepared.workspace_directories_payload,
        }
        if execution_site_id is not None:
            runtime_job_payload["execution_site_id"] = str(execution_site_id)
        return runtime_job_payload

    def build_tool_command_payload(
        self,
        *,
        request: RuntimeOperationRequest,
        prepared: ToolCommandPreparedPayloads,
        operation_id: str,
        runtime_image: str,
        tool: str,
        command: str,
        command_id: str,
        task_runtime_job_id: str,
    ) -> RunnerToolCommandPayload:
        return RunnerToolCommandPayload(
            operation_id=operation_id,
            workspace_id=request.workspace_id,
            task_runtime_job_id=task_runtime_job_id,
            runtime_image=runtime_image,
            tool=tool,
            command=command,
            cwd=prepared.cwd,
            env=prepared.env_payload,
            command_id=command_id,
            timeout_seconds=prepared.timeout_seconds,
            timeout_policy=prepared.timeout_policy,
            route_policy=prepared.route_policy,
            delivery_policy=prepared.delivery_policy,
            tool_call_id=prepared.tool_call_id,
            tool_batch_id=prepared.tool_batch_id,
            execution_strategy=prepared.execution_strategy,
            params=prepared.params_payload,
            workspace_files=tuple(
                normalize_runtime_workspace_files(prepared.workspace_files_payload)
            ),
            workspace_directories=tuple(
                normalize_runtime_workspace_directories(
                    prepared.workspace_directories_payload
                )
            ),
        )

    def build_outbound_payload(
        self,
        tool_command_payload: RunnerToolCommandPayload,
    ) -> dict[str, Any]:
        return _prepare_transport_params(asdict(tool_command_payload))

    def resolve_tool_command_idempotency_key(
        self,
        *,
        request: RuntimeOperationRequest,
        runner_id: UUID,
        task_runtime_job_id: str,
        command_id: str,
    ) -> str:
        return (
            f"tooling_plane:tool.command:tenant:{request.tenant_id}:task:{request.task_id}:"
            f"runner:{runner_id}:workspace:{request.workspace_id}:"
            f"task_runtime_job_id:{task_runtime_job_id}:command_id:{command_id}"
        )

    def resolve_outbound_message_id(self, *, runtime_job_id: str) -> str:
        return f"tooling-plane-tool-command-{runtime_job_id}-{uuid4().hex}"

    def resolve_outbound_idempotency_key(self, *, runtime_job_id: UUID) -> str:
        return f"tooling_plane:{RunnerMessageType.TOOL_COMMAND.value}:{runtime_job_id}"


def _resolve_tool_command_transport(request: RuntimeOperationRequest) -> str | None:
    candidate = request.payload.get("transport")
    raw_params = request.payload.get("params")
    if candidate is None and isinstance(raw_params, Mapping):
        candidate = raw_params.get("transport")
    return normalize_tool_command_transport(candidate)


def _resolve_route_policy_metadata(request: RuntimeOperationRequest) -> dict[str, Any]:
    lane_dispatch = request.metadata.get("lane_dispatch")
    if isinstance(lane_dispatch, Mapping):
        lane = _resolve_optional_text(lane_dispatch.get("lane"))
        authority = _resolve_optional_text(lane_dispatch.get("authority"))
        metadata: dict[str, Any] = {}
        if lane is not None:
            metadata["selected_lane"] = lane
        if authority is not None:
            metadata["selected_authority"] = authority
        return metadata
    return {}
