"""Runner-owned collection and storage of task runtime environment info.

This module keeps Kali container environment discovery inside the customer
runner execution plane while sharing the canonical environment-info shape with
backend prompt formatting through `runtime_shared.environment_info`.
"""

from __future__ import annotations

import json
from typing import Any

from drowai_runner.docker_runtime import RunnerDockerRuntime
from drowai_runner.workspace import RunnerWorkspaceManager
from runtime_shared.environment_info import (
    ENV_INFO_FILENAME,
    collect_environment_info_from_executor,
)

COLLECTION_TIMEOUT_SECONDS = 10


def collect_runner_environment_info(
    *,
    docker_runtime: RunnerDockerRuntime,
    container_id: str,
) -> dict[str, Any]:
    """Collect canonical environment info from a runner-owned container."""

    def _execute(command: str) -> str:
        probe = docker_runtime.exec_probe(
            container_id,
            ["/bin/bash", "-lc", command],
            timeout_seconds=COLLECTION_TIMEOUT_SECONDS,
        )
        if int(probe.exit_code) != 0:
            return ""
        return str(probe.stdout or "")

    return collect_environment_info_from_executor(_execute)


def save_runner_environment_info(
    *,
    workspace_manager: RunnerWorkspaceManager,
    workspace_id: str,
    env_info: dict[str, Any],
) -> None:
    """Persist environment info inside a runner task workspace."""
    workspace_manager.initialize_task_workspace(workspace_id)
    encoded = (json.dumps(dict(env_info), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    workspace_manager.write_workspace_bytes(
        workspace_id, ENV_INFO_FILENAME, encoded, mode=0o600
    )


def load_runner_environment_info(
    *,
    workspace_manager: RunnerWorkspaceManager,
    workspace_id: str,
) -> dict[str, Any] | None:
    """Load cached runner environment info, returning None when unavailable."""
    try:
        payload = json.loads(
            workspace_manager.read_workspace_bytes(
                workspace_id, ENV_INFO_FILENAME, max_bytes=1024 * 1024
            ).decode("utf-8")
        )
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def collect_and_save_runner_environment_info(
    *,
    docker_runtime: RunnerDockerRuntime,
    workspace_manager: RunnerWorkspaceManager,
    container_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Collect and cache runner-owned environment info for one task runtime."""
    env_info = collect_runner_environment_info(
        docker_runtime=docker_runtime,
        container_id=container_id,
    )
    save_runner_environment_info(
        workspace_manager=workspace_manager,
        workspace_id=workspace_id,
        env_info=env_info,
    )
    return env_info


__all__ = [
    "COLLECTION_TIMEOUT_SECONDS",
    "collect_and_save_runner_environment_info",
    "collect_runner_environment_info",
    "load_runner_environment_info",
    "save_runner_environment_info",
]
