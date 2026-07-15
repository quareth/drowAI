"""Shared runtime workspace file declarations and materialization helpers."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from runtime_shared.workspace_filesystem import (
    WorkspaceFilesystem,
    WorkspacePathError,
    normalize_workspace_relative_path as _normalize_workspace_relative_path,
)


MAX_WORKSPACE_FILES_PER_COMMAND = 32
MAX_WORKSPACE_DIRECTORIES_PER_COMMAND = 64
MAX_WORKSPACE_FILE_BYTES = 1024 * 1024
WORKSPACE_FILE_MODE_WRITE = "write"
_WORKSPACE_FILE_ALLOWED_FIELDS = frozenset(
    {"relative_path", "content_base64", "mode", "description"}
)
_WORKSPACE_DIRECTORY_ALLOWED_FIELDS = frozenset({"relative_path", "description"})


class RuntimeWorkspaceFileError(ValueError):
    """Raised when a runtime workspace file declaration is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceFile:
    """One file that must be materialized in a task runtime workspace."""

    relative_path: str
    content_base64: str
    mode: Literal["write"] = WORKSPACE_FILE_MODE_WRITE
    description: str | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        relative_path: str,
        content: bytes,
        description: str | None = None,
    ) -> "RuntimeWorkspaceFile":
        """Build a workspace file declaration from bytes."""

        if len(content) > MAX_WORKSPACE_FILE_BYTES:
            raise RuntimeWorkspaceFileError("runtime workspace file exceeds maximum size")
        return cls(
            relative_path=normalize_workspace_relative_path(relative_path),
            content_base64=base64.b64encode(content).decode("ascii"),
            description=description,
        )

    @classmethod
    def from_text(
        cls,
        *,
        relative_path: str,
        content: str,
        encoding: str = "utf-8",
        description: str | None = None,
    ) -> "RuntimeWorkspaceFile":
        """Build a workspace file declaration from text."""

        return cls.from_bytes(
            relative_path=relative_path,
            content=content.encode(encoding),
            description=description,
        )

    def content_bytes(self) -> bytes:
        """Return decoded file content bytes after size validation."""

        try:
            data = base64.b64decode(self.content_base64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise RuntimeWorkspaceFileError("content_base64 must be valid base64") from exc
        if len(data) > MAX_WORKSPACE_FILE_BYTES:
            raise RuntimeWorkspaceFileError("runtime workspace file exceeds maximum size")
        return data

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for protocol transport."""

        payload = asdict(self)
        if payload.get("description") is None:
            payload.pop("description", None)
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceDirectory:
    """One directory that must exist in a task runtime workspace."""

    relative_path: str
    description: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for protocol transport."""

        payload = asdict(self)
        if payload.get("description") is None:
            payload.pop("description", None)
        return payload


@dataclass(frozen=True, slots=True)
class RuntimeWorkspacePreparation:
    """Runtime workspace files and directories required before execution."""

    files: tuple[RuntimeWorkspaceFile, ...] = ()
    directories: tuple[RuntimeWorkspaceDirectory, ...] = ()


def normalize_workspace_relative_path(value: Any) -> str:
    """Return a normalized workspace-relative path or raise on unsafe input."""
    try:
        return _normalize_workspace_relative_path(value)
    except WorkspacePathError as exc:
        raise RuntimeWorkspaceFileError(str(exc)) from exc


def normalize_runtime_workspace_files(value: Any) -> tuple[RuntimeWorkspaceFile, ...]:
    """Validate and normalize runtime workspace file declarations."""

    if value in (None, ""):
        return ()
    if isinstance(value, RuntimeWorkspaceFile):
        items: Iterable[Any] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        items = value
    else:
        raise RuntimeWorkspaceFileError("workspace_files must be a list")

    normalized: list[RuntimeWorkspaceFile] = []
    seen_paths: set[str] = set()
    for raw in items:
        item = _coerce_runtime_workspace_file(raw)
        if item.relative_path in seen_paths:
            raise RuntimeWorkspaceFileError(
                f"duplicate runtime workspace file path: {item.relative_path}"
            )
        seen_paths.add(item.relative_path)
        normalized.append(item)
        if len(normalized) > MAX_WORKSPACE_FILES_PER_COMMAND:
            raise RuntimeWorkspaceFileError("too many runtime workspace files")
    return tuple(normalized)


def normalize_runtime_workspace_directories(
    value: Any,
) -> tuple[RuntimeWorkspaceDirectory, ...]:
    """Validate and normalize runtime workspace directory declarations."""

    if value in (None, ""):
        return ()
    if isinstance(value, RuntimeWorkspaceDirectory):
        items: Iterable[Any] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        items = value
    else:
        raise RuntimeWorkspaceFileError("workspace_directories must be a list")

    normalized: list[RuntimeWorkspaceDirectory] = []
    seen_paths: set[str] = set()
    for raw in items:
        item = _coerce_runtime_workspace_directory(raw)
        if item.relative_path in seen_paths:
            raise RuntimeWorkspaceFileError(
                f"duplicate runtime workspace directory path: {item.relative_path}"
            )
        seen_paths.add(item.relative_path)
        normalized.append(item)
        if len(normalized) > MAX_WORKSPACE_DIRECTORIES_PER_COMMAND:
            raise RuntimeWorkspaceFileError("too many runtime workspace directories")
    return tuple(normalized)


def normalize_runtime_workspace_preparation(
    *,
    files: Any = (),
    directories: Any = (),
) -> RuntimeWorkspacePreparation:
    """Validate and normalize runtime workspace preparation declarations."""

    return RuntimeWorkspacePreparation(
        files=normalize_runtime_workspace_files(files),
        directories=normalize_runtime_workspace_directories(directories),
    )


def runtime_workspace_files_to_payload(
    files: Iterable[RuntimeWorkspaceFile],
) -> list[dict[str, Any]]:
    """Serialize workspace file declarations for JSON transport."""

    return [file.to_payload() for file in normalize_runtime_workspace_files(tuple(files))]


def runtime_workspace_directories_to_payload(
    directories: Iterable[RuntimeWorkspaceDirectory],
) -> list[dict[str, Any]]:
    """Serialize workspace directory declarations for JSON transport."""

    return [
        directory.to_payload()
        for directory in normalize_runtime_workspace_directories(tuple(directories))
    ]


def materialize_runtime_workspace_directories(
    *,
    workspace: str | Path,
    directories: Iterable[RuntimeWorkspaceDirectory],
) -> list[str]:
    """Create declared directories under a runtime workspace."""

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    filesystem = WorkspaceFilesystem(root)
    materialized: list[str] = []
    for item in normalize_runtime_workspace_directories(tuple(directories)):
        relative_path = normalize_workspace_relative_path(item.relative_path)
        filesystem.mkdirs(relative_path, mode=0o755)
        materialized.append(relative_path)
    return materialized


def materialize_runtime_workspace_files(
    *,
    workspace: str | Path,
    files: Iterable[RuntimeWorkspaceFile],
) -> list[str]:
    """Atomically write declared files under a runtime workspace."""

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    filesystem = WorkspaceFilesystem(root)
    materialized: list[str] = []
    for item in normalize_runtime_workspace_files(tuple(files)):
        if item.mode != WORKSPACE_FILE_MODE_WRITE:
            raise RuntimeWorkspaceFileError("unsupported runtime workspace file mode")
        relative_path = normalize_workspace_relative_path(item.relative_path)
        filesystem.write_bytes_atomic(
            relative_path,
            item.content_bytes(),
            mode=0o644,
        )
        materialized.append(relative_path)
    return materialized


def materialize_runtime_workspace_preparation(
    *,
    workspace: str | Path,
    files: Iterable[RuntimeWorkspaceFile] = (),
    directories: Iterable[RuntimeWorkspaceDirectory] = (),
) -> RuntimeWorkspacePreparation:
    """Materialize declared directories and files under a runtime workspace."""

    preparation = normalize_runtime_workspace_preparation(
        files=tuple(files),
        directories=tuple(directories),
    )
    materialize_runtime_workspace_directories(
        workspace=workspace,
        directories=preparation.directories,
    )
    materialize_runtime_workspace_files(workspace=workspace, files=preparation.files)
    return preparation


def _coerce_runtime_workspace_file(value: Any) -> RuntimeWorkspaceFile:
    if isinstance(value, RuntimeWorkspaceFile):
        return RuntimeWorkspaceFile(
            relative_path=normalize_workspace_relative_path(value.relative_path),
            content_base64=value.content_base64,
            mode=value.mode,
            description=value.description,
        )
    if not isinstance(value, Mapping):
        raise RuntimeWorkspaceFileError("runtime workspace file must be an object")
    unknown = set(value) - _WORKSPACE_FILE_ALLOWED_FIELDS
    if unknown:
        raise RuntimeWorkspaceFileError(
            f"unknown runtime workspace file field(s): {', '.join(sorted(unknown))}"
        )
    mode = str(value.get("mode") or WORKSPACE_FILE_MODE_WRITE).strip().lower()
    if mode != WORKSPACE_FILE_MODE_WRITE:
        raise RuntimeWorkspaceFileError("unsupported runtime workspace file mode")
    description = value.get("description")
    return RuntimeWorkspaceFile(
        relative_path=normalize_workspace_relative_path(value.get("relative_path")),
        content_base64=str(value.get("content_base64") or ""),
        mode=WORKSPACE_FILE_MODE_WRITE,
        description=str(description) if description is not None else None,
    )


def _coerce_runtime_workspace_directory(value: Any) -> RuntimeWorkspaceDirectory:
    if isinstance(value, RuntimeWorkspaceDirectory):
        return RuntimeWorkspaceDirectory(
            relative_path=normalize_workspace_relative_path(value.relative_path),
            description=value.description,
        )
    if not isinstance(value, Mapping):
        raise RuntimeWorkspaceFileError("runtime workspace directory must be an object")
    unknown = set(value) - _WORKSPACE_DIRECTORY_ALLOWED_FIELDS
    if unknown:
        raise RuntimeWorkspaceFileError(
            f"unknown runtime workspace directory field(s): {', '.join(sorted(unknown))}"
        )
    description = value.get("description")
    return RuntimeWorkspaceDirectory(
        relative_path=normalize_workspace_relative_path(value.get("relative_path")),
        description=str(description) if description is not None else None,
    )


__all__ = [
    "MAX_WORKSPACE_DIRECTORIES_PER_COMMAND",
    "MAX_WORKSPACE_FILE_BYTES",
    "MAX_WORKSPACE_FILES_PER_COMMAND",
    "RuntimeWorkspaceDirectory",
    "RuntimeWorkspaceFile",
    "RuntimeWorkspaceFileError",
    "RuntimeWorkspacePreparation",
    "WORKSPACE_FILE_MODE_WRITE",
    "materialize_runtime_workspace_directories",
    "materialize_runtime_workspace_files",
    "materialize_runtime_workspace_preparation",
    "normalize_runtime_workspace_directories",
    "normalize_runtime_workspace_files",
    "normalize_runtime_workspace_preparation",
    "normalize_workspace_relative_path",
    "runtime_workspace_directories_to_payload",
    "runtime_workspace_files_to_payload",
]
