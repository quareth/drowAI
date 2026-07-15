"""Race-safe host filesystem operations scoped to one task workspace.

This module is the shared trust-boundary primitive for control-plane and runner
code that operates on runtime-writable task directories.  It resolves paths
relative to directory descriptors, never follows symlinks, accepts only regular
files and directories for reads, and keeps validation and I/O on the same open
file descriptor.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence


class WorkspacePathError(ValueError):
    """Raised when a caller supplies a malformed or escaping workspace path."""


class WorkspaceEntryUnsafeError(ValueError):
    """Raised when a workspace entry is a symlink, special file, or changed path."""


class WorkspaceFilesystemUnsupportedError(RuntimeError):
    """Raised when the host cannot provide required descriptor-relative safety."""


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    """Safe metadata for one workspace-relative regular file or directory."""

    relative_path: str
    kind: Literal["file", "directory"]
    size: int
    modified_at: float
    digest: str | None = None


def normalize_workspace_relative_path(value: Any) -> str:
    """Return a normalized workspace-relative path without filesystem access."""

    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise WorkspacePathError("workspace path must not be empty")
    if text.startswith("/workspace/"):
        text = text[len("/workspace/") :]
    if text.startswith("/"):
        raise WorkspacePathError("workspace path must be relative")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkspacePathError("workspace path must stay inside the workspace")
    return "/".join(parts)


class WorkspaceFilesystem:
    """Perform descriptor-anchored operations beneath one trusted workspace root."""

    _DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._ensure_supported()

    @staticmethod
    def _ensure_supported() -> None:
        required = ("O_DIRECTORY", "O_NONBLOCK", "O_NOFOLLOW", "O_CLOEXEC")
        if any(not hasattr(os, name) for name in required):
            raise WorkspaceFilesystemUnsupportedError(
                "descriptor-relative workspace operations are unavailable"
            )
        if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
            raise WorkspaceFilesystemUnsupportedError(
                "descriptor-relative workspace operations are unavailable"
            )

    @contextmanager
    def _root_descriptor(self) -> Iterator[int]:
        try:
            descriptor = os.open(self.root, self._DIR_FLAGS)
        except OSError as exc:
            self._raise_safe_oserror(exc)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise WorkspaceEntryUnsafeError("workspace root is not a directory")
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _parent_descriptor(
        self, relative_path: str, *, create: bool = False, directory_mode: int = 0o700
    ) -> Iterator[tuple[int, str]]:
        normalized = normalize_workspace_relative_path(relative_path)
        parts = normalized.split("/")
        with self._root_descriptor() as root_fd:
            current_fd = os.dup(root_fd)
            try:
                for component in parts[:-1]:
                    next_fd = self._open_directory_component(
                        current_fd,
                        component,
                        create=create,
                        mode=directory_mode,
                    )
                    os.close(current_fd)
                    current_fd = next_fd
                yield current_fd, parts[-1]
            finally:
                os.close(current_fd)

    def _open_directory_component(
        self, parent_fd: int, component: str, *, create: bool, mode: int
    ) -> int:
        try:
            return os.open(component, self._DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(component, mode=mode, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                return os.open(component, self._DIR_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                self._raise_safe_oserror(exc)
        except OSError as exc:
            self._raise_safe_oserror(exc)

    @staticmethod
    def _raise_safe_oserror(exc: OSError) -> None:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EINVAL, errno.ENXIO}:
            raise WorkspaceEntryUnsafeError(
                "workspace entry is unsafe or changed during access"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError("workspace entry does not exist") from exc
        raise OSError(exc.errno, "workspace filesystem operation failed") from exc

    @staticmethod
    def _require_regular(descriptor: int) -> os.stat_result:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceEntryUnsafeError("workspace entry is not a regular file")
        return metadata

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("workspace write made no progress")
            view = view[written:]

    @contextmanager
    def _regular_file_descriptor(self, relative_path: str) -> Iterator[tuple[int, os.stat_result]]:
        with self._parent_descriptor(relative_path) as (parent_fd, name):
            try:
                descriptor = os.open(name, self._READ_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            try:
                metadata = self._require_regular(descriptor)
                yield descriptor, metadata
            finally:
                os.close(descriptor)

    def mkdirs(self, relative_path: str, *, mode: int = 0o700) -> None:
        """Create a directory tree without following any existing symlink."""

        normalized = normalize_workspace_relative_path(relative_path)
        with self._root_descriptor() as root_fd:
            current_fd = os.dup(root_fd)
            try:
                for component in normalized.split("/"):
                    next_fd = self._open_directory_component(
                        current_fd, component, create=True, mode=mode
                    )
                    os.close(current_fd)
                    current_fd = next_fd
            finally:
                os.close(current_fd)

    def read_bytes(self, relative_path: str, *, max_bytes: int | None = None) -> bytes:
        """Read one regular file from the descriptor that was safety-checked."""

        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        with self._regular_file_descriptor(relative_path) as (descriptor, metadata):
            if max_bytes is not None and metadata.st_size > max_bytes:
                raise ValueError("workspace file exceeds maximum size")
            chunks: list[bytes] = []
            remaining = metadata.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

    def read_prefix(self, relative_path: str, *, max_bytes: int) -> bytes:
        """Read at most ``max_bytes`` from one validated regular-file descriptor."""

        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        with self._regular_file_descriptor(relative_path) as (descriptor, _):
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

    def metadata(self, relative_path: str, *, digest: bool = False) -> WorkspaceEntry:
        """Return metadata for one safe regular file."""

        normalized = normalize_workspace_relative_path(relative_path)
        with self._regular_file_descriptor(normalized) as (descriptor, item_stat):
            item_digest = self._hash_descriptor(descriptor) if digest else None
            return WorkspaceEntry(
                relative_path=normalized,
                kind="file",
                size=item_stat.st_size,
                modified_at=item_stat.st_mtime,
                digest=item_digest,
            )

    def hash_file(self, relative_path: str, *, algorithm: str = "sha256") -> str:
        """Hash a regular file through its already-validated descriptor."""

        with self._regular_file_descriptor(relative_path) as (descriptor, _):
            return self._hash_descriptor(descriptor, algorithm=algorithm)

    def chmod_file(self, relative_path: str, mode: int) -> None:
        """Apply permissions to one already-validated regular-file descriptor."""

        with self._regular_file_descriptor(relative_path) as (descriptor, _):
            os.fchmod(descriptor, mode)

    @staticmethod
    def _hash_descriptor(descriptor: int, *, algorithm: str = "sha256") -> str:
        digest = hashlib.new(algorithm)
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()

    def write_bytes_atomic(
        self,
        relative_path: str,
        content: bytes,
        *,
        mode: int = 0o600,
        create_parents: bool = True,
    ) -> None:
        """Atomically replace a file without following the destination path."""

        with self._parent_descriptor(relative_path, create=create_parents) as (parent_fd, name):
            temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
                self._write_all(descriptor, content)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.fsync(parent_fd)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass

    def append_bytes(
        self,
        relative_path: str,
        content: bytes,
        *,
        mode: int = 0o600,
        create_parents: bool = True,
    ) -> None:
        """Append bytes to a no-follow regular-file descriptor."""

        with self._parent_descriptor(relative_path, create=create_parents) as (parent_fd, name):
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_NONBLOCK
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                self._raise_safe_oserror(exc)
            try:
                self._require_regular(descriptor)
                self._write_all(descriptor, content)
            finally:
                os.close(descriptor)

    def append_bytes_locked(
        self,
        relative_path: str,
        lock_path: str,
        content: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        """Append while holding an exclusive no-follow cross-process file lock."""

        with self._locked_file(lock_path, mode=mode):
            self.append_bytes(relative_path, content, mode=mode)

    def read_bytes_locked(
        self,
        relative_path: str,
        lock_path: str,
        *,
        max_bytes: int | None = None,
        mode: int = 0o600,
    ) -> bytes:
        """Read a regular file while holding a descriptor-safe exclusive lock."""

        with self._locked_file(lock_path, mode=mode):
            return self.read_bytes(relative_path, max_bytes=max_bytes)

    @contextmanager
    def _locked_file(self, lock_path: str, *, mode: int) -> Iterator[None]:
        with self._parent_descriptor(lock_path, create=True) as (parent_fd, name):
            try:
                lock_fd = os.open(
                    name,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_NONBLOCK
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    mode,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                self._raise_safe_oserror(exc)
            try:
                self._require_regular(lock_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def snapshot_to_temp(
        self, relative_path: str, *, directory: str | os.PathLike[str] | None = None
    ) -> Path:
        """Copy one safe regular file into a trusted private temporary file."""

        descriptor, temporary_name = tempfile.mkstemp(prefix="drowai-workspace-", dir=directory)
        destination = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with self._regular_file_descriptor(relative_path) as (source_fd, _):
                while chunk := os.read(source_fd, 1024 * 1024):
                    self._write_all(descriptor, chunk)
            os.fsync(descriptor)
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)

    def list_entries(
        self, relative_path: str | None = None, *, recursive: bool = False
    ) -> tuple[WorkspaceEntry, ...]:
        """List safe regular files and directories, rejecting unsafe descendants."""

        with self._directory_descriptor(relative_path) as (directory_fd, prefix):
            entries: list[WorkspaceEntry] = []
            self._collect_entries(directory_fd, prefix, recursive, entries)
            return tuple(entries)

    def iter_entries(
        self,
        relative_path: str | None = None,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
    ) -> Iterator[WorkspaceEntry]:
        """Yield safe entries incrementally while keeping traversal descriptor-bound."""

        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be at least one")
        with self._directory_descriptor(relative_path) as (directory_fd, prefix):
            yield from self._iter_entries(
                directory_fd,
                prefix,
                recursive=recursive,
                depth=1,
                max_depth=max_depth,
            )

    def _iter_entries(
        self,
        directory_fd: int,
        prefix: str,
        *,
        recursive: bool,
        depth: int,
        max_depth: int | None,
    ) -> Iterator[WorkspaceEntry]:
        with os.scandir(directory_fd) as directory_entries:
            for directory_entry in directory_entries:
                name = directory_entry.name
                relative = f"{prefix}/{name}" if prefix else name
                try:
                    item_stat = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    self._raise_safe_oserror(exc)
                if stat.S_ISLNK(item_stat.st_mode):
                    raise WorkspaceEntryUnsafeError("workspace contains an unsafe symlink")
                if stat.S_ISDIR(item_stat.st_mode):
                    child_fd = self._open_directory_component(
                        directory_fd, name, create=False, mode=0o700
                    )
                    try:
                        opened_stat = os.fstat(child_fd)
                        yield WorkspaceEntry(relative, "directory", 0, opened_stat.st_mtime)
                        if recursive and (max_depth is None or depth < max_depth):
                            yield from self._iter_entries(
                                child_fd,
                                relative,
                                recursive=True,
                                depth=depth + 1,
                                max_depth=max_depth,
                            )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(item_stat.st_mode):
                    try:
                        file_fd = os.open(name, self._READ_FLAGS, dir_fd=directory_fd)
                    except OSError as exc:
                        self._raise_safe_oserror(exc)
                    try:
                        opened_stat = self._require_regular(file_fd)
                        yield WorkspaceEntry(
                            relative, "file", opened_stat.st_size, opened_stat.st_mtime
                        )
                    finally:
                        os.close(file_fd)
                else:
                    raise WorkspaceEntryUnsafeError("workspace contains a special file")

    @contextmanager
    def _directory_descriptor(
        self, relative_path: str | None
    ) -> Iterator[tuple[int, str]]:
        if relative_path is None:
            with self._root_descriptor() as root_fd:
                yield root_fd, ""
            return
        normalized = normalize_workspace_relative_path(relative_path)
        with self._parent_descriptor(normalized) as (parent_fd, name):
            try:
                descriptor = os.open(name, self._DIR_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            try:
                yield descriptor, normalized
            finally:
                os.close(descriptor)

    def _collect_entries(
        self,
        directory_fd: int,
        prefix: str,
        recursive: bool,
        output: list[WorkspaceEntry],
    ) -> None:
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{prefix}/{name}" if prefix else name
            try:
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            if stat.S_ISLNK(item_stat.st_mode):
                raise WorkspaceEntryUnsafeError("workspace contains an unsafe symlink")
            if stat.S_ISDIR(item_stat.st_mode):
                child_fd = self._open_directory_component(
                    directory_fd, name, create=False, mode=0o700
                )
                try:
                    opened_stat = os.fstat(child_fd)
                    output.append(
                        WorkspaceEntry(relative, "directory", 0, opened_stat.st_mtime)
                    )
                    if recursive:
                        self._collect_entries(child_fd, relative, True, output)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(item_stat.st_mode):
                try:
                    file_fd = os.open(name, self._READ_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    self._raise_safe_oserror(exc)
                try:
                    opened_stat = self._require_regular(file_fd)
                    output.append(
                        WorkspaceEntry(relative, "file", opened_stat.st_size, opened_stat.st_mtime)
                    )
                finally:
                    os.close(file_fd)
            else:
                raise WorkspaceEntryUnsafeError("workspace contains a special file")

    def create_zip(
        self,
        relative_paths: Sequence[str],
        *,
        destination: str | os.PathLike[str] | None = None,
    ) -> Path:
        """Create a ZIP by streaming safe descriptors; remove partial output on error."""

        if not relative_paths:
            raise WorkspacePathError("at least one workspace path is required")
        output = Path(destination) if destination is not None else None
        output_fd, output_name = tempfile.mkstemp(
            prefix=".drowai-workspace-" if output is not None else "drowai-workspace-",
            suffix=".zip",
            dir=output.parent if output is not None else None,
        )
        os.close(output_fd)
        temporary_output = Path(output_name)
        try:
            with zipfile.ZipFile(
                temporary_output, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for raw_path in relative_paths:
                    normalized = normalize_workspace_relative_path(raw_path)
                    self._add_to_zip(archive, normalized)
            if output is not None:
                os.replace(temporary_output, output)
                return output
            return temporary_output
        except BaseException:
            temporary_output.unlink(missing_ok=True)
            raise

    def _add_to_zip(self, archive: zipfile.ZipFile, relative_path: str) -> None:
        with self._parent_descriptor(relative_path) as (parent_fd, name):
            try:
                item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            if stat.S_ISLNK(item_stat.st_mode):
                raise WorkspaceEntryUnsafeError("workspace contains an unsafe symlink")
            if stat.S_ISREG(item_stat.st_mode):
                self._stream_zip_file(archive, parent_fd, name, relative_path)
                return
            if not stat.S_ISDIR(item_stat.st_mode):
                raise WorkspaceEntryUnsafeError("workspace contains a special file")
            directory_fd = self._open_directory_component(
                parent_fd, name, create=False, mode=0o700
            )
            try:
                self._stream_zip_directory(archive, directory_fd, relative_path)
            finally:
                os.close(directory_fd)

    def _stream_zip_directory(
        self, archive: zipfile.ZipFile, directory_fd: int, prefix: str
    ) -> None:
        names = sorted(os.listdir(directory_fd))
        if not names:
            archive.writestr(f"{prefix}/", b"")
            return
        for name in names:
            relative = f"{prefix}/{name}"
            try:
                item_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            if stat.S_ISLNK(item_stat.st_mode):
                raise WorkspaceEntryUnsafeError("workspace contains an unsafe symlink")
            if stat.S_ISREG(item_stat.st_mode):
                self._stream_zip_file(archive, directory_fd, name, relative)
            elif stat.S_ISDIR(item_stat.st_mode):
                child_fd = self._open_directory_component(
                    directory_fd, name, create=False, mode=0o700
                )
                try:
                    self._stream_zip_directory(archive, child_fd, relative)
                finally:
                    os.close(child_fd)
            else:
                raise WorkspaceEntryUnsafeError("workspace contains a special file")

    def _stream_zip_file(
        self, archive: zipfile.ZipFile, parent_fd: int, name: str, archive_name: str
    ) -> None:
        try:
            source_fd = os.open(name, self._READ_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            self._raise_safe_oserror(exc)
        try:
            self._require_regular(source_fd)
            with archive.open(archive_name, "w") as destination:
                while chunk := os.read(source_fd, 1024 * 1024):
                    destination.write(chunk)
        finally:
            os.close(source_fd)

    def remove(
        self, relative_path: str, *, recursive: bool = False, missing_ok: bool = False
    ) -> None:
        """Remove an entry without following links; recursive removal unlinks links."""

        try:
            with self._parent_descriptor(relative_path) as (parent_fd, name):
                self._remove_entry(parent_fd, name, recursive=recursive)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def _remove_entry(self, parent_fd: int, name: str, *, recursive: bool) -> None:
        try:
            item_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            self._raise_safe_oserror(exc)
        if stat.S_ISDIR(item_stat.st_mode):
            if not recursive:
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError as exc:
                    self._raise_safe_oserror(exc)
                return
            directory_fd = self._open_directory_component(
                parent_fd, name, create=False, mode=0o700
            )
            try:
                for child in os.listdir(directory_fd):
                    self._remove_entry(directory_fd, child, recursive=True)
            finally:
                os.close(directory_fd)
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as exc:
                self._raise_safe_oserror(exc)
            return
        try:
            os.unlink(name, dir_fd=parent_fd)
        except OSError as exc:
            self._raise_safe_oserror(exc)


__all__ = [
    "WorkspaceEntry",
    "WorkspaceEntryUnsafeError",
    "WorkspaceFilesystem",
    "WorkspaceFilesystemUnsupportedError",
    "WorkspacePathError",
    "normalize_workspace_relative_path",
]
