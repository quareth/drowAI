"""Client-lifetime heartbeat reporting for the cloud control channel.

Owns the runner-side heartbeat concern: recovering the active job count,
building the active runtime job payloads (with the protocol truncation max),
constructing the heartbeat payload/envelope, and sending it over the websocket.

Boundary: this module only reports heartbeat state from an injected config,
runner version, and a job-store provider callable. It performs no service
composition, protocol routing, session/handler logic, and must not import
``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Callable

from drowai_runner.config import RunnerConfig
from drowai_runner.heartbeat import build_runner_heartbeat_payload
from drowai_runner.job_store import RunnerJobStore
from drowai_runner.protocol_handler import build_runner_heartbeat_envelope
from runtime_shared.runner_protocol import (
    RUNNER_ACTIVE_RUNTIME_JOBS_MAX_ITEMS,
    RunnerActiveRuntimeJobPayload,
)

from drowai_runner.control_channel.identity.models import CloudChannelIdentity


class RunnerHeartbeatReporter:
    """Builds and sends runner heartbeats from the active job store state."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        runner_version: str,
        job_store_provider: Callable[[], RunnerJobStore],
    ) -> None:
        self._config = config
        self._runner_version = runner_version
        self._job_store_provider = job_store_provider

    def count_active_jobs(self) -> int:
        job_store = self._job_store_provider()
        try:
            return len(job_store.recover_active_jobs())
        except Exception:
            return 0

    def active_runtime_jobs(self) -> tuple[RunnerActiveRuntimeJobPayload, ...]:
        job_store = self._job_store_provider()
        try:
            jobs = job_store.recover_active_jobs()
        except Exception:
            return ()
        return tuple(
            RunnerActiveRuntimeJobPayload(
                runtime_job_id=str(job.runtime_job_id),
                task_id=str(job.task_id),
                workspace_id=str(job.workspace_id),
                status=str(job.status),
            )
            for job in jobs[:RUNNER_ACTIVE_RUNTIME_JOBS_MAX_ITEMS]
        )

    def send_heartbeat(self, *, websocket, identity: CloudChannelIdentity) -> None:
        heartbeat_payload = build_runner_heartbeat_payload(
            config=self._config,
            runner_version=self._runner_version,
            active_tasks=self.count_active_jobs(),
            active_runtime_jobs=self.active_runtime_jobs(),
        )
        heartbeat = build_runner_heartbeat_envelope(
            tenant_id=identity.tenant_id,
            runner_id=identity.runner_id,
            heartbeat_payload=heartbeat_payload,
            protocol_version=identity.protocol_version,
        )
        websocket.send(heartbeat.to_json())
