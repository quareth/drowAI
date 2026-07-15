"""Tests for Runtime Path Consolidation deployment-profile validation.

Responsibilities:
- Verify deployment-profile parsing.
- Ensure single-host and distributed product profiles share one runtime path.
- Keep validation errors pinned to exact unsafe or missing environment keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.generated_config import (
    CLOUD_RUNNER_CONTROL_ENABLED_ENV,
    CONFIG_DIR_ENV,
    DATA_PLANE_OBJECT_STORE_BACKEND_ENV,
    DEPLOYMENT_PROFILE_ENV,
    RUNNER_TOOL_COMMAND_ENABLED_ENV,
    SECRETS_DIR_ENV,
    TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
    GeneratedConfigPaths,
    bootstrap_generated_config,
    read_backend_env,
    write_backend_env,
)
from backend.config.deployment_topology import (
    DeploymentProfile,
    DeploymentProfileValidationError,
    get_deployment_profile_state,
)


def _set_product_profile_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str,
) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", profile)
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "runner")
    monkeypatch.setenv("ENABLE_CLOUD_RUNNER_CONTROL", "true")
    monkeypatch.setenv("RUNNER_TOOL_COMMAND_ENABLED", "true")


def _generated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GeneratedConfigPaths:
    paths = GeneratedConfigPaths(
        config_dir=tmp_path / "config",
        secrets_dir=tmp_path / "secrets",
    )
    monkeypatch.setenv(CONFIG_DIR_ENV, str(paths.config_dir))
    monkeypatch.setenv(SECRETS_DIR_ENV, str(paths.secrets_dir))
    return paths


def _clear_product_policy_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        DEPLOYMENT_PROFILE_ENV,
        TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
        CLOUD_RUNNER_CONTROL_ENABLED_ENV,
        RUNNER_TOOL_COMMAND_ENABLED_ENV,
        DATA_PLANE_OBJECT_STORE_BACKEND_ENV,
        "DATA_PLANE_OBJECT_STORE_BUCKET",
        "DROWAI_RUNNER_REGISTRATION_TOKEN",
        "DROWAI_RUNNER_ENROLLMENT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_dev_local_profile_allows_local_runtime_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    monkeypatch.setenv("TASK_RUNTIME_PLACEMENT_MODE_DEFAULT", "local")

    profile = get_deployment_profile_state()

    assert profile.profile is DeploymentProfile.DEV_LOCAL
    assert profile.runtime_placement_mode == "local"


@pytest.mark.parametrize("deployment_profile", ("single_host", "distributed"))
def test_generated_config_bootstrap_writes_product_runner_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    paths = _generated_paths(tmp_path, monkeypatch)
    _clear_product_policy_process_env(monkeypatch)

    env = bootstrap_generated_config(
        profile=deployment_profile,
        docker=False,
        paths=paths,
        postgres_host="postgres",
    )
    file_env = read_backend_env(paths.backend_env_path)

    for source in (env, file_env):
        assert source[DEPLOYMENT_PROFILE_ENV] == deployment_profile
        assert source[TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV] == "runner"
        assert source[CLOUD_RUNNER_CONTROL_ENABLED_ENV] == "true"
        assert source[RUNNER_TOOL_COMMAND_ENABLED_ENV] == "true"
        assert source[DATA_PLANE_OBJECT_STORE_BACKEND_ENV] == "local"
        assert "DROWAI_RUNNER_REGISTRATION_TOKEN" not in source
        assert "DROWAI_RUNNER_ENROLLMENT_TOKEN" not in source


@pytest.mark.parametrize(
    ("deployment_profile", "expected_profile"),
    (
        ("single_host", DeploymentProfile.SINGLE_HOST),
        ("distributed", DeploymentProfile.DISTRIBUTED),
    ),
)
def test_product_profile_validation_uses_generated_config_without_user_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
    expected_profile: DeploymentProfile,
) -> None:
    paths = _generated_paths(tmp_path, monkeypatch)
    _clear_product_policy_process_env(monkeypatch)
    bootstrap_generated_config(
        profile=deployment_profile,
        docker=False,
        paths=paths,
        postgres_host="postgres",
    )
    _clear_product_policy_process_env(monkeypatch)

    profile = get_deployment_profile_state()

    assert profile.profile is expected_profile
    assert profile.runtime_placement_mode == "runner"
    assert profile.cloud_runner_control_enabled is True
    assert profile.runner_tool_command_enabled is True
    assert profile.object_store_backend == "local"


@pytest.mark.parametrize("deployment_profile", ("single_host", "distributed"))
def test_product_profile_validation_fails_when_generated_policy_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    paths = _generated_paths(tmp_path, monkeypatch)
    _clear_product_policy_process_env(monkeypatch)
    write_backend_env(
        {DEPLOYMENT_PROFILE_ENV: deployment_profile},
        path=paths.backend_env_path,
    )

    with pytest.raises(
        DeploymentProfileValidationError,
        match="TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
    ):
        get_deployment_profile_state()


@pytest.mark.parametrize(
    ("deployment_profile", "name", "value", "expected"),
    (
        (
            "single_host",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "local",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
        ),
        (
            "distributed",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "local",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
        ),
    ),
)
def test_product_profiles_reject_unsafe_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
    name: str,
    value: str,
    expected: str,
) -> None:
    _set_product_profile_env(monkeypatch, profile=deployment_profile)
    monkeypatch.setenv(name, value)

    with pytest.raises(DeploymentProfileValidationError, match=expected):
        get_deployment_profile_state()


def test_single_host_and_distributed_profiles_share_same_runtime_path_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for profile_name, expected in (
        ("single_host", DeploymentProfile.SINGLE_HOST),
        ("distributed", DeploymentProfile.DISTRIBUTED),
    ):
        _set_product_profile_env(monkeypatch, profile=profile_name)
        monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "s3")
        monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BUCKET", "prod-bucket")

        profile = get_deployment_profile_state()

        assert profile.profile is expected
        assert profile.runtime_placement_mode == "runner"


@pytest.mark.parametrize("deployment_profile", ("single_host", "distributed"))
def test_product_profiles_require_non_local_object_store_bucket_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    _set_product_profile_env(monkeypatch, profile=deployment_profile)
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BUCKET", "prod-bucket")

    profile = get_deployment_profile_state()

    assert profile.profile in {DeploymentProfile.SINGLE_HOST, DeploymentProfile.DISTRIBUTED}
    assert profile.object_store_backend_non_local is True
    assert profile.object_store_bucket_configured is True


@pytest.mark.parametrize(
    ("deployment_profile", "name", "value", "expected"),
    (
        (
            "single_host",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "local",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
        ),
        (
            "single_host",
            "ENABLE_CLOUD_RUNNER_CONTROL",
            "false",
            "ENABLE_CLOUD_RUNNER_CONTROL=true",
        ),
        (
            "single_host",
            "RUNNER_TOOL_COMMAND_ENABLED",
            "false",
            "RUNNER_TOOL_COMMAND_ENABLED=true",
        ),
        (
            "distributed",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "local",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner",
        ),
        (
            "distributed",
            "ENABLE_CLOUD_RUNNER_CONTROL",
            "false",
            "ENABLE_CLOUD_RUNNER_CONTROL=true",
        ),
        (
            "distributed",
            "RUNNER_TOOL_COMMAND_ENABLED",
            "false",
            "RUNNER_TOOL_COMMAND_ENABLED=true",
        ),
    ),
)
def test_product_profiles_reject_missing_required_flags(
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
    name: str,
    value: str,
    expected: str,
) -> None:
    _set_product_profile_env(monkeypatch, profile=deployment_profile)
    monkeypatch.setenv(name, value)

    with pytest.raises(DeploymentProfileValidationError, match=expected):
        get_deployment_profile_state()


@pytest.mark.parametrize("deployment_profile", ("single_host", "distributed"))
def test_product_profiles_reject_non_local_object_store_without_bucket(
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    _set_product_profile_env(monkeypatch, profile=deployment_profile)
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.delenv("DATA_PLANE_OBJECT_STORE_BUCKET", raising=False)

    with pytest.raises(DeploymentProfileValidationError, match="DATA_PLANE_OBJECT_STORE_BUCKET"):
        get_deployment_profile_state()
