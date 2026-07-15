"""Runner heartbeat payload helpers for cloud control-channel sessions.

This module composes heartbeat payloads from local runner config and
self-observed runtime availability so cloud mode can emit consistent
`runner.heartbeat` messages.
"""

from __future__ import annotations

from typing import Callable, Sequence

from drowai_runner.capacity import build_runner_capacity_payload
from drowai_runner.config import RunnerConfig
from runtime_shared.runner_protocol import RunnerActiveRuntimeJobPayload, RunnerHeartbeatPayload


RuntimeImageAvailabilityProbe = Callable[[str], bool]


def build_runner_heartbeat_payload(
    *,
    config: RunnerConfig,
    runner_version: str,
    active_tasks: int,
    active_runtime_jobs: Sequence[RunnerActiveRuntimeJobPayload] = (),
    runtime_image_available_probe: RuntimeImageAvailabilityProbe | None = None,
) -> RunnerHeartbeatPayload:
    """Return a schema-compatible heartbeat payload snapshot for cloud mode."""
    docker_available = _docker_daemon_available()
    runtime_image_available = _resolve_runtime_image_available(
        docker_available=docker_available,
        runtime_image=config.runtime_image_tag,
        probe=runtime_image_available_probe,
    )
    return RunnerHeartbeatPayload(
        capacity=build_runner_capacity_payload(
            config=config,
            runner_version=runner_version,
            active_tasks=active_tasks,
            docker_available=docker_available,
            runtime_image_available=runtime_image_available,
            active_runtime_jobs=active_runtime_jobs,
        )
    )


def _resolve_runtime_image_available(
    *,
    docker_available: bool,
    runtime_image: str,
    probe: RuntimeImageAvailabilityProbe | None,
) -> bool:
    if not docker_available:
        return False
    if probe is None:
        return True
    runtime_image_value = (runtime_image or "").strip()
    if not runtime_image_value:
        return False
    try:
        return bool(probe(runtime_image_value))
    except Exception:
        return False


def _docker_daemon_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True
