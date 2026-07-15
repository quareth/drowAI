"""Tests for safe LLM credential encryption-key resolution and recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from backend.services.llm_provider import credential_service


def test_legacy_key_file_accepts_trailing_newline_without_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newline-delimited legacy key file must retain its existing key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))
    valid_key = Fernet.generate_key()
    persisted_key = valid_key + b"\n"
    key_path = tmp_path / ".encryption_key"
    key_path.write_bytes(persisted_key)
    monkeypatch.setattr(credential_service, "_ENCRYPTION_KEY_CACHE", None)

    resolved = credential_service.get_encryption_key()

    assert resolved == valid_key
    assert key_path.read_bytes() == persisted_key


def test_invalid_legacy_key_file_recovers_from_valid_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale invalid key file must not override corrected configuration."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DROWAI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("DROWAI_SECRETS_DIR", str(tmp_path / "secrets"))
    valid_key = Fernet.generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", valid_key.decode())
    (tmp_path / ".encryption_key").write_text("<GENERATE_FERNET_KEY>", encoding="utf-8")
    monkeypatch.setattr(credential_service, "_ENCRYPTION_KEY_CACHE", None)

    resolved = credential_service.get_encryption_key()

    assert resolved == valid_key
    assert (tmp_path / ".encryption_key").read_bytes() == valid_key
