"""Provider interface for task execution runtime operations.

Responsibilities:
- Define the runtime-provider boundary used by task/runtime orchestration services.
- Keep management-plane code independent from local Docker/runtime implementation details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .contracts import RuntimeOperationRequest, RuntimeOperationResult


class TaskExecutionRuntimeProvider(ABC):
    """Abstract runtime provider for task-scoped execution operations."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the stable provider identifier."""

    @abstractmethod
    async def provision_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Provision or start runtime resources for a task."""

    @abstractmethod
    async def materialize_runtime_workspace(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Materialize runtime workspace state from `workspace_id`."""

    @abstractmethod
    async def pause_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Pause a running task runtime."""

    @abstractmethod
    async def resume_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Resume a paused task runtime."""

    @abstractmethod
    async def stop_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Stop task runtime execution without full retirement."""

    @abstractmethod
    async def retire_task_runtime(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Retire runtime resources and cleanup provider-owned runtime state."""

    @abstractmethod
    async def append_runtime_input(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Append runtime input for a waiting task runtime."""

    @abstractmethod
    async def materialize_vpn_config(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Materialize VPN config needed by task runtime."""

    @abstractmethod
    async def retry_vpn_connection(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Retry VPN connection from provider-owned runtime context."""

    @abstractmethod
    async def check_vpn_status(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Read runtime VPN status."""

    @abstractmethod
    async def get_runtime_status(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Return current runtime status details."""

    @abstractmethod
    async def get_runtime_startup_progress(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Return runtime startup/provisioning progress details."""

    @abstractmethod
    async def get_runtime_logs(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Return runtime log snapshot or stream metadata."""

    @abstractmethod
    async def get_runtime_metrics(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Return runtime metrics snapshot."""

    @abstractmethod
    async def list_runtime_inventory(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Return authorized runtime inventory for compatibility surfaces."""

    @abstractmethod
    async def cleanup_runtime_workspace(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Clean up provider-owned runtime workspace state."""

    @abstractmethod
    async def read_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Read environment metadata from provider-owned runtime state."""

    @abstractmethod
    async def write_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Write environment metadata to provider-owned runtime state."""

    @abstractmethod
    async def query_runtime_environment_metadata(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Query runtime environment metadata through provider boundary."""

    @abstractmethod
    async def open_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Open runtime terminal session."""

    @abstractmethod
    async def send_terminal_input(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Send terminal input to an active runtime session."""

    @abstractmethod
    async def read_terminal_output(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Read terminal output from an active runtime session."""

    @abstractmethod
    async def resize_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Resize an active runtime terminal session."""

    @abstractmethod
    async def close_terminal_session(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Close an active runtime terminal session."""

    @abstractmethod
    async def execute_runtime_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Execute a task-scoped runtime command (REST command surface)."""

    @abstractmethod
    async def dispatch_tool_execution(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Compatibility dispatch surface for task-scoped tool execution."""

    @abstractmethod
    async def read_runtime_artifact_file(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Read runtime-produced artifact files through provider boundary."""

    @abstractmethod
    async def promote_artifact_refs(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Declare canonical artifact refs on the runner data plane."""

    @abstractmethod
    async def finalize_tool_command_result(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Persist the control-plane canonical tool verdict for a completed process."""

    @abstractmethod
    async def write_runtime_artifact_file(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Write one backend-materialized workspace file through provider boundary."""

    @abstractmethod
    async def query_runtime_artifacts(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Query runtime-produced artifacts through provider boundary."""

    @abstractmethod
    async def send_tool_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Dispatch one structured runner tool command through provider transport."""

    @abstractmethod
    async def cancel_tool_command(
        self, request: RuntimeOperationRequest
    ) -> RuntimeOperationResult:
        """Request best-effort cancellation for active tool command processes."""


__all__ = ["TaskExecutionRuntimeProvider"]
