"""Tests for runner-owned runtime environment-info collection."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.environment import (
    collect_and_save_runner_environment_info,
    load_runner_environment_info,
)
from drowai_runner.workspace import RunnerWorkspaceManager


class _Probe:
    def __init__(self, *, exit_code: int, stdout: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = ""


class _FakeDockerRuntime:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, list[str], int]] = []

    def exec_probe(self, container_id: str, command: list[str], *, timeout_seconds: int = 10) -> _Probe:
        self.calls.append((container_id, command, timeout_seconds))
        command_text = command[-1]
        if command_text == "cat /etc/resolv.conf":
            return _Probe(exit_code=1, stdout="")
        return _Probe(exit_code=0, stdout=self.outputs.get(command_text, ""))


def test_collect_and_save_runner_environment_info_persists_canonical_shape(tmp_path: Path) -> None:
    workspace_manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace_manager.initialize_task_workspace("task-42")
    docker_runtime = _FakeDockerRuntime(
        {
            "hostname": "runner-task",
            "cat /etc/os-release": 'PRETTY_NAME="Kali"\nVERSION_ID="2026.1"',
            "uname -r": "6.12-kali",
            "ip addr show": "2: eth0: <UP>\n    inet 10.0.0.5/24",
            "ip route": "default via 10.0.0.1 dev eth0",
        }
    )

    env_info = collect_and_save_runner_environment_info(
        docker_runtime=docker_runtime,  # type: ignore[arg-type]
        workspace_manager=workspace_manager,
        container_id="cid-42",
        workspace_id="task-42",
    )
    loaded = load_runner_environment_info(
        workspace_manager=workspace_manager,
        workspace_id="task-42",
    )

    assert env_info["hostname"] == "runner-task"
    assert env_info["network"]["default_gateway"] == "10.0.0.1"
    assert env_info["collection_errors"] == ["Failed to read /etc/resolv.conf"]
    assert loaded == env_info
    assert all(call[0] == "cid-42" for call in docker_runtime.calls)
