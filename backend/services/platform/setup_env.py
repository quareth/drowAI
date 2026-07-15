"""Setup wizard helpers for database checks and generated deployment config."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from backend.config.generated_config import (
    DATABASE_URL_ENV,
    DEFAULT_POSTGRES_DB,
    DEFAULT_POSTGRES_USER,
    POSTGRES_DB_ENV,
    POSTGRES_USER_ENV,
    resolve_config_value,
)
from deploy.env_contract import single_host_management_env


def _database_url_host() -> str | None:
    """Return the hostname from the configured application ``DATABASE_URL``."""
    database_url = str(os.getenv("DATABASE_URL", "") or "").strip()
    if not database_url:
        return None
    normalized = database_url.replace("postgres://", "postgresql://", 1)
    hostname = (urlparse(normalized).hostname or "").strip()
    return hostname or None


def resolve_database_host() -> str:
    """Return the default Postgres host for the active deployment profile."""
    configured_host = _database_url_host()
    if configured_host:
        return configured_host

    profile = str(os.getenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local") or "dev_local").strip().lower()
    if profile == "single_host":
        return "postgres"
    return "localhost"


def resolve_configured_database_identity(engine: Engine | None = None) -> tuple[str, str]:
    """Return the configured database name and user accepted by setup."""
    configured_url: URL | None = getattr(engine, "url", None)
    if configured_url is None:
        database_url = str(resolve_config_value(DATABASE_URL_ENV) or "").strip()
        if database_url:
            configured_url = make_url(database_url.replace("postgres://", "postgresql://", 1))

    if configured_url is not None and configured_url.get_backend_name() == "postgresql":
        db_name = str(configured_url.database or "").strip()
        db_user = str(configured_url.username or "").strip()
        if db_name and db_user:
            return db_name, db_user

    return (
        str(resolve_config_value(POSTGRES_DB_ENV, DEFAULT_POSTGRES_DB) or DEFAULT_POSTGRES_DB).strip(),
        str(
            resolve_config_value(POSTGRES_USER_ENV, DEFAULT_POSTGRES_USER)
            or DEFAULT_POSTGRES_USER
        ).strip(),
    )


def build_database_url(
    *,
    db_name: str,
    db_user: str,
    db_password: str,
    db_host: str | None = None,
    db_port: int = 5432,
) -> str:
    """Build a PostgreSQL SQLAlchemy URL."""
    host = (db_host or resolve_database_host()).strip() or "localhost"
    user = quote_plus(db_user)
    password = quote_plus(db_password)
    return f"postgresql://{user}:{password}@{host}:{db_port}/{db_name}"


def test_database_connection(
    *,
    db_name: str,
    db_user: str,
    db_password: str,
    db_host: str | None = None,
    db_port: int = 5432,
) -> None:
    """Verify PostgreSQL connectivity using the supplied credentials."""
    url = build_database_url(
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        db_host=db_host,
        db_port=db_port,
    )
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def ping_configured_database(engine: Engine | None = None) -> bool:
    """Return True when the configured application database responds to a ping."""
    target_engine = engine
    created = False
    if target_engine is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            return False
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        target_engine = create_engine(database_url, pool_pre_ping=True)
        created = True
    try:
        with target_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        if created:
            target_engine.dispose()


def resolve_env_file_path() -> Path:
    """Return the repo-root `.env` path used by standalone installs."""
    configured = str(os.getenv("DROWAI_ENV_FILE", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / ".env"


def _read_env_file_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate_key, value = line.split("=", 1)
        if candidate_key.strip() == key:
            normalized = value.strip().strip('"').strip("'")
            return normalized or None
    return None


def _generate_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def resolve_encryption_key(encryption_key: str | None = None) -> str:
    """Return the stable encryption key used by generated config or dev overrides."""
    explicit = str(encryption_key or "").strip()
    if explicit:
        return explicit
    generated_value = str(resolve_config_value("ENCRYPTION_KEY") or "").strip()
    if generated_value:
        return generated_value
    env_value = str(os.getenv("ENCRYPTION_KEY", "") or "").strip()
    if env_value:
        return env_value
    file_value = _read_env_file_value(resolve_env_file_path(), "ENCRYPTION_KEY")
    if file_value:
        return file_value
    return _generate_encryption_key()


def generate_env_file_content(
    *,
    database_url: str,
    postgres_db: str,
    postgres_user: str,
    postgres_password: str,
    jwt_secret: str,
    access_token_expire_minutes: int,
    runner_registration_token: str | None = None,
    runner_tenant_id: int | str | None = None,
    runtime_image: str | None = None,
    encryption_key: str | None = None,
) -> str:
    """Build standalone `.env` content aligned with env.example and env_contract."""
    profile = str(os.getenv("DROWAI_DEPLOYMENT_PROFILE", "single_host") or "single_host").strip().lower()
    if profile not in {"single_host", "dev_local"}:
        profile = "single_host"

    runtime_image_value = (runtime_image or os.getenv("DROWAI_RUNTIME_IMAGE") or "").strip()
    encryption_key_value = resolve_encryption_key(encryption_key)
    management_env = single_host_management_env(
        control_plane_url="http://backend:8000" if profile == "single_host" else "http://127.0.0.1:8000",
        runtime_image=runtime_image_value or None,
    )

    lines = [
        "# DrowAI Configuration",
        "# Generated by setup wizard",
        "",
        "# Database",
        f"DATABASE_URL={database_url}",
        f"POSTGRES_DB={postgres_db}",
        f"POSTGRES_USER={postgres_user}",
        f"POSTGRES_PASSWORD={postgres_password}",
        "",
        "# Security",
        f"JWT_SECRET={jwt_secret}",
        f"ENCRYPTION_KEY={encryption_key_value}",
        f"ACCESS_TOKEN_EXPIRE_MINUTES={access_token_expire_minutes}",
    ]
    if runtime_image_value:
        lines.extend(
            [
                "",
                "# Runtime / execution plane override",
                f"DROWAI_RUNTIME_IMAGE={runtime_image_value}",
            ]
        )

    for key, value in management_env.items():
        if key not in {
            "DROWAI_DEPLOYMENT_PROFILE",
            "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT",
            "ENABLE_CLOUD_RUNNER_CONTROL",
            "RUNNER_TOOL_COMMAND_ENABLED",
            "DATA_PLANE_OBJECT_STORE_BACKEND",
        }:
            continue
        lines.append(f"{key}={value}")

    if runner_registration_token:
        normalized_runner_tenant_id = str(runner_tenant_id or "").strip()
        if not normalized_runner_tenant_id:
            normalized_runner_tenant_id = "1"
        lines.extend(
            [
                "",
                "# Runner registration",
                f"DROWAI_RUNNER_REGISTRATION_TOKEN={runner_registration_token}",
                f"DROWAI_RUNNER_TENANT_ID={normalized_runner_tenant_id}",
            ]
        )

    lines.extend(
        [
            "",
            "# Logging",
            "LOG_FILE=backend/log/backend.log",
            "LOG_LEVEL=INFO",
            "DEBUG=false",
            "",
            "# LLM provider API keys are configured in the setup/settings UI and stored encrypted.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_env_file(content: str, *, path: Path | None = None) -> Path:
    """Write generated env content to disk."""
    target = path or resolve_env_file_path()
    target.write_text(content, encoding="utf-8")
    return target
