"""Managed-runner configuration contract for the runner process.

This module provides backend-independent defaults and fail-closed validation
for local runner resources plus managed control-plane connectivity settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import tomllib

from drowai_runner.cloud_config import RunnerCloudConfig, validate_cloud_base_url
from runtime_shared.runtime_network import (
    DEFAULT_RUNTIME_NETWORK_POOL,
    RUNTIME_NETWORK_POOL_ENV,
    parse_runtime_network_pool,
)
from runtime_shared.runtime_image_contract import default_runtime_image_for_machine

_DEFAULT_RUNTIME_IMAGE_TAG = default_runtime_image_for_machine()
_DEFAULT_RUNNER_ROOT = Path("/var/lib/drowai")
_DEFAULT_DEV_RUNNER_ROOT = Path(".drowai-runner")
_ALLOW_INSECURE_CLOUD_ENDPOINT_ENV = "DROWAI_RUNNER_ALLOW_INSECURE_CLOUD_ENDPOINT"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_runner_root(raw_root: str) -> Path:
    """Return an absolute runner root suitable for Docker bind mounts."""
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root


@dataclass(frozen=True)
class RunnerConfig:
    """Minimal runner configuration shared by local and distributed deployments."""

    runner_root: Path
    runtime_image_tag: str
    docker_endpoint_mode: str
    max_active_tasks: int
    max_parallel_commands_per_task: int
    cleanup_retention_hours: int
    log_level: str
    cloud_base_url: str | None = None
    registration_token: str | None = None
    tenant_id: int | None = None
    runner_id: str | None = None
    credential_secret_path: Path | None = None
    heartbeat_interval_seconds: int = 30
    tls_verify: bool = True
    allow_insecure_cloud_endpoint: bool = False
    labels: dict[str, str] | None = None
    capabilities: tuple[str, ...] = ()
    host_bind_root: Path | None = None
    runtime_network_pool: str = DEFAULT_RUNTIME_NETWORK_POOL

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "RunnerConfig":
        """Build config from a mapping and validate fail-closed defaults."""
        dev_mode = _truthy(
            _as_text(_read_option(values, "DROWAI_RUNNER_DEV_MODE", "dev_mode"))
        )
        default_root = _DEFAULT_DEV_RUNNER_ROOT if dev_mode else _DEFAULT_RUNNER_ROOT
        runner_root = _normalize_runner_root(
            _as_text(
                _read_option(values, "DROWAI_RUNNER_ROOT", "runner_root"),
                str(default_root),
            )
        )
        allow_insecure_cloud_endpoint = _as_bool(
            _read_option(
                values,
                _ALLOW_INSECURE_CLOUD_ENDPOINT_ENV,
                "allow_insecure_cloud_endpoint",
            ),
            default=False,
        )
        cloud_base_url = _as_optional_text(_read_control_plane_url_option(values))
        if cloud_base_url:
            cloud_base_url = validate_cloud_base_url(
                cloud_base_url,
                allow_insecure_cloud_endpoint=allow_insecure_cloud_endpoint,
            )

        labels = _normalize_labels(
            _read_option(values, "DROWAI_RUNNER_LABELS", "labels")
        )
        capabilities = _normalize_capabilities(
            _read_option(values, "DROWAI_RUNNER_CAPABILITIES", "capabilities")
        )
        config = cls(
            runner_root=runner_root,
            runtime_image_tag=_as_text(
                _read_option(values, "DROWAI_RUNTIME_IMAGE", "runtime_image_tag"),
                _DEFAULT_RUNTIME_IMAGE_TAG,
            ).strip()
            or _DEFAULT_RUNTIME_IMAGE_TAG,
            docker_endpoint_mode=(
                _as_text(
                    _read_option(
                        values,
                        "DROWAI_RUNNER_DOCKER_ENDPOINT_MODE",
                        "docker_endpoint_mode",
                    ),
                    "local",
                )
                .strip()
                .lower()
            ),
            max_active_tasks=int(
                _as_text(
                    _read_option(values, "DROWAI_RUNNER_MAX_ACTIVE_TASKS", "max_active_tasks"),
                    "2",
                )
            ),
            max_parallel_commands_per_task=int(
                _as_text(
                    _read_option(
                        values,
                        "DROWAI_RUNNER_MAX_PARALLEL_COMMANDS_PER_TASK",
                        "max_parallel_commands_per_task",
                    ),
                    "4",
                )
            ),
            cleanup_retention_hours=int(
                _as_text(
                    _read_option(
                        values,
                        "DROWAI_RUNNER_CLEANUP_RETENTION_HOURS",
                        "cleanup_retention_hours",
                    ),
                    "24",
                )
            ),
            log_level=_as_text(
                _read_option(values, "DROWAI_RUNNER_LOG_LEVEL", "log_level"),
                "INFO",
            )
            .strip()
            .upper(),
            cloud_base_url=cloud_base_url,
            registration_token=_as_optional_text(
                _read_option(
                    values,
                    "DROWAI_RUNNER_REGISTRATION_TOKEN",
                    "registration_token",
                )
            ),
            tenant_id=_as_optional_positive_int(
                _read_option(values, "DROWAI_RUNNER_TENANT_ID", "tenant_id")
            ),
            runner_id=_as_optional_text(
                _read_option(values, "DROWAI_RUNNER_ID", "runner_id")
            ),
            credential_secret_path=_normalize_optional_path(
                _read_option(
                    values,
                    "DROWAI_RUNNER_CREDENTIAL_SECRET_PATH",
                    "credential_secret_path",
                ),
                runner_root=runner_root,
            ),
            heartbeat_interval_seconds=int(
                _as_text(
                    _read_option(
                        values,
                        "DROWAI_RUNNER_HEARTBEAT_INTERVAL_SECONDS",
                        "heartbeat_interval_seconds",
                    ),
                    "30",
                )
            ),
            tls_verify=_as_bool(
                _read_option(values, "DROWAI_RUNNER_TLS_VERIFY", "tls_verify"),
                default=True,
            ),
            allow_insecure_cloud_endpoint=allow_insecure_cloud_endpoint,
            labels=labels,
            capabilities=capabilities,
            host_bind_root=_normalize_optional_path(
                _read_option(
                    values,
                    "DROWAI_RUNNER_HOST_BIND_ROOT",
                    "host_bind_root",
                ),
                runner_root=runner_root,
            ),
            runtime_network_pool=_as_text(
                _read_option(
                    values,
                    RUNTIME_NETWORK_POOL_ENV,
                    "runtime_network_pool",
                ),
                DEFAULT_RUNTIME_NETWORK_POOL,
            ).strip(),
        )
        return config.validate()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RunnerConfig":
        values = os.environ if env is None else env
        return cls.from_mapping(values)

    @classmethod
    def from_toml(cls, config_path: str | Path) -> "RunnerConfig":
        """Load runner config from a TOML file path."""
        with Path(config_path).open("rb") as handle:
            payload = tomllib.load(handle)
        runner_payload = payload.get("runner", payload)
        if not isinstance(runner_payload, dict):
            raise ValueError("Runner config TOML must provide a [runner] table or root mapping.")
        return cls.from_mapping(runner_payload)

    def validate(self) -> "RunnerConfig":
        if self.docker_endpoint_mode not in {"local", "socket", "environment"}:
            raise ValueError(
                "docker_endpoint_mode must be one of: local, socket, environment."
            )
        if self.max_active_tasks < 1:
            raise ValueError("max_active_tasks must be >= 1.")
        if self.max_parallel_commands_per_task < 1:
            raise ValueError("max_parallel_commands_per_task must be >= 1.")
        if self.cleanup_retention_hours < 1:
            raise ValueError("cleanup_retention_hours must be >= 1.")
        if not self.runtime_image_tag:
            raise ValueError("runtime_image_tag must not be empty.")
        if not self.log_level:
            raise ValueError("log_level must not be empty.")
        if self.tenant_id is not None and self.tenant_id < 1:
            raise ValueError("tenant_id must be >= 1 when configured.")
        parse_runtime_network_pool(self.runtime_network_pool)

        RunnerCloudConfig(
            cloud_base_url=self.cloud_base_url,
            registration_token=self.registration_token,
            runner_id=self.runner_id,
            credential_secret_path=self.credential_secret_path,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            tls_verify=self.tls_verify,
            allow_insecure_cloud_endpoint=self.allow_insecure_cloud_endpoint,
            labels=dict(self.labels or {}),
            capabilities=self.capabilities,
        ).validate()
        return self


def _as_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_optional_text(value: object) -> str | None:
    text = _as_text(value).strip()
    return text or None


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _truthy(str(value))


def _as_optional_positive_int(value: object) -> int | None:
    text = _as_optional_text(value)
    if text is None:
        return None
    parsed = int(text)
    if parsed < 1:
        raise ValueError("tenant_id must be >= 1 when configured.")
    return parsed


def _normalize_optional_path(value: object, *, runner_root: Path) -> Path | None:
    text = _as_optional_text(value)
    if text is None:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = runner_root / candidate
    return candidate


def _normalize_labels(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        normalized: dict[str, str] = {}
        for key, raw_value in value.items():
            normalized_key = str(key).strip()
            normalized_value = str(raw_value).strip()
            if normalized_key:
                normalized[normalized_key] = normalized_value
        return normalized

    parsed = _parse_json_or_csv(value)
    if isinstance(parsed, Mapping):
        normalized: dict[str, str] = {}
        for key, raw_value in parsed.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            normalized[normalized_key] = str(raw_value).strip()
        return normalized
    raise ValueError("runner labels must be a mapping or JSON object.")


def _normalize_capabilities(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(
            item.strip()
            for item in (str(raw_item) for raw_item in value)
            if item.strip()
        )

    parsed = _parse_json_or_csv(value)
    if isinstance(parsed, list):
        return tuple(
            item.strip()
            for item in (str(raw_item) for raw_item in parsed)
            if item.strip()
        )
    if isinstance(parsed, str):
        return tuple(part.strip() for part in parsed.split(",") if part.strip())
    raise ValueError("runner capabilities must be a sequence or JSON array.")


def _parse_json_or_csv(value: object) -> object:
    text = _as_text(value).strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _read_option(values: Mapping[str, object], env_key: str, field_key: str) -> object:
    if env_key in values:
        return values[env_key]
    return values.get(field_key)


def _read_control_plane_url_option(values: Mapping[str, object]) -> object:
    for key in (
        "DROWAI_RUNNER_CONTROL_PLANE_URL",
        "control_plane_url",
        "DROWAI_RUNNER_CLOUD_BASE_URL",
        "cloud_base_url",
    ):
        if key in values:
            return values[key]
    return None
