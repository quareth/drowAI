"""Focused tests for Data Plane local object-store abstraction behavior.

These tests validate that the object-store port and local implementation remain
key-scoped, root-confined, and suitable for just-in-time signed URL generation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.data_plane.registry import build_object_store


def test_local_object_store_put_read_head_delete_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    store = LocalObjectStore(root_path=root, signed_url_ttl_seconds=300)
    object_key = "tenant-1/task-2/execution-3/artifact.txt"
    payload = b"artifact-content"

    put_head = store.put_bytes(
        object_key,
        payload,
        content_type="text/plain",
        metadata={"origin": "pytest"},
    )

    assert put_head.object_key == object_key
    assert put_head.byte_size == len(payload)
    assert put_head.content_type == "text/plain"
    assert put_head.content_sha256 == sha256(payload).hexdigest()
    assert put_head.metadata == {"origin": "pytest"}

    assert store.read_bytes(object_key) == payload
    assert store.read_bytes(object_key, max_bytes=8) == payload[:8]

    head = store.head_object(object_key)
    assert head is not None
    assert head.object_key == object_key
    assert head.byte_size == len(payload)

    assert store.delete_object(object_key) is True
    assert store.head_object(object_key) is None
    assert store.delete_object(object_key) is False


@pytest.mark.parametrize(
    "object_key",
    [
        "",
        ".",
        "/etc/passwd",
        "../secret.txt",
        "artifacts/../../x",
        "artifact/\x00name.txt",
    ],
)
def test_local_object_store_rejects_traversal_or_invalid_keys(
    tmp_path: Path,
    object_key: str,
) -> None:
    store = LocalObjectStore(root_path=tmp_path / "objects")
    with pytest.raises(ValueError):
        store.put_bytes(object_key, b"denied")


def test_signed_targets_are_just_in_time_and_do_not_expose_host_paths(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 5, 25, 10, 0, 0, tzinfo=UTC)
    root = tmp_path / "private-host-path"
    store = LocalObjectStore(
        root_path=root,
        signed_url_ttl_seconds=120,
        clock=lambda: fixed_now,
    )

    upload = store.create_signed_upload(
        "tenant-1/task-2/report.txt",
        content_type="text/plain",
        metadata={"scope": "task"},
    )
    download = store.create_signed_download(
        "tenant-1/task-2/report.txt",
        response_filename="report.txt",
    )

    assert upload.method == "PUT"
    assert upload.object_key == "tenant-1/task-2/report.txt"
    assert upload.expires_at == datetime(2026, 5, 25, 10, 2, 0, tzinfo=UTC)
    assert upload.url.startswith("local-object://upload/tenant-1/task-2/report.txt?token=")
    assert str(root) not in upload.url
    assert upload.headers["content-type"] == "text/plain"
    assert upload.headers["x-meta-scope"] == "task"

    assert download.method == "GET"
    assert download.object_key == "tenant-1/task-2/report.txt"
    assert download.expires_at == datetime(2026, 5, 25, 10, 2, 0, tzinfo=UTC)
    assert download.url.startswith("local-object://download/tenant-1/task-2/report.txt?token=")
    assert str(root) not in download.url
    assert download.headers["content-disposition"] == 'attachment; filename="report.txt"'


def test_registry_builds_local_store_with_configured_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    object_root = tmp_path / "data-plane-object-root"
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "local")
    monkeypatch.setenv("DATA_PLANE_LOCAL_OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("DATA_PLANE_SIGNED_URL_TTL_SECONDS", "600")

    store = build_object_store()
    assert isinstance(store, LocalObjectStore)

    store.put_bytes("tenant-9/task-8/file.bin", b"abc")
    assert (object_root / "tenant-9" / "task-8" / "file.bin").exists()


def test_registry_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_PLANE_OBJECT_STORE_BACKEND", "unsupported")
    with pytest.raises(ValueError):
        build_object_store()
