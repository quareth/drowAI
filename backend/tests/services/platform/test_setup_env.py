"""Tests for setup wizard environment helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from backend.services.platform.setup_env import (
    generate_env_file_content,
    resolve_configured_database_identity,
    resolve_database_host,
    resolve_encryption_key,
    resolve_env_file_path,
)


def test_resolve_configured_database_identity_uses_postgres_engine_url() -> None:
    engine = create_engine("postgresql://configured_user:secret@localhost/configured_db")
    try:
        assert resolve_configured_database_identity(engine) == (
            "configured_db",
            "configured_user",
        )
    finally:
        engine.dispose()


def test_resolve_configured_database_identity_uses_generated_values_for_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_DB", "configured_db")
    monkeypatch.setenv("POSTGRES_USER", "configured_user")
    engine = create_engine("sqlite:///:memory:")
    try:
        assert resolve_configured_database_identity(engine) == (
            "configured_db",
            "configured_user",
        )
    finally:
        engine.dispose()


def test_resolve_database_host_prefers_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    monkeypatch.setenv("DATABASE_URL", "postgresql://drowai_user:secret@localhost:5432/drowai")
    assert resolve_database_host() == "localhost"


def test_resolve_database_host_uses_compose_default_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "single_host")
    assert resolve_database_host() == "postgres"


def test_resolve_database_host_uses_localhost_for_dev_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local")
    assert resolve_database_host() == "localhost"


def test_resolve_env_file_path_uses_configured_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DROWAI_ENV_FILE", "/app/.env")
    assert resolve_env_file_path() == Path("/app/.env")


def test_generate_env_file_content_preserves_process_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "stable-process-key")
    monkeypatch.delenv("DROWAI_RUNTIME_IMAGE", raising=False)

    content = generate_env_file_content(
        database_url="postgresql://drowai_user:secret@postgres:5432/drowai",
        postgres_db="drowai",
        postgres_user="drowai_user",
        postgres_password="secret",
        jwt_secret="jwt-secret",
        access_token_expire_minutes=30,
        runner_registration_token="rit_test_token",
        runner_tenant_id=42,
    )

    assert "ENCRYPTION_KEY=stable-process-key" in content
    assert "DROWAI_RUNNER_REGISTRATION_TOKEN=rit_test_token" in content
    assert "DROWAI_RUNNER_TENANT_ID=42" in content
    assert "DROWAI_RUNTIME_IMAGE" not in content
    assert "OPENAI_API_KEY" not in content
    assert "AGENT_REASONING_MOCK_MODE" not in content
    assert "REASONING_DB_PERSIST" not in content
    assert "REASONING_DB_STREAM" not in content
    assert "VITE_API_URL" not in content
    assert "VITE_API_TIMEOUT" not in content
    assert "VITE_ENABLE_MULTI_TASK_STREAM_MANAGER" not in content
    assert "DROWAI_RUNNER_DEV_MODE" not in content
    assert "DROWAI_RUNNER_CAPABILITIES" not in content
    assert "DROWAI_RUNNER_CONTROL_PLANE_URL" not in content
    assert "DROWAI_RUNNER_HOST_BIND_ROOT" not in content
    assert "DROWAI_RUNNER_LABELS" not in content
    assert "DROWAI_RUNNER_ROOT" not in content
    assert "DROWAI_RUNNER_TLS_VERIFY" not in content
    assert "PYTHONUNBUFFERED" not in content


def test_generate_env_file_content_keeps_runtime_image_as_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DROWAI_RUNTIME_IMAGE", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", "stable-process-key")

    content = generate_env_file_content(
        database_url="postgresql://drowai_user:secret@postgres:5432/drowai",
        postgres_db="drowai",
        postgres_user="drowai_user",
        postgres_password="secret",
        jwt_secret="jwt-secret",
        access_token_expire_minutes=30,
        runtime_image="drowai/kali-pentesting:amd64-runtime",
    )

    assert "DROWAI_RUNTIME_IMAGE=drowai/kali-pentesting:amd64-runtime" in content


def test_resolve_encryption_key_falls_back_to_existing_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ENCRYPTION_KEY=stable-file-key\n", encoding="utf-8")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("DROWAI_ENV_FILE", str(env_file))

    assert resolve_encryption_key() == "stable-file-key"
