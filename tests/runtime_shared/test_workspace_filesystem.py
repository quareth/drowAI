"""Regression tests for descriptor-anchored task workspace operations."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import stat
import time
import zipfile
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspaceFilesystem,
    WorkspacePathError,
    normalize_workspace_relative_path,
)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, WorkspaceFilesystem]:
    """Return a private workspace root and its safe filesystem capability."""

    root = tmp_path / "workspace"
    root.mkdir()
    return root, WorkspaceFilesystem(root)


def _locked_append_worker(
    root: str,
    started: multiprocessing.Event,
    elapsed: Connection,
) -> None:
    """Append in another process and report how long lock acquisition took."""

    started.set()
    before = time.monotonic()
    WorkspaceFilesystem(root).append_bytes_locked(
        "events.jsonl", "events.lock", b"child\n"
    )
    elapsed.send(time.monotonic() - before)
    elapsed.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("reports/a.txt", "reports/a.txt"), ("/workspace/a.txt", "a.txt")],
)
def test_normalizes_workspace_paths(value: str, expected: str) -> None:
    assert normalize_workspace_relative_path(value) == expected


@pytest.mark.parametrize("value", ["", "/etc/passwd", "../secret", "a/../b", "a//b"])
def test_rejects_escaping_or_malformed_paths(value: str) -> None:
    with pytest.raises(WorkspacePathError):
        normalize_workspace_relative_path(value)


def test_read_hash_metadata_and_snapshot_regular_file(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    content = b"safe workspace content"
    (root / "report.txt").write_bytes(content)

    assert filesystem.read_bytes("report.txt", max_bytes=len(content)) == content
    assert filesystem.hash_file("report.txt") == hashlib.sha256(content).hexdigest()
    entry = filesystem.metadata("report.txt", digest=True)
    assert entry.relative_path == "report.txt"
    assert entry.kind == "file"
    assert entry.size == len(content)
    assert entry.digest == hashlib.sha256(content).hexdigest()

    snapshot = filesystem.snapshot_to_temp("report.txt", directory=tmp_path)
    try:
        assert snapshot.read_bytes() == content
        assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    finally:
        snapshot.unlink()


def test_read_rejects_symlink_and_special_file(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    outside = tmp_path / "outside.txt"
    outside.write_text("canary")
    (root / "link").symlink_to(outside)

    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.read_bytes("link")

    fifo = root / "pipe"
    os.mkfifo(fifo)
    try:
        with pytest.raises(WorkspaceEntryUnsafeError):
            filesystem.read_bytes("pipe")
    finally:
        fifo.unlink()


def test_append_rejects_fifo_without_waiting_for_a_reader(
    workspace: tuple[Path, WorkspaceFilesystem],
) -> None:
    root, filesystem = workspace
    fifo = root / "events.jsonl"
    os.mkfifo(fifo)

    started = time.monotonic()
    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.append_bytes("events.jsonl", b"record\n")

    assert time.monotonic() - started < 1.0


def test_atomic_replace_replaces_symlink_not_target(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"canary")
    destination = root / "vpn" / "task.ovpn"
    destination.parent.mkdir()
    destination.symlink_to(outside)

    filesystem.write_bytes_atomic("vpn/task.ovpn", b"new config")

    assert outside.read_bytes() == b"canary"
    assert destination.read_bytes() == b"new config"
    assert not destination.is_symlink()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_append_rejects_file_and_parent_symlinks(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"canary")
    (root / "input.jsonl").symlink_to(outside)

    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.append_bytes("input.jsonl", b'{"value": 1}\n')
    assert outside.read_bytes() == b"canary"

    (root / "input.jsonl").unlink()
    (root / "control").symlink_to(tmp_path)
    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.append_bytes("control/input.jsonl", b"unsafe\n")
    assert not (tmp_path / "input.jsonl").exists()


def test_atomic_append_and_directory_creation(
    workspace: tuple[Path, WorkspaceFilesystem],
) -> None:
    root, filesystem = workspace
    filesystem.mkdirs("nested/results")
    filesystem.append_bytes("nested/results/events.jsonl", b"one\n")
    filesystem.append_bytes("nested/results/events.jsonl", b"two\n")

    output = root / "nested" / "results" / "events.jsonl"
    assert output.read_bytes() == b"one\ntwo\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700


def test_locked_append_rejects_symlinked_data_and_lock(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"canary")
    (root / "events.jsonl").symlink_to(outside)

    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.append_bytes_locked("events.jsonl", "events.lock", b"unsafe\n")
    assert outside.read_bytes() == b"canary"

    (root / "events.jsonl").unlink()
    (root / "events.lock").unlink()
    (root / "events.lock").symlink_to(outside)
    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.append_bytes_locked("events.jsonl", "events.lock", b"unsafe\n")
    assert not (root / "events.jsonl").exists()
    assert outside.read_bytes() == b"canary"


def test_locked_append_serializes_across_processes(
    workspace: tuple[Path, WorkspaceFilesystem],
) -> None:
    root, filesystem = workspace
    filesystem.append_bytes_locked("events.jsonl", "events.lock", b"parent\n")
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    elapsed, child_elapsed = context.Pipe(duplex=False)

    import fcntl

    lock_fd = os.open(root / "events.lock", os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    process = context.Process(
        target=_locked_append_worker, args=(str(root), started, child_elapsed)
    )
    process.start()
    child_elapsed.close()
    assert started.wait(timeout=2)
    time.sleep(0.2)
    assert process.is_alive()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    process.join(timeout=3)

    assert process.exitcode == 0
    assert elapsed.poll(timeout=1)
    assert elapsed.recv() >= 0.15
    elapsed.close()
    process.close()
    assert (root / "events.jsonl").read_bytes() == b"parent\nchild\n"


def test_recursive_listing_rejects_nested_symlink(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    (root / "reports").mkdir()
    (root / "reports" / "safe.txt").write_text("safe")
    (root / "reports" / "escape").symlink_to(tmp_path)

    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.list_entries("reports", recursive=True)


def test_zip_streams_regular_files_and_rejects_nested_symlink(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    reports = root / "reports"
    reports.mkdir()
    (reports / "safe.txt").write_bytes(b"safe")

    archive = filesystem.create_zip(["reports"])
    try:
        with zipfile.ZipFile(archive) as opened:
            assert opened.read("reports/safe.txt") == b"safe"
    finally:
        archive.unlink()

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"canary")
    (reports / "escape.txt").symlink_to(outside)
    destination = tmp_path / "existing.zip"
    destination.write_bytes(b"existing")

    with pytest.raises(WorkspaceEntryUnsafeError):
        filesystem.create_zip(["reports"], destination=destination)
    assert destination.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".drowai-workspace-*.zip"))


def test_recursive_remove_unlinks_symlink_without_touching_target(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path
) -> None:
    root, filesystem = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("safe")
    tree = root / "tree"
    tree.mkdir()
    (tree / "regular.txt").write_text("delete")
    (tree / "escape").symlink_to(outside)

    filesystem.remove("tree", recursive=True)

    assert not tree.exists()
    assert canary.read_text() == "safe"


def test_open_descriptor_is_not_redirected_after_path_swap(
    workspace: tuple[Path, WorkspaceFilesystem], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, filesystem = workspace
    source = root / "source.txt"
    source.write_bytes(b"original")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"canary")
    real_read = os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(root / "old-source.txt")
            source.symlink_to(outside)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", swapping_read)
    assert filesystem.read_bytes("source.txt") == b"original"
