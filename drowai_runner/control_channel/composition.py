"""Client-lifetime runner service composition for the cloud control channel.

Owns lazy construction and caching of the runner-side runtime collaborators used
by the cloud client: the runtime job store and the operation service together
with its docker runtime, logs/metrics adapter, terminal proxy, and cleanup
service.

Boundary: this module only constructs and caches these collaborators from an
injected config, workspace manager, and docker client factory. It performs no
websocket I/O, protocol handling, heartbeat, or session/handler logic, and must
not import ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Callable

from drowai_runner.cleanup import RunnerCleanupService
from drowai_runner.config import RunnerConfig
from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.job_store import RunnerJobStore, initialize_runner_job_store
from drowai_runner.logs_metrics import RunnerLogsMetricsAdapter
from drowai_runner.operation_service import RunnerOperationService
from drowai_runner.terminal_proxy import RunnerTerminalProxy
from drowai_runner.workspace import RunnerWorkspaceManager

from drowai_runner.control_channel.terminal.pty_adapter import _RunnerPtyAdapter


class RunnerControlChannelComposition:
    """Lazily constructs and caches client-lifetime runner runtime collaborators."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        workspace_manager: RunnerWorkspaceManager,
        docker_client_factory: Callable[[], object],
    ) -> None:
        self._config = config
        self._workspace_manager = workspace_manager
        self._docker_client_factory = docker_client_factory
        self._operation_service: RunnerOperationService | None = None
        self._job_store: RunnerJobStore | None = None

    def job_store(self) -> RunnerJobStore:
        existing = self._job_store
        if existing is not None:
            return existing
        store = initialize_runner_job_store(self._config.runner_root / "jobs.sqlite")
        self._job_store = store
        return store

    def operation_service(self) -> RunnerOperationService:
        existing = self._operation_service
        if existing is not None:
            return existing
        workspace = self._workspace_manager
        job_store = self.job_store()
        docker_runtime = RunnerDockerRuntime(client_factory=self._docker_client_factory)
        logs_metrics = RunnerLogsMetricsAdapter(
            job_store=job_store,
            docker_runtime=docker_runtime,
            workspace_manager=workspace,
        )
        cleanup = RunnerCleanupService(
            workspace_manager=workspace,
            job_store=job_store,
            remove_container=lambda container_id: docker_runtime.remove_container(
                container_id,
                force=True,
            ),
            cleanup_retention_hours=self._config.cleanup_retention_hours,
            remove_orphan_network=docker_runtime.remove_orphan_task_network,
        )
        self._operation_service = RunnerOperationService(
            config=self._config,
            workspace=workspace,
            job_store=job_store,
            docker_runtime=docker_runtime,
            logs_metrics=logs_metrics,
            terminal_proxy=RunnerTerminalProxy(
                job_store=job_store,
                pty_adapter=_RunnerPtyAdapter(docker_runtime=docker_runtime),
            ),
            cleanup=cleanup,
        )
        return self._operation_service
