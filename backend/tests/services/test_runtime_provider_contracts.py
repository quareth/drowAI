"""Contract tests for the runtime provider boundary."""

from __future__ import annotations

import json

from backend.services.runtime_provider import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    TaskExecutionRuntimeProvider,
    build_runtime_result,
    is_pending_runner_operation_result,
    is_runner_assignment_probe_result,
)
from backend.services.runtime_provider.terminal_stream_contract import (
    is_push_terminal_stream,
    terminal_stream_from_payload,
)


class _StubRuntimeProvider(TaskExecutionRuntimeProvider):
    """Concrete implementation used to verify abstract contract shape."""

    @property
    def provider_name(self) -> str:
        return "stub"

    async def provision_task_runtime(self, request: RuntimeOperationRequest):
        return build_runtime_result(
            request,
            accepted=True,
            provider=self.provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
        )

    async def materialize_runtime_workspace(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def pause_task_runtime(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def resume_task_runtime(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def stop_task_runtime(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def retire_task_runtime(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def append_runtime_input(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def materialize_vpn_config(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def retry_vpn_connection(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def check_vpn_status(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def get_runtime_status(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def get_runtime_startup_progress(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def get_runtime_logs(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def get_runtime_metrics(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def list_runtime_inventory(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def cleanup_runtime_workspace(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def read_runtime_environment_metadata(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def write_runtime_environment_metadata(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def query_runtime_environment_metadata(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def open_terminal_session(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def send_terminal_input(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def read_terminal_output(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def resize_terminal_session(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def close_terminal_session(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def execute_runtime_command(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def dispatch_tool_execution(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def read_runtime_artifact_file(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def promote_artifact_refs(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def finalize_tool_command_result(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def write_runtime_artifact_file(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def query_runtime_artifacts(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def send_tool_command(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)

    async def cancel_tool_command(self, request: RuntimeOperationRequest):
        return await self.provision_task_runtime(request)


def test_runtime_request_and_result_include_identity_fields():
    """Request/result envelopes keep tenant/task/actor/placement identity."""
    request = RuntimeOperationRequest(
        tenant_id="tenant-local",
        task_id=42,
        user_id=7,
        actor_type=RuntimeActorType.USER,
        actor_id=7,
        runtime_placement_mode=RuntimePlacementMode.LOCAL,
        workspace_id="task-42",
        runner_id=None,
        execution_site_id=None,
        operation="get_runtime_status",
        timeout_seconds=15.0,
        metadata={"source": "test"},
        payload={"verbose": True},
    )

    result = build_runtime_result(
        request,
        accepted=True,
        provider="local_docker",
        status=RuntimeOperationStatus.SUCCEEDED,
        metadata={"state": "running"},
    )

    assert result.tenant_id == request.tenant_id
    assert result.task_id == request.task_id
    assert result.user_id == request.user_id
    assert result.actor_type == request.actor_type
    assert result.actor_id == request.actor_id
    assert result.runtime_placement_mode == request.runtime_placement_mode
    assert result.workspace_id == request.workspace_id
    assert result.runner_id == request.runner_id
    assert result.execution_site_id == request.execution_site_id
    assert result.accepted is True
    assert result.provider == "local_docker"
    assert result.operation == request.operation
    assert result.status == RuntimeOperationStatus.SUCCEEDED
    assert result.ok is True
    assert request.runtime_call_scope is RuntimeCallScope.PRODUCT_TASK
    assert request.metadata["runtime_call_scope"] == "product_task"
    json.dumps(request.metadata)


def test_runtime_request_accepts_explicit_serializable_scope():
    """Explicit non-product scopes are normalized and copied as string metadata."""
    request = RuntimeOperationRequest(
        tenant_id="tenant-local",
        task_id=42,
        user_id=7,
        actor_type=RuntimeActorType.USER,
        actor_id=7,
        runtime_placement_mode=RuntimePlacementMode.LOCAL,
        workspace_id="task-42",
        runner_id=None,
        execution_site_id=None,
        operation="list_runtime_inventory",
        runtime_call_scope="diagnostic",
        metadata={"runtime_call_scope": RuntimeCallScope.TEST},
    )
    copied = request.with_payload(verbose=True)

    assert request.runtime_call_scope is RuntimeCallScope.DIAGNOSTIC
    assert request.metadata["runtime_call_scope"] == "diagnostic"
    assert copied.runtime_call_scope is RuntimeCallScope.DIAGNOSTIC
    assert copied.metadata["runtime_call_scope"] == "diagnostic"
    json.dumps(copied.metadata)


def test_runtime_request_rejects_unknown_scope():
    """Unknown runtime scopes fail closed at request construction."""
    try:
        RuntimeOperationRequest(
            tenant_id="tenant-local",
            task_id=42,
            user_id=7,
            actor_type=RuntimeActorType.USER,
            actor_id=7,
            runtime_placement_mode=RuntimePlacementMode.LOCAL,
            workspace_id="task-42",
            operation="get_runtime_status",
            runtime_call_scope="unknown",
        )
    except ValueError as exc:
        assert "Unsupported runtime call scope" in str(exc)
    else:
        raise AssertionError("unknown runtime call scope should fail closed")


def test_provider_interface_conformance_can_be_instantiated():
    """A provider must implement all abstract operations in the boundary."""
    provider = _StubRuntimeProvider()
    assert provider.provider_name == "stub"


def test_runner_pending_and_runner_control_probe_helpers_ignore_provider_branding():
    request = RuntimeOperationRequest(
        tenant_id=1,
        task_id=9,
        user_id=2,
        actor_type=RuntimeActorType.SYSTEM,
        actor_id="system",
        runtime_placement_mode=RuntimePlacementMode.RUNNER,
        workspace_id="task-9",
        operation="provision_task_runtime",
    )
    pending_result = build_runtime_result(
        request,
        accepted=True,
        provider="managed_runner",
        status=RuntimeOperationStatus.RUNNING,
        metadata={"protocol_domain": "remote_runtime"},
    )
    probe_result = build_runtime_result(
        request,
        accepted=True,
        provider="runner_control",
        status=RuntimeOperationStatus.ACCEPTED,
        metadata={"assignment_probe": True},
    )

    assert is_pending_runner_operation_result(pending_result) is True
    assert is_runner_assignment_probe_result(probe_result) is True


def test_terminal_stream_contract_detects_shared_provider_stream_shape():
    """Provider stream detection should stay centralized across runner providers."""

    class _StreamClient:
        session_id = "sess-1"
        push_frames = True

        async def send_input(self, data):
            del data

        async def read_output(self, size=4096, timeout=None):
            del size, timeout
            return b""

        async def resize(self, cols, rows):
            del cols, rows

        async def close(self):
            return None

    stream = _StreamClient()
    payload = {"socket": stream}

    assert terminal_stream_from_payload(payload) is stream
    assert is_push_terminal_stream(stream) is True
