"""Runner-control/remote-runtime mount-policy and runtime-path contract tests.

These tests validate that container bind mounts and runtime startup paths are
assembled through the workspace/control mount policy path.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.execution_plane_non_dind_regression


def _unified_docker_service_cls():
    """Load the compatibility Docker service class used by legacy tests."""
    module = importlib.import_module("backend.services.unified_docker_service")
    return module.UnifiedDockerService


def _build_service():
    unified_cls = _unified_docker_service_cls()
    with patch("backend.services.unified_docker_service.docker.from_env") as mock_from_env:
        mock_client = Mock()
        mock_client.ping.return_value = True
        mock_from_env.return_value = mock_client
        return unified_cls()


def test_mount_policy_has_rw_workspace_and_ro_control(monkeypatch) -> None:
    """Backend-local Docker must mount task data RW and control material RO."""
    service = _build_service()
    with (
        patch("backend.services.unified_docker_service.get_workspace_path", return_value="/host/workspaces/task-17"),
        patch(
            "backend.services.docker.container_config.WorkspaceConfig.get_task_control_path",
            return_value=Path("/host/control/task-17"),
        ),
    ):
        config = service._prepare_container_config(task_id=17)

    assert config["environment"]["DROWAI_MOUNT_POLICY"] == "workspace-control"
    assert config["volumes"] == {
        "/host/workspaces/task-17": {"bind": "/workspace", "mode": "rw"},
        "/host/control/task-17": {"bind": "/run/drowai/control", "mode": "ro"},
    }
    binds = {spec["bind"] for spec in config["volumes"].values()}
    assert "/agent_src/agent" not in binds
    assert "/agent_src/kali_executor" not in binds
    assert "/agent_src/scripts/vpn" not in binds


def test_workspace_control_uses_image_internal_runtime_paths(monkeypatch) -> None:
    """Workspace/control mounts must not use /agent_src startup/vpn paths."""
    service = _build_service()
    config = service._prepare_container_config(task_id=91)
    command = config["command"][2]

    assert config["environment"]["DROWAI_MOUNT_POLICY"] == "workspace-control"
    assert "/opt/drowai/runtime/python/workspace_init.py" in command
    assert "/opt/drowai/runtime/python/executor_daemon.py" in command
    assert "/opt/drowai/runtime/vpn/vpn-manager.sh" in command
    assert "/agent_src/agent/workspace_init.py" not in command
    assert "/agent_src/kali_executor/executor_daemon.py" not in command
    assert "/agent_src/scripts/vpn/vpn-manager.sh" not in command


def test_workspace_bootstrap_for_workspace_control_avoids_agent_src(monkeypatch) -> None:
    """Init-time workspace bootstrap stays image-internal with the two mounts."""
    service = _build_service()
    commands = service._workspace_bootstrap_commands_for_policy(55, "workspace-control")

    assert commands[0] == "mkdir -p /opt/drowai/runtime/python/workspaces"
    assert commands[1] == "ln -sf /workspace /opt/drowai/runtime/python/workspaces/task-55"
    assert "/agent_src/agent/workspaces" not in "\n".join(commands)
