"""Validate Remote Runtime fixed runtime contract for startup, VPN, and diagnostics."""

import importlib
import logging
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


def test_runtime_path_defaults_to_image_internal(monkeypatch) -> None:
    monkeypatch.delenv("DROWAI_RUNTIME_PATH_MODE", raising=False)
    monkeypatch.delenv("DROWAI_RUNTIME_VALIDATION_MODE", raising=False)

    service = _build_service()
    config = service._prepare_container_config(task_id=101)
    command = config["command"][2]

    assert config["environment"]["DROWAI_RUNTIME_PATH_SOURCE"] == "image-internal"
    assert config["environment"]["PYTHONPATH"] == "/opt/drowai/runtime/python"
    assert config["working_dir"] == "/opt/drowai/runtime/python"
    assert "/opt/drowai/runtime/python/workspace_init.py" in command
    assert "/opt/drowai/runtime/python/executor_daemon.py" in command
    assert "bash /opt/drowai/runtime/vpn/vpn-manager.sh connect" in command
    assert "activation=fixed_image_internal" in command
    assert "/agent_src/" not in command


def test_legacy_runtime_toggles_no_longer_change_contract(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_RUNTIME_PATH_MODE", "legacy")
    monkeypatch.setenv("DROWAI_RUNTIME_VALIDATION_MODE", "0")

    service = _build_service()
    config = service._prepare_container_config(task_id=202)
    command = config["command"][2]
    env = config["environment"]

    assert env["DROWAI_RUNTIME_PATH_SOURCE"] == "image-internal"
    assert "DROWAI_RUNTIME_VALIDATION_ACTIVE" not in env
    assert "DROWAI_RUNTIME_ROLLBACK_REQUESTED" not in env
    assert "DROWAI_RUNTIME_ROLLBACK_ACTIVE" not in env
    assert "DROWAI_RUNTIME_FALLBACK_OR_ENFORCEMENT_USED" not in env
    assert "activation=fixed_image_internal" in command
    assert "/agent_src/" not in command


def test_vpn_connect_exec_shell_is_image_internal_even_with_legacy_request(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_RUNTIME_PATH_MODE", "legacy")

    service = _build_service()
    shell = service.build_vpn_connect_exec_shell(888)

    assert "VPN_CONFIG=/run/drowai/control/vpn/task.ovpn" in shell
    assert "bash /opt/drowai/runtime/vpn/vpn-manager.sh connect" in shell
    assert "/agent_src/scripts/vpn/vpn-manager.sh" not in shell
    assert not shell.endswith("|| true")


def test_runtime_path_diagnostics_show_fixed_activation(caplog, monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_RUNTIME_PATH_MODE", "legacy")
    monkeypatch.setenv("DROWAI_RUNTIME_VALIDATION_MODE", "0")

    service = _build_service()
    with caplog.at_level(logging.INFO, logger="backend.services.unified_docker_service"):
        config = service._prepare_container_config(task_id=404)
    command = config["command"][2]

    assert "activation=fixed_image_internal" in command
    assert any("context=container_prepare" in r.getMessage() for r in caplog.records)


def test_mount_policy_logs_are_sanitized(caplog, monkeypatch) -> None:
    """Mount-policy logs must identify policy/type without absolute workspace paths."""
    service = _build_service()
    with (
        patch("backend.services.unified_docker_service.get_workspace_path", return_value="/very/secret/workspaces/task-5150"),
        caplog.at_level(logging.INFO, logger="backend.services.unified_docker_service"),
    ):
        service._prepare_container_config(task_id=5150)

    mount_policy_logs = [record.getMessage() for record in caplog.records if "[mount-policy]" in record.getMessage()]
    assert mount_policy_logs, "expected mount-policy diagnostic log"
    assert any("effective_policy=workspace-control" in message for message in mount_policy_logs)
    assert any("workspace_mount_id=task-5150" in message for message in mount_policy_logs)
    assert any("workspace_source_type=host-local" in message for message in mount_policy_logs)
    assert all("/very/secret/workspaces/task-5150" not in message for message in mount_policy_logs)


def test_workspace_bootstrap_commands_use_image_internal_paths(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_RUNTIME_PATH_MODE", "legacy")

    service = _build_service()
    commands = service._workspace_bootstrap_commands_for_policy(77, "workspace-control")

    joined = "\n".join(commands)
    assert "/opt/drowai/runtime/python/workspaces" in joined
    assert "/agent_src/agent/workspaces" not in joined
