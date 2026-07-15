"""Tests for managed runner configuration defaults and safety checks.

These checks lock the managed runner config contract to backend-independent
defaults and control-plane validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drowai_runner.config import RunnerConfig
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine


def test_runner_config_loads_safe_defaults_without_backend_settings() -> None:
    config = RunnerConfig.from_env({})

    assert str(config.runner_root) == "/var/lib/drowai"
    assert config.runtime_image_tag == default_runtime_image_for_machine()
    assert config.max_active_tasks == 2
    assert config.max_parallel_commands_per_task == 4


def test_runner_config_dev_root_is_absolute_for_docker_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = RunnerConfig.from_env({"DROWAI_RUNNER_DEV_MODE": "1"})

    assert config.runner_root == tmp_path / ".drowai-runner"
    assert config.runner_root.is_absolute()


def test_runner_config_relative_explicit_root_is_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = RunnerConfig.from_env({"DROWAI_RUNNER_ROOT": "runner-data"})

    assert config.runner_root == tmp_path / "runner-data"
    assert config.runner_root.is_absolute()


def test_runner_config_loads_toml_runner_table(tmp_path: Path) -> None:
    config_path = tmp_path / "runner.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                'runner_root = "/tmp/drowai-runner"',
                'runtime_image_tag = "custom/runtime:dev"',
                'docker_endpoint_mode = "local"',
                "max_active_tasks = 3",
                "max_parallel_commands_per_task = 2",
                "cleanup_retention_hours = 12",
                'log_level = "debug"',
                "tenant_id = 42",
            ]
        ),
        encoding="utf-8",
    )

    config = RunnerConfig.from_toml(config_path)

    assert str(config.runner_root) == "/tmp/drowai-runner"
    assert config.runtime_image_tag == "custom/runtime:dev"
    assert config.max_active_tasks == 3
    assert config.log_level == "DEBUG"
    assert config.tenant_id == 42


def test_runner_config_loads_enrollment_toml_without_tenant_id(tmp_path: Path) -> None:
    config_path = tmp_path / "enrollment.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runner]",
                f'runner_root = "{tmp_path / "runner-root"}"',
                'control_plane_url = "http://management.local:8000"',
                'registration_token = "rit_enrollment_token"',
                "allow_insecure_cloud_endpoint = true",
            ]
        ),
        encoding="utf-8",
    )

    config = RunnerConfig.from_toml(config_path)

    assert config.cloud_base_url == "http://management.local:8000"
    assert config.registration_token == "rit_enrollment_token"
    assert config.tenant_id is None
