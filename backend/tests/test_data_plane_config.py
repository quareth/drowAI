"""Tests for data plane configuration parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.data_plane import get_data_plane_config
from backend.config.workspace_config import WorkspaceConfig


def test_data_plane_config_defaults_preserve_local_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_PLANE_OBJECT_STORE_BACKEND", raising=False)
    monkeypatch.delenv("DATA_PLANE_LOCAL_OBJECT_STORE_ROOT", raising=False)

    config = get_data_plane_config()

    assert config.object_store_backend == "local"
    assert config.local_object_store_root == WorkspaceConfig.get_project_root() / "agent" / "object_store"


def test_data_plane_config_requires_bucket_for_non_local_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.delenv("DATA_PLANE_OBJECT_STORE_BUCKET", raising=False)

    with pytest.raises(ValueError) as error:
        get_data_plane_config()

    assert "DATA_PLANE_OBJECT_STORE_BUCKET" in str(error.value)


def test_data_plane_config_log_fields_are_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_PLANE_LOCAL_OBJECT_STORE_ROOT", str(tmp_path / "private-root"))
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BUCKET", "tenant-sensitive-bucket")
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_PREFIX", "tenant/a")

    config = get_data_plane_config()
    fields = config.to_log_fields()

    assert fields["local_object_store_root"] == "<configured>"
    assert fields["object_store_bucket"] == "<SET>"
    assert "tenant-sensitive-bucket" not in str(fields)
    assert str(tmp_path) not in str(fields)
    assert "http://" not in str(fields)
    assert "https://" not in str(fields)
