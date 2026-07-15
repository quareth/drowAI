"""Tests for managed control-plane runner configuration and log masking."""

from __future__ import annotations

import pytest

from drowai_runner.app import _masked_runner_log
from drowai_runner.config import RunnerConfig


def test_runner_config_preserves_managed_defaults() -> None:
    config = RunnerConfig.from_env({})

    assert config.cloud_base_url is None
    assert config.registration_token is None
    assert config.credential_secret_path is None
    assert config.heartbeat_interval_seconds == 30
    assert config.tls_verify is True
    assert config.labels == {}
    assert config.capabilities == ()


def test_control_plane_url_alias_takes_precedence_over_cloud_alias() -> None:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_CONTROL_PLANE_URL": "https://control.example.com",
            "DROWAI_RUNNER_CLOUD_BASE_URL": "https://cloud.example.com",
        }
    )

    assert config.cloud_base_url == "https://control.example.com"


def test_control_plane_url_rejects_non_tls_endpoint_without_dev_override() -> None:
    with pytest.raises(ValueError, match="must use https://"):
        RunnerConfig.from_env(
            {
                "DROWAI_RUNNER_CONTROL_PLANE_URL": "http://localhost:8080",
                "DROWAI_RUNNER_REGISTRATION_TOKEN": "token-1",
            }
        )


def test_control_plane_url_allows_non_tls_endpoint_with_dev_override() -> None:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_CONTROL_PLANE_URL": "http://localhost:8080/",
            "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT": "true",
            "DROWAI_RUNNER_REGISTRATION_TOKEN": "token-1",
            "DROWAI_RUNNER_LABELS": '{"site":"hq"}',
            "DROWAI_RUNNER_CAPABILITIES": '["docker","file_comm"]',
        }
    )

    assert config.cloud_base_url == "http://localhost:8080"
    assert config.allow_insecure_cloud_endpoint is True
    assert config.labels == {"site": "hq"}
    assert config.capabilities == ("docker", "file_comm")


def test_managed_mode_accepts_valid_credential_secret_path() -> None:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_CONTROL_PLANE_URL": "https://cloud.example.com",
            "DROWAI_RUNNER_CREDENTIAL_SECRET_PATH": "credentials/runner.secret",
        }
    )

    assert config.registration_token is None
    assert config.credential_secret_path is not None
    assert config.credential_secret_path.name == "runner.secret"


@pytest.mark.parametrize(
    "secret_path",
    [
        "/",
        "../outside.secret",
    ],
)
def test_cloud_mode_rejects_unsafe_credential_secret_path(secret_path: str) -> None:
    with pytest.raises(ValueError, match="credential_secret_path"):
        RunnerConfig.from_env(
            {
                "DROWAI_RUNNER_CONTROL_PLANE_URL": "https://cloud.example.com",
                "DROWAI_RUNNER_CREDENTIAL_SECRET_PATH": secret_path,
            }
        )


def test_masked_runner_log_hides_registration_token_value() -> None:
    config = RunnerConfig.from_env(
        {
            "DROWAI_RUNNER_CONTROL_PLANE_URL": "https://cloud.example.com",
            "DROWAI_RUNNER_REGISTRATION_TOKEN": "runner-token-secret",
            "DROWAI_RUNNER_CREDENTIAL_SECRET_PATH": "credentials/runner.secret",
        }
    )

    line = _masked_runner_log(config)

    assert "runner-token-secret" not in line
    assert "registration_token=<KEY_SET>" in line
    assert "control_plane_url=https://cloud.example.com" in line
