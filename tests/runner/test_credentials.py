"""Tests for runner credential storage and redaction helpers."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from drowai_runner.credentials import (
    mask_secret,
    read_runner_credential_secret,
    write_runner_credential_secret,
)


def test_write_runner_credential_secret_uses_restrictive_permissions(tmp_path: Path) -> None:
    target = tmp_path / "credentials" / "runner.secret"

    written_path = write_runner_credential_secret(target, "runner-secret-value")

    assert written_path == target
    assert read_runner_credential_secret(target) == "runner-secret-value"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & 0o077 == 0


def test_read_runner_credential_secret_rejects_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.secret"
    target.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="secret file is empty"):
        read_runner_credential_secret(target)


def test_mask_secret_never_returns_raw_secret_values() -> None:
    assert mask_secret(None) == "<NO_KEY>"
    assert mask_secret("") == "<NO_KEY>"
    assert mask_secret("super-secret-token") == "<KEY_SET>"
