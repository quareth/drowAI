"""Runner credential secret persistence and masking helpers.

This module owns runner-local credential file IO so cloud credentials are
written with restrictive permissions and never emitted in logs.
"""

from __future__ import annotations

import os
from pathlib import Path


def mask_secret(value: str | None) -> str:
    """Return a safe marker for secret-bearing fields."""
    return "<NO_KEY>" if not (value or "").strip() else "<KEY_SET>"


def write_runner_credential_secret(path: str | Path, secret: str) -> Path:
    """Persist a runner credential secret to disk with restrictive permissions."""
    secret_value = secret.strip()
    if not secret_value:
        raise ValueError("Runner credential secret must not be empty.")

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _apply_chmod_if_possible(target.parent, 0o700)

    file_descriptor = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(secret_value)
            handle.write("\n")
    finally:
        os.close(file_descriptor)

    _apply_chmod_if_possible(target, 0o600)
    return target


def read_runner_credential_secret(path: str | Path) -> str:
    """Read a runner credential secret from disk and reject empty content."""
    target = Path(path).expanduser()
    payload = target.read_text(encoding="utf-8").strip()
    if not payload:
        raise ValueError("Runner credential secret file is empty.")
    return payload


def _apply_chmod_if_possible(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        return
