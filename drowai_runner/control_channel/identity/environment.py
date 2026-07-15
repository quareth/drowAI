"""Environment-sourced runner identity resolution (tenant id, version, default name).

Reads environment variables only; raises ``RunnerCloudClientError`` on
missing/invalid tenant id. No file or network I/O.
"""

from __future__ import annotations

import os
import socket

from drowai_runner.control_channel.constants import RUNNER_VERSION_ENV, TENANT_ID_ENV
from drowai_runner.control_channel.errors import RunnerCloudClientError


def _resolve_tenant_id() -> int:
    raw = str(_read_env(TENANT_ID_ENV) or "").strip()
    if not raw:
        raise RunnerCloudClientError(
            error_code="RUNNER_TENANT_ID_MISSING",
            message=f"Set {TENANT_ID_ENV} for cloud mode.",
        )
    try:
        tenant_id = int(raw)
    except ValueError as exc:
        raise RunnerCloudClientError(
            error_code="RUNNER_TENANT_ID_INVALID",
            message=f"{TENANT_ID_ENV} must be an integer.",
        ) from exc
    if tenant_id < 1:
        raise RunnerCloudClientError(
            error_code="RUNNER_TENANT_ID_INVALID",
            message=f"{TENANT_ID_ENV} must be >= 1.",
        )
    return tenant_id


def _resolve_runner_version() -> str:
    value = str(_read_env(RUNNER_VERSION_ENV) or "").strip()
    return value or "runner-control-client"


def _default_runner_name() -> str:
    host = socket.gethostname().strip() or "runner"
    trimmed = "".join(ch for ch in host if ch.isalnum() or ch in {"-", "_"})
    return f"runner-{trimmed[:48] or 'node'}"


def _read_env(name: str) -> str | None:
    return os.getenv(name)
