"""Regression tests for auth expiry configuration wiring.

Responsibilities:
- Ensure `backend.auth.ACCESS_TOKEN_EXPIRE_MINUTES` follows environment-backed config.
- Ensure JWT signing secret resolution is fail-closed outside debug mode.
"""

from __future__ import annotations

import importlib
import logging
import os
import secrets

import pytest


@pytest.fixture(autouse=True)
def _isolate_generated_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Prevent developer-local generated secrets from affecting auth tests."""
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))


def _reload_auth_modules(monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
    """Reload config/auth with current monkeypatched environment."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    import backend.config.generated_config as generated_config_module

    def _resolve_env_only(name: str, default: str | None = None) -> str | None:
        value = os.getenv(name)
        return value.strip() if value and value.strip() else default

    monkeypatch.setattr(generated_config_module, "resolve_config_value", _resolve_env_only)
    import backend.config as config_module
    import backend.auth as auth_module

    config_module = importlib.reload(config_module)
    auth_module = importlib.reload(auth_module)
    return config_module, auth_module


def test_auth_access_token_expiry_minutes_honors_env(monkeypatch) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "123")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")

    config_module, auth_module = _reload_auth_modules(monkeypatch)

    assert config_module.ACCESS_TOKEN_EXPIRE_MINUTES == 123
    assert auth_module.ACCESS_TOKEN_EXPIRE_MINUTES == 123


def test_auth_jwt_secret_uses_jwt_secret_env(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-value")

    _, auth_module = _reload_auth_modules(monkeypatch)

    assert secrets.compare_digest(auth_module.SECRET_KEY, "jwt-secret-value")


def test_auth_jwt_secret_warns_for_development_default_in_debug(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("DEBUG", "true")

    caplog.set_level(logging.WARNING, logger="backend.auth")
    _, auth_module = _reload_auth_modules(monkeypatch)

    assert secrets.compare_digest(
        auth_module.SECRET_KEY,
        "your-super-secret-jwt-key-change-in-production",
    )
    assert "JWT_SECRET is not set" in caplog.text


def test_auth_jwt_secret_raises_when_missing_in_production(monkeypatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("DEBUG", "false")

    with pytest.raises(RuntimeError, match="JWT_SECRET is required"):
        _reload_auth_modules(monkeypatch)


def test_auth_jwt_secret_raises_when_dev_default_in_production(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
    monkeypatch.setenv("DEBUG", "false")

    with pytest.raises(RuntimeError, match="must not use the development default"):
        _reload_auth_modules(monkeypatch)


def test_auth_jwt_algorithm_is_internal_constant(monkeypatch) -> None:
    monkeypatch.setenv("JWT_ALGORITHM", "none")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")

    _, auth_module = _reload_auth_modules(monkeypatch)

    assert auth_module.JWT_ALGORITHM == "HS256"
