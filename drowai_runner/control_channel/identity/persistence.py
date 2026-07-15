"""On-disk persistence of runner credential secret, runner id, and negotiated
protocol version, with least-privilege file modes.

Owns the credential/runner-id/protocol-version paths and their atomic, mode-
restricted read/write helpers. Imports ``RunnerConfig`` and the credential
read/write helpers; no control_channel siblings.
"""

from __future__ import annotations

import os
from pathlib import Path

from drowai_runner.config import RunnerConfig
from drowai_runner.credentials import (
    read_runner_credential_secret,
    write_runner_credential_secret,
)


def _persist_runner_secret(config: RunnerConfig, secret: str) -> None:
    secret_path = _credential_secret_path(config)
    write_runner_credential_secret(secret_path, secret)


def _load_runner_secret_if_present(config: RunnerConfig) -> str | None:
    secret_path = _credential_secret_path(config)
    if not secret_path.exists():
        return None
    try:
        return read_runner_credential_secret(secret_path)
    except (OSError, ValueError):
        return None


def _persist_runner_id(config: RunnerConfig, runner_id: str) -> Path:
    runner_id_value = runner_id.strip()
    if not runner_id_value:
        raise ValueError("Runner id must not be empty.")
    path = _runner_id_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _best_effort_chmod(path.parent, 0o700)

    file_descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(runner_id_value)
            handle.write("\n")
    finally:
        os.close(file_descriptor)

    _best_effort_chmod(path, 0o600)
    return path


def _persist_runner_tenant_id(config: RunnerConfig, tenant_id: int) -> Path:
    tenant_value = str(int(tenant_id))
    path = _runner_tenant_id_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _best_effort_chmod(path.parent, 0o700)

    file_descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(tenant_value)
            handle.write("\n")
    finally:
        os.close(file_descriptor)

    _best_effort_chmod(path, 0o600)
    return path


def _load_runner_id_if_present(config: RunnerConfig) -> str | None:
    path = _runner_id_path(config)
    if not path.exists():
        return None
    try:
        payload = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return payload or None


def _load_runner_tenant_id_if_present(config: RunnerConfig) -> int | None:
    path = _runner_tenant_id_path(config)
    if not path.exists():
        return None
    try:
        payload = path.read_text(encoding="utf-8").strip()
        parsed = int(payload)
    except (OSError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _runner_id_path(config: RunnerConfig) -> Path:
    secret_path = _credential_secret_path(config)
    return secret_path.with_name(f"{secret_path.name}.runner_id")


def _runner_tenant_id_path(config: RunnerConfig) -> Path:
    secret_path = _credential_secret_path(config)
    return secret_path.with_name(f"{secret_path.name}.tenant_id")


def _persist_runner_protocol_version(config: RunnerConfig, protocol_version: str) -> Path | None:
    """Persist the control-plane-negotiated protocol version for reconnect reuse."""
    version_value = str(protocol_version or "").strip()
    if not version_value:
        return None
    path = _runner_protocol_version_path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _best_effort_chmod(path.parent, 0o700)

    file_descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(version_value)
            handle.write("\n")
    finally:
        os.close(file_descriptor)

    _best_effort_chmod(path, 0o600)
    return path


def _load_runner_protocol_version_if_present(config: RunnerConfig) -> str | None:
    path = _runner_protocol_version_path(config)
    if not path.exists():
        return None
    try:
        payload = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return payload or None


def _runner_protocol_version_path(config: RunnerConfig) -> Path:
    secret_path = _credential_secret_path(config)
    return secret_path.with_name(f"{secret_path.name}.protocol_version")


def _credential_secret_path(config: RunnerConfig) -> Path:
    return config.credential_secret_path or (config.runner_root / "credentials" / "runner.secret")


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        return
