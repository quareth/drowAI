"""Generated deployment configuration and secret-file helpers.

This module owns the non-user-authored config contract shared by Docker
deployments and local Python launchers. Environment variables remain the
highest-precedence developer override.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import quote_plus

from deploy.env_contract import product_runtime_policy_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_ROOT = _REPO_ROOT / ".drowai-local"
_DOCKER_ROOT = Path("/var/lib/drowai")

CONFIG_DIR_ENV = "DROWAI_CONFIG_DIR"
SECRETS_DIR_ENV = "DROWAI_SECRETS_DIR"

DEPLOYMENT_PROFILE_ENV = "DROWAI_DEPLOYMENT_PROFILE"
TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV = "TASK_RUNTIME_PLACEMENT_MODE_DEFAULT"
CLOUD_RUNNER_CONTROL_ENABLED_ENV = "ENABLE_CLOUD_RUNNER_CONTROL"
RUNNER_TOOL_COMMAND_ENABLED_ENV = "RUNNER_TOOL_COMMAND_ENABLED"
DATA_PLANE_OBJECT_STORE_BACKEND_ENV = "DATA_PLANE_OBJECT_STORE_BACKEND"

POSTGRES_USER_ENV = "POSTGRES_USER"
POSTGRES_DB_ENV = "POSTGRES_DB"
POSTGRES_HOST_ENV = "POSTGRES_HOST"
POSTGRES_PORT_ENV = "POSTGRES_PORT"
POSTGRES_PASSWORD_ENV = "POSTGRES_PASSWORD"
DATABASE_URL_ENV = "DATABASE_URL"
JWT_SECRET_ENV = "JWT_SECRET"
ENCRYPTION_KEY_ENV = "ENCRYPTION_KEY"
MANAGEMENT_URL_ENV = "DROWAI_MANAGEMENT_URL"

DEFAULT_POSTGRES_USER = "drowai_user"
DEFAULT_POSTGRES_DB = "drowai"
DEFAULT_POSTGRES_HOST = "localhost"
DEFAULT_POSTGRES_PORT = 5432
_SECRET_CONFIG_NAMES = frozenset({JWT_SECRET_ENV, ENCRYPTION_KEY_ENV})


@dataclass(frozen=True, slots=True)
class GeneratedConfigPaths:
    """Filesystem locations for generated deployment config and secrets."""

    config_dir: Path
    secrets_dir: Path

    @property
    def backend_env_path(self) -> Path:
        return self.config_dir / "backend.env"

    @property
    def postgres_password_path(self) -> Path:
        return self.secrets_dir / "postgres_password"

    @property
    def jwt_secret_path(self) -> Path:
        return self.secrets_dir / "jwt_secret"

    @property
    def encryption_key_path(self) -> Path:
        return self.secrets_dir / "encryption_key"


def default_generated_paths(*, docker: bool | None = None) -> GeneratedConfigPaths:
    """Return configured generated config paths for current process."""
    config_dir = os.getenv(CONFIG_DIR_ENV)
    secrets_dir = os.getenv(SECRETS_DIR_ENV)
    if config_dir or secrets_dir:
        base = _DOCKER_ROOT if docker else _LOCAL_ROOT
        return GeneratedConfigPaths(
            config_dir=Path(config_dir or base / "config").expanduser(),
            secrets_dir=Path(secrets_dir or base / "secrets").expanduser(),
        )

    use_docker_default = bool(docker)
    base = _DOCKER_ROOT if use_docker_default else _LOCAL_ROOT
    return GeneratedConfigPaths(config_dir=base / "config", secrets_dir=base / "secrets")


def bootstrap_generated_config(
    *,
    profile: str = "dev_local",
    docker: bool = False,
    paths: GeneratedConfigPaths | None = None,
    postgres_host: str | None = None,
) -> dict[str, str]:
    """Create missing generated config/secrets and return resolved env values."""
    resolved_paths = paths or default_generated_paths(docker=docker)
    resolved_paths.config_dir.mkdir(parents=True, exist_ok=True)
    resolved_paths.secrets_dir.mkdir(parents=True, exist_ok=True)

    postgres_user = _env_or_file_value(POSTGRES_USER_ENV, default=DEFAULT_POSTGRES_USER)
    postgres_db = _env_or_file_value(POSTGRES_DB_ENV, default=DEFAULT_POSTGRES_DB)
    default_host = postgres_host or ("postgres" if docker else DEFAULT_POSTGRES_HOST)
    postgres_host_value = _env_or_file_value(POSTGRES_HOST_ENV, default=default_host)
    postgres_port = _env_or_file_value(POSTGRES_PORT_ENV, default=str(DEFAULT_POSTGRES_PORT))

    jwt_secret = _validate_secret_config_value(
        JWT_SECRET_ENV,
        os.getenv(JWT_SECRET_ENV) or secrets.token_urlsafe(64),
    )
    encryption_key = _validate_secret_config_value(
        ENCRYPTION_KEY_ENV,
        os.getenv(ENCRYPTION_KEY_ENV) or _generate_fernet_key(),
    )

    _ensure_secret_file(
        resolved_paths.postgres_password_path,
        os.getenv(POSTGRES_PASSWORD_ENV) or secrets.token_urlsafe(32),
    )
    _ensure_secret_file(
        resolved_paths.jwt_secret_path,
        jwt_secret,
        config_name=JWT_SECRET_ENV,
    )
    _ensure_secret_file(
        resolved_paths.encryption_key_path,
        encryption_key,
        config_name=ENCRYPTION_KEY_ENV,
    )

    env = build_generated_env(
        profile=profile,
        paths=resolved_paths,
        postgres_user=postgres_user,
        postgres_db=postgres_db,
        postgres_host=postgres_host_value,
        postgres_port=int(postgres_port),
    )
    write_backend_env(env, path=resolved_paths.backend_env_path)
    return env


def resolve_config_value(name: str, default: str | None = None) -> str | None:
    """Resolve one value using env override then generated config/secrets."""
    env_value = os.getenv(name)
    if env_value is not None and env_value.strip():
        return _validate_secret_config_value(name, env_value)

    paths = default_generated_paths()
    file_env = read_backend_env(paths.backend_env_path)
    if name in file_env and file_env[name].strip():
        return _validate_secret_config_value(name, file_env[name])

    if name == POSTGRES_PASSWORD_ENV:
        return _read_secret_file(paths.postgres_password_path) or default
    if name == JWT_SECRET_ENV:
        value = _read_secret_file(paths.jwt_secret_path)
        return _validate_secret_config_value(name, value) if value else default
    if name == ENCRYPTION_KEY_ENV:
        value = _read_secret_file(paths.encryption_key_path)
        return _validate_secret_config_value(name, value) if value else default
    if name == DATABASE_URL_ENV:
        password = resolve_config_value(POSTGRES_PASSWORD_ENV)
        if password:
            return build_database_url(
                db_name=file_env.get(POSTGRES_DB_ENV, DEFAULT_POSTGRES_DB),
                db_user=file_env.get(POSTGRES_USER_ENV, DEFAULT_POSTGRES_USER),
                db_password=password,
                db_host=file_env.get(POSTGRES_HOST_ENV, DEFAULT_POSTGRES_HOST),
                db_port=int(file_env.get(POSTGRES_PORT_ENV, str(DEFAULT_POSTGRES_PORT))),
            )
    return default


def resolve_config_bool(name: str, *, default: bool = False) -> bool:
    """Resolve a boolean value using env override then generated config."""
    value = resolve_config_value(name)
    if value is None:
        return bool(default)
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid {name} value: expected a boolean.")


def resolved_backend_env(*, profile: str = "dev_local", docker: bool = False) -> dict[str, str]:
    """Return generated env, bootstrapping it when missing."""
    paths = default_generated_paths(docker=docker)
    if not paths.backend_env_path.exists():
        return bootstrap_generated_config(profile=profile, docker=docker, paths=paths)
    _ensure_secret_file(
        paths.jwt_secret_path,
        secrets.token_urlsafe(64),
        config_name=JWT_SECRET_ENV,
    )
    _ensure_secret_file(
        paths.encryption_key_path,
        _generate_fernet_key(),
        config_name=ENCRYPTION_KEY_ENV,
    )
    file_env = read_backend_env(paths.backend_env_path)
    reconciled_file_env = _reconcile_generated_profile_env(file_env, profile=profile, paths=paths)
    reconciled_file_env[JWT_SECRET_ENV] = _validate_secret_config_value(
        JWT_SECRET_ENV,
        _read_required_secret(paths.jwt_secret_path),
    )
    reconciled_file_env[ENCRYPTION_KEY_ENV] = _validate_secret_config_value(
        ENCRYPTION_KEY_ENV,
        _read_required_secret(paths.encryption_key_path),
    )
    if reconciled_file_env != file_env:
        write_backend_env(reconciled_file_env, path=paths.backend_env_path)

    env = dict(reconciled_file_env)
    for key in (DATABASE_URL_ENV, JWT_SECRET_ENV, ENCRYPTION_KEY_ENV):
        resolved = resolve_config_value(key)
        if resolved:
            env[key] = resolved
    return env


def build_generated_env(
    *,
    profile: str,
    paths: GeneratedConfigPaths,
    postgres_user: str,
    postgres_db: str,
    postgres_host: str,
    postgres_port: int,
) -> dict[str, str]:
    """Build the generated backend env map from secret files."""
    postgres_password = _read_required_secret(paths.postgres_password_path)
    jwt_secret = _validate_secret_config_value(
        JWT_SECRET_ENV,
        _read_required_secret(paths.jwt_secret_path),
    )
    encryption_key = _validate_secret_config_value(
        ENCRYPTION_KEY_ENV,
        _read_required_secret(paths.encryption_key_path),
    )
    env = {
        POSTGRES_USER_ENV: postgres_user,
        POSTGRES_DB_ENV: postgres_db,
        POSTGRES_HOST_ENV: postgres_host,
        POSTGRES_PORT_ENV: str(postgres_port),
        POSTGRES_PASSWORD_ENV: postgres_password,
        DATABASE_URL_ENV: build_database_url(
            db_name=postgres_db,
            db_user=postgres_user,
            db_password=postgres_password,
            db_host=postgres_host,
            db_port=postgres_port,
        ),
        JWT_SECRET_ENV: jwt_secret,
        ENCRYPTION_KEY_ENV: encryption_key,
        DEPLOYMENT_PROFILE_ENV: profile,
    }
    env.update(_generated_product_runtime_policy_env(profile))
    return env


def build_database_url(
    *,
    db_name: str,
    db_user: str,
    db_password: str,
    db_host: str,
    db_port: int,
) -> str:
    """Build a PostgreSQL SQLAlchemy URL."""
    return (
        f"postgresql://{quote_plus(db_user)}:{quote_plus(db_password)}"
        f"@{db_host}:{db_port}/{db_name}"
    )


def read_backend_env(path: Path | None = None) -> dict[str, str]:
    """Read an env-style generated backend config file."""
    target = path or default_generated_paths().backend_env_path
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
            env[key] = parts[0] if len(parts) == 1 else value.strip()
        except ValueError:
            env[key] = value.strip().strip("\"'")
    return env


def write_backend_env(env: Mapping[str, str], *, path: Path | None = None) -> Path:
    """Write generated backend env atomically enough for local startup."""
    target = path or default_generated_paths().backend_env_path
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Generated by DrowAI; do not edit for product deployment."]
    for key in sorted(env):
        value = str(env[key])
        lines.append(f"{key}={shlex.quote(value)}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def update_generated_database_password(
    password: str,
    *,
    paths: GeneratedConfigPaths | None = None,
) -> dict[str, str]:
    """Persist a rotated database password and refresh generated backend env."""
    return update_generated_database_config(postgres_password=password, paths=paths)


def update_generated_database_config(
    *,
    postgres_password: str | None = None,
    postgres_user: str | None = None,
    postgres_db: str | None = None,
    postgres_host: str | None = None,
    postgres_port: int | str | None = None,
    paths: GeneratedConfigPaths | None = None,
) -> dict[str, str]:
    """Persist generated PostgreSQL connection settings and refresh backend env."""
    resolved_paths = paths or default_generated_paths()
    current_env = read_backend_env(resolved_paths.backend_env_path)
    if postgres_password is not None:
        cleaned = str(postgres_password or "")
        if not cleaned:
            raise ValueError("Database password must not be empty.")
        _write_secret_file(resolved_paths.postgres_password_path, cleaned, overwrite=True)
    profile = current_env.get("DROWAI_DEPLOYMENT_PROFILE") or os.getenv(
        "DROWAI_DEPLOYMENT_PROFILE", "dev_local"
    )
    env = build_generated_env(
        profile=profile,
        paths=resolved_paths,
        postgres_user=str(postgres_user or current_env.get(POSTGRES_USER_ENV) or DEFAULT_POSTGRES_USER),
        postgres_db=str(postgres_db or current_env.get(POSTGRES_DB_ENV) or DEFAULT_POSTGRES_DB),
        postgres_host=str(postgres_host or current_env.get(POSTGRES_HOST_ENV) or DEFAULT_POSTGRES_HOST),
        postgres_port=int(postgres_port or current_env.get(POSTGRES_PORT_ENV) or DEFAULT_POSTGRES_PORT),
    )
    write_backend_env(env, path=resolved_paths.backend_env_path)
    return env


def update_generated_management_url(
    management_url: str,
    *,
    paths: GeneratedConfigPaths | None = None,
) -> dict[str, str]:
    """Persist the canonical Runner-facing Management URL."""
    cleaned = str(management_url or "").strip()
    if not cleaned:
        raise ValueError("Management URL must not be empty.")
    resolved_paths = paths or default_generated_paths()
    env = read_backend_env(resolved_paths.backend_env_path)
    env[MANAGEMENT_URL_ENV] = cleaned
    write_backend_env(env, path=resolved_paths.backend_env_path)
    return env


def shell_export_lines(env: Mapping[str, str]) -> str:
    """Return POSIX shell export lines for generated env values."""
    return "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in sorted(env.items()))


def _env_or_file_value(name: str, *, default: str) -> str:
    value = os.getenv(name)
    if value is not None and value.strip():
        return value.strip()
    return default


def _generated_product_runtime_policy_env(profile: str) -> dict[str, str]:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"single_host", "distributed"}:
        return {}
    return product_runtime_policy_env(profile=normalized_profile)


def _reconcile_generated_profile_env(
    env: Mapping[str, str],
    *,
    profile: str,
    paths: GeneratedConfigPaths,
) -> dict[str, str]:
    """Return env with launcher-requested generated profile policy applied."""
    normalized_profile = str(profile or "").strip().lower() or "dev_local"
    reconciled = dict(env)
    reconciled[DEPLOYMENT_PROFILE_ENV] = normalized_profile
    if _is_sqlite_database_url(reconciled.get(DATABASE_URL_ENV)):
        reconciled[DATABASE_URL_ENV] = _database_url_from_generated_postgres(
            reconciled,
            paths=paths,
        )
    product_policy = _generated_product_runtime_policy_env(normalized_profile)
    if product_policy:
        reconciled.update(product_policy)
    else:
        for key in (
            TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
            CLOUD_RUNNER_CONTROL_ENABLED_ENV,
            RUNNER_TOOL_COMMAND_ENABLED_ENV,
        ):
            reconciled.pop(key, None)
    return reconciled


def _is_sqlite_database_url(value: object) -> bool:
    return str(value or "").strip().lower().startswith("sqlite:")


def _database_url_from_generated_postgres(
    env: Mapping[str, str],
    *,
    paths: GeneratedConfigPaths,
) -> str:
    password = (
        _read_secret_file(paths.postgres_password_path)
        or str(env.get(POSTGRES_PASSWORD_ENV) or "").strip()
    )
    if not password:
        raise RuntimeError("Generated PostgreSQL password is missing.")
    return build_database_url(
        db_name=str(env.get(POSTGRES_DB_ENV) or DEFAULT_POSTGRES_DB),
        db_user=str(env.get(POSTGRES_USER_ENV) or DEFAULT_POSTGRES_USER),
        db_password=password,
        db_host=str(env.get(POSTGRES_HOST_ENV) or DEFAULT_POSTGRES_HOST),
        db_port=int(env.get(POSTGRES_PORT_ENV) or DEFAULT_POSTGRES_PORT),
    )


def validate_encryption_key(value: str | bytes) -> bytes:
    """Return a validated Fernet key without exposing its value in errors."""
    key_bytes = value.encode() if isinstance(value, str) else bytes(value)
    try:
        decoded = base64.b64decode(key_bytes, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{ENCRYPTION_KEY_ENV} must be a valid Fernet key") from exc
    if len(decoded) != 32:
        raise ValueError(f"{ENCRYPTION_KEY_ENV} must be a valid Fernet key")
    return key_bytes


def _validate_secret_config_value(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if name not in _SECRET_CONFIG_NAMES:
        return normalized
    if normalized.startswith("<") and normalized.endswith(">"):
        raise ValueError(f"{name} must not use a placeholder value")
    if name == ENCRYPTION_KEY_ENV:
        validate_encryption_key(normalized)
    return normalized


def _ensure_secret_file(
    path: Path,
    value: str,
    *,
    config_name: str | None = None,
) -> None:
    existing = _read_secret_file(path) if path.exists() else None
    if existing:
        if config_name is None:
            return
        try:
            _validate_secret_config_value(config_name, existing)
            return
        except ValueError:
            pass
    _write_secret_file(path, value, overwrite=path.exists())


def _write_secret_file(path: Path, value: str, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return
    path.write_text(str(value).strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_secret_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_required_secret(path: Path) -> str:
    value = _read_secret_file(path)
    if not value:
        raise RuntimeError(f"Generated secret is missing: {path}")
    return value


def _generate_fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage generated DrowAI config.")
    parser.add_argument("command", choices=("init", "print-env"), nargs="?", default="init")
    parser.add_argument("--profile", default=os.getenv("DROWAI_DEPLOYMENT_PROFILE", "dev_local"))
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--postgres-host", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for generated config bootstrapping."""
    args = _build_parser().parse_args(argv)
    env = bootstrap_generated_config(
        profile=args.profile,
        docker=bool(args.docker),
        postgres_host=args.postgres_host,
    )
    if args.command == "print-env":
        print(shell_export_lines(env))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
