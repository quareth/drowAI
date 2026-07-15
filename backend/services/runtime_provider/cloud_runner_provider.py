"""Cloud runner runtime provider facade for collaborator wiring.

Responsibilities:
- Keep the public CloudRunnerRuntimeProvider class at the canonical import path.
- Wire cloud-runner operation collaborators with provider-owned dependencies.
- Delegate public provider interface methods without owning extracted bodies.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.runtime_job_service import RuntimeJobService

from .cloud_runner.constants import (
    _POD_ID,
)
from .cloud_runner.jobs.identity import RuntimeJobIdentityResolver
from .cloud_runner.jobs.queries import CloudRunnerRuntimeJobQueries
from .cloud_runner.dispatch.remote_dispatcher import CloudRunnerRemoteDispatcher
from .cloud_runner.dispatch.operation_waiter import CloudRunnerOperationWaiter
from .cloud_runner.operations.artifact import CloudRunnerArtifactOperations
from .cloud_runner.operations.environment_metadata import (
    CloudRunnerEnvironmentMetadataOperations,
)
from .cloud_runner.operations.lifecycle import CloudRunnerLifecycleOperations
from .cloud_runner.result_builders import CloudRunnerResultBuilder
from .cloud_runner.terminal.result_waiter import (
    CloudRunnerTerminalResultWaiter,
)
from .cloud_runner.terminal.operations import CloudRunnerTerminalOperations
from .cloud_runner.terminal.stream_client import CloudRunnerTerminalStreamAttacher
from .cloud_runner.tool_commands.dispatcher import CloudRunnerToolCommandDispatcher
from .cloud_runner.tool_commands.finalizer import ToolCommandResultFinalizer
from .cloud_runner.tool_commands.projection import ToolCommandResultProjector
from .cloud_runner.tool_commands.result_waiter import ToolCommandResultWaiter
from .contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)
from .provider import TaskExecutionRuntimeProvider

SessionFactory = Callable[[], Session]
RuntimeJobServiceFactory = Callable[[Session], RuntimeJobService]
CoordinationStoreFactory = Callable[[Session], DBRunnerCoordinationStore]


class CloudRunnerRuntimeProvider(TaskExecutionRuntimeProvider):
    """Cloud-backed runtime provider facade for wired operation collaborators."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        runtime_job_service_factory: RuntimeJobServiceFactory | None = None,
        coordination_store_factory: CoordinationStoreFactory | None = None,
    ) -> None:
        self._session_factory = session_factory or SessionLocal
        self._runtime_job_service_factory = runtime_job_service_factory or RuntimeJobService
        self._coordination_store_factory = coordination_store_factory or (
            lambda db: DBRunnerCoordinationStore(db, pod_id=_POD_ID)
        )
        self._result_builder = CloudRunnerResultBuilder(provider_name=self.provider_name)
        self._runtime_job_queries = CloudRunnerRuntimeJobQueries()
        self._runtime_job_identity = RuntimeJobIdentityResolver(
            runtime_job_queries=self._runtime_job_queries
        )
        self._remote_dispatcher = CloudRunnerRemoteDispatcher(
            session_factory=self._session_factory,
            runtime_job_service_factory=self._runtime_job_service_factory,
            coordination_store_factory=self._coordination_store_factory,
            runtime_job_identity=self._runtime_job_identity,
            provider_name=self.provider_name,
        )
        self._operation_waiter = CloudRunnerOperationWaiter(
            session_factory=self._session_factory,
            provider_name=self.provider_name,
        )
        self._lifecycle = CloudRunnerLifecycleOperations(
            session_factory=self._session_factory,
            remote_dispatcher=self._remote_dispatcher,
            operation_waiter=self._operation_waiter,
            result_builder=self._result_builder,
            runtime_job_queries=self._runtime_job_queries,
            provider_name=self.provider_name,
        )
        self._artifact = CloudRunnerArtifactOperations(
            remote_dispatcher=self._remote_dispatcher,
            operation_waiter=self._operation_waiter,
            result_builder=self._result_builder,
        )
        self._environment_metadata = CloudRunnerEnvironmentMetadataOperations(
            remote_dispatcher=self._remote_dispatcher,
            operation_waiter=self._operation_waiter,
            result_builder=self._result_builder,
            provider_name=self.provider_name,
        )
        terminal_streams = CloudRunnerTerminalStreamAttacher(
            session_factory=self._session_factory,
            provider_name=self.provider_name,
        )
        terminal_result_waiter = CloudRunnerTerminalResultWaiter(
            session_factory=self._session_factory,
            provider_name=self.provider_name,
        )
        self._terminal = CloudRunnerTerminalOperations(
            remote_dispatcher=self._remote_dispatcher,
            result_builder=self._result_builder,
            terminal_streams=terminal_streams,
            terminal_result_waiter=terminal_result_waiter,
            provider_name=self.provider_name,
        )
        self._tool_command_result_projector = ToolCommandResultProjector(
            provider_name=self.provider_name,
        )
        tool_command_result_waiter = ToolCommandResultWaiter(
            session_factory=self._session_factory,
            runtime_job_service_factory=self._runtime_job_service_factory,
            provider_name=self.provider_name,
            projector=self._tool_command_result_projector,
        )
        self._tool_commands = CloudRunnerToolCommandDispatcher(
            session_factory=self._session_factory,
            runtime_job_service_factory=self._runtime_job_service_factory,
            coordination_store_factory=self._coordination_store_factory,
            runtime_job_queries=self._runtime_job_queries,
            result_builder=self._result_builder,
            provider_name=self.provider_name,
            projector=self._tool_command_result_projector,
            result_waiter=tool_command_result_waiter,
        )
        self._tool_command_result_finalizer = ToolCommandResultFinalizer(
            session_factory=self._session_factory,
            result_builder=self._result_builder,
            provider_name=self.provider_name,
            projector=self._tool_command_result_projector,
        )

    @property
    def provider_name(self) -> str:
        return "cloud_runner"

    async def provision_task_runtime(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._lifecycle.provision_task_runtime(request)

    async def materialize_runtime_workspace(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._lifecycle.materialize_runtime_workspace(request)

    async def pause_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.pause_task_runtime(request)

    async def resume_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.resume_task_runtime(request)

    async def stop_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.stop_task_runtime(request)

    async def retire_task_runtime(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.retire_task_runtime(request)

    async def append_runtime_input(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.append_runtime_input(request)

    async def materialize_vpn_config(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.materialize_vpn_config(request)

    async def retry_vpn_connection(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.retry_vpn_connection(request)

    async def check_vpn_status(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.check_vpn_status(request)

    async def get_runtime_status(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.get_runtime_status(request)

    async def get_runtime_startup_progress(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._lifecycle.get_runtime_startup_progress(request)

    async def get_runtime_logs(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.get_runtime_logs(request)

    async def get_runtime_metrics(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.get_runtime_metrics(request)

    async def list_runtime_inventory(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.list_runtime_inventory(request)

    async def cleanup_runtime_workspace(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.cleanup_runtime_workspace(request)

    async def read_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._environment_metadata.read_runtime_environment_metadata(request)

    async def write_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._environment_metadata.write_runtime_environment_metadata(request)

    async def query_runtime_environment_metadata(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        return await self._environment_metadata.query_runtime_environment_metadata(request)

    async def open_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._terminal.open_terminal_session(request)

    async def send_terminal_input(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._terminal.send_terminal_input(request)

    async def read_terminal_output(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._terminal.read_terminal_output(request)

    async def resize_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._terminal.resize_terminal_session(request)

    async def close_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._terminal.close_terminal_session(request)

    async def execute_runtime_command(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.execute_runtime_command(request)

    async def dispatch_tool_execution(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._lifecycle.dispatch_tool_execution(request)

    async def read_runtime_artifact_file(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._artifact.read_runtime_artifact_file(request)

    async def promote_artifact_refs(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._artifact.promote_artifact_refs(request)

    async def finalize_tool_command_result(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        return await self._tool_command_result_finalizer.finalize_tool_command_result(request)

    async def write_runtime_artifact_file(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._artifact.write_runtime_artifact_file(request)

    async def query_runtime_artifacts(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._artifact.query_runtime_artifacts(request)

    async def send_tool_command(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return await self._tool_commands.send_tool_command(request)

    async def cancel_tool_command(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=True,
            provider=self.provider_name,
            status=RuntimeOperationStatus.ACCEPTED,
            metadata={
                "runtime_kill_attempted": False,
                "runtime_kill_supported": False,
                "process_state": "orphaned_until_terminal",
                "reason": "runner_tool_cancel_protocol_not_available",
                "command_ids": list(request.payload.get("command_ids") or []),
                "runtime_job_ids": list(request.payload.get("runtime_job_ids") or []),
            },
        )


__all__ = ["CloudRunnerRuntimeProvider"]
