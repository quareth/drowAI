"""Tests for generated deployment config bootstrapping and resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.generated_config import (
    CLOUD_RUNNER_CONTROL_ENABLED_ENV,
    DATABASE_URL_ENV,
    DEPLOYMENT_PROFILE_ENV,
    ENCRYPTION_KEY_ENV,
    JWT_SECRET_ENV,
    POSTGRES_HOST_ENV,
    POSTGRES_PASSWORD_ENV,
    RUNNER_TOOL_COMMAND_ENABLED_ENV,
    TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
    GeneratedConfigPaths,
    bootstrap_generated_config,
    read_backend_env,
    resolve_config_value,
    resolved_backend_env,
    validate_encryption_key,
)


@pytest.fixture
def generated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GeneratedConfigPaths:
    paths = GeneratedConfigPaths(
        config_dir=tmp_path / "config",
        secrets_dir=tmp_path / "secrets",
    )
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(paths.config_dir))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(paths.secrets_dir))
    for key in (DATABASE_URL_ENV, POSTGRES_HOST_ENV, POSTGRES_PASSWORD_ENV, JWT_SECRET_ENV, ENCRYPTION_KEY_ENV):
        monkeypatch.delenv(key, raising=False)
    return paths


def test_bootstrap_generated_config_is_idempotent(generated_paths: GeneratedConfigPaths) -> None:
    first = bootstrap_generated_config(
        profile="single_host",
        docker=False,
        paths=generated_paths,
        postgres_host="postgres",
    )
    original_password = generated_paths.postgres_password_path.read_text(encoding="utf-8")

    second = bootstrap_generated_config(
        profile="single_host",
        docker=False,
        paths=generated_paths,
        postgres_host="postgres",
    )

    assert generated_paths.postgres_password_path.read_text(encoding="utf-8") == original_password
    assert second["DATABASE_URL"] == first["DATABASE_URL"]


def test_resolve_config_value_prefers_process_env(
    generated_paths: GeneratedConfigPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_generated_config(profile="single_host", docker=False, paths=generated_paths)
    monkeypatch.setenv(JWT_SECRET_ENV, "dev-override-secret")

    assert resolve_config_value(JWT_SECRET_ENV) == "dev-override-secret"


def test_bootstrap_rejects_placeholder_jwt_secret(
    generated_paths: GeneratedConfigPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JWT_SECRET_ENV, "<GENERATE_LONG_RANDOM_SECRET>")

    with pytest.raises(ValueError, match="JWT_SECRET must not use a placeholder value"):
        bootstrap_generated_config(profile="single_host", docker=False, paths=generated_paths)


def test_bootstrap_rejects_invalid_encryption_key(
    generated_paths: GeneratedConfigPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, "not-a-fernet-key")

    with pytest.raises(ValueError, match="ENCRYPTION_KEY must be a valid Fernet key"):
        bootstrap_generated_config(profile="single_host", docker=False, paths=generated_paths)


def test_dev_local_bootstrap_uses_postgresql_default(generated_paths: GeneratedConfigPaths) -> None:
    env = bootstrap_generated_config(profile="dev_local", docker=False, paths=generated_paths)

    assert env["DATABASE_URL"].startswith("postgresql://")
    assert generated_paths.jwt_secret_path.exists()
    assert generated_paths.encryption_key_path.exists()


def test_resolved_backend_env_repairs_invalid_generated_secret_files(
    generated_paths: GeneratedConfigPaths,
) -> None:
    bootstrap_generated_config(profile="single_host", docker=False, paths=generated_paths)
    generated_paths.jwt_secret_path.write_text(
        "<GENERATE_LONG_RANDOM_SECRET>\n",
        encoding="utf-8",
    )
    generated_paths.encryption_key_path.write_text(
        "<GENERATE_FERNET_KEY>\n",
        encoding="utf-8",
    )

    env = resolved_backend_env(profile="single_host", docker=False)

    assert not env[JWT_SECRET_ENV].startswith("<")
    validate_encryption_key(env[ENCRYPTION_KEY_ENV])


def test_resolved_backend_env_reconciles_existing_profile_without_rotating_secrets(
    generated_paths: GeneratedConfigPaths,
) -> None:
    bootstrap_generated_config(profile="dev_local", docker=False, paths=generated_paths)
    original_jwt = generated_paths.jwt_secret_path.read_text(encoding="utf-8")
    original_encryption_key = generated_paths.encryption_key_path.read_text(encoding="utf-8")

    env = resolved_backend_env(profile="single_host", docker=False)
    file_env = read_backend_env(generated_paths.backend_env_path)

    for source in (env, file_env):
        assert source[DEPLOYMENT_PROFILE_ENV] == "single_host"
        assert source[TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV] == "runner"
        assert source[CLOUD_RUNNER_CONTROL_ENABLED_ENV] == "true"
        assert source[RUNNER_TOOL_COMMAND_ENABLED_ENV] == "true"
        assert source[DATABASE_URL_ENV].startswith("postgresql://")
    assert generated_paths.jwt_secret_path.read_text(encoding="utf-8") == original_jwt
    assert generated_paths.encryption_key_path.read_text(encoding="utf-8") == original_encryption_key
