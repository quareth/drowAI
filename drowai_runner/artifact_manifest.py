"""Runner artifact manifest scanner for data_plane upload promotion.

This module normalizes file-comm artifact references into workspace-local
manifest items, computes deterministic file metadata (size/hash/type), and
emits bounded warnings for skipped paths without importing backend modules.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from runtime_shared.runner_protocol import (
    RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS,
    RunnerArtifactManifestItem,
)
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspaceFilesystem,
    WorkspacePathError,
    normalize_workspace_relative_path,
)

_WORKSPACE_MOUNT_ROOT = PurePosixPath("/workspace")
_DEFAULT_ARTIFACT_KIND = "file"
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_TEXT_LIKE_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-ndjson",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactScanWarning:
    """Bounded warning payload for one skipped or rejected artifact reference."""

    code: str
    message: str
    artifact_ref: str

    def to_json(self) -> dict[str, str]:
        """Return a JSON-safe warning representation for tool-result metadata."""
        return {
            "code": self.code,
            "message": self.message,
            "artifact_ref": self.artifact_ref,
        }


@dataclass(frozen=True, slots=True)
class ScannedArtifactFile:
    """Runner-local file resolved from one normalized manifest item."""

    artifact_client_id: str
    relative_path: str
    absolute_path: Path
    workspace_root: Path | None
    size_bytes: int
    content_sha256: str
    content_type: str
    is_text: bool


@dataclass(frozen=True, slots=True)
class ArtifactManifestScanResult:
    """Result of scanning runner artifact references for manifest upload."""

    manifest_items: tuple[RunnerArtifactManifestItem, ...]
    files_by_client_id: dict[str, ScannedArtifactFile]
    warnings: tuple[ArtifactScanWarning, ...]
    warnings_truncated_count: int
    skipped_count: int

    def warnings_json(self) -> list[dict[str, str]]:
        """Serialize warning entries for metadata projection."""
        return [warning.to_json() for warning in self.warnings]


def scan_runner_artifacts_for_manifest(
    *,
    workspace_path: Path,
    artifacts: Sequence[object],
    max_items: int = RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS,
    max_warnings: int = 16,
) -> ArtifactManifestScanResult:
    """Normalize and scan artifact references into data_plane manifest items."""
    resolved_workspace = workspace_path.absolute()
    filesystem = WorkspaceFilesystem(resolved_workspace)
    manifest_items: list[RunnerArtifactManifestItem] = []
    files_by_client_id: dict[str, ScannedArtifactFile] = {}
    warnings: list[ArtifactScanWarning] = []
    warnings_truncated_count = 0
    skipped_count = 0

    if max_items < 1:
        max_items = RUNNER_ARTIFACT_MANIFEST_MAX_ITEMS
    capped_candidates = list(artifacts[:max_items])
    if len(artifacts) > max_items:
        skipped_count += len(artifacts) - max_items
        warnings_truncated_count = _append_warning(
            warnings,
            ArtifactScanWarning(
                code="artifact_manifest_limit_exceeded",
                message=f"Only first {max_items} artifact references were scanned.",
                artifact_ref=f"count={len(artifacts)}",
            ),
            max_warnings=max_warnings,
            warnings_truncated_count=warnings_truncated_count,
        )

    for index, candidate in enumerate(capped_candidates):
        normalized = _normalize_artifact_reference(candidate, index=index)
        if normalized is None:
            skipped_count += 1
            warnings_truncated_count = _append_warning(
                warnings,
                ArtifactScanWarning(
                    code="artifact_reference_invalid",
                    message="Artifact reference must be a non-empty path string or mapping.",
                    artifact_ref=_as_warning_ref(candidate),
                ),
                max_warnings=max_warnings,
                warnings_truncated_count=warnings_truncated_count,
            )
            continue
        relative_path_raw, artifact_kind, artifact_client_id = normalized
        relative_path = _normalize_workspace_artifact_path(
            workspace_root=resolved_workspace,
            artifact_path=relative_path_raw,
        )
        if relative_path is None:
            skipped_count += 1
            warnings_truncated_count = _append_warning(
                warnings,
                ArtifactScanWarning(
                    code="artifact_path_outside_workspace",
                    message="Artifact path is outside the task workspace and was skipped.",
                    artifact_ref=relative_path_raw,
                ),
                max_warnings=max_warnings,
                warnings_truncated_count=warnings_truncated_count,
            )
            continue
        try:
            entry = filesystem.metadata(relative_path, digest=True)
            content_prefix = filesystem.read_prefix(relative_path, max_bytes=4096)
        except FileNotFoundError:
            skipped_count += 1
            warnings_truncated_count = _append_warning(
                warnings,
                ArtifactScanWarning(
                    code="artifact_file_missing",
                    message="Artifact file does not exist at scan time and was skipped.",
                    artifact_ref=relative_path,
                ),
                max_warnings=max_warnings,
                warnings_truncated_count=warnings_truncated_count,
            )
            continue
        except (WorkspaceEntryUnsafeError, WorkspacePathError):
            skipped_count += 1
            warnings_truncated_count = _append_warning(
                warnings,
                ArtifactScanWarning(
                    code="RUNNER_WORKSPACE_ENTRY_UNSAFE",
                    message="Artifact path is unsafe and was skipped.",
                    artifact_ref=relative_path,
                ),
                max_warnings=max_warnings,
                warnings_truncated_count=warnings_truncated_count,
            )
            continue

        if artifact_client_id in files_by_client_id:
            skipped_count += 1
            warnings_truncated_count = _append_warning(
                warnings,
                ArtifactScanWarning(
                    code="artifact_client_id_duplicate",
                    message="Artifact client id was duplicated and later entry was skipped.",
                    artifact_ref=artifact_client_id,
                ),
                max_warnings=max_warnings,
                warnings_truncated_count=warnings_truncated_count,
            )
            continue

        size_bytes = entry.size
        content_sha256 = entry.digest or ""
        content_type = _guess_content_type(relative_path)
        is_text = _detect_text_content(content_prefix, content_type=content_type)

        metadata = {
            "source": "file_comm_result",
            "scan_relative_path": relative_path,
        }
        manifest_item = RunnerArtifactManifestItem(
            artifact_client_id=artifact_client_id,
            relative_path=relative_path,
            artifact_kind=artifact_kind,
            size_bytes=size_bytes,
            content_sha256=content_sha256,
            content_type=content_type,
            is_text=is_text,
            created_at=None,
            metadata=metadata,
        )
        scanned = ScannedArtifactFile(
            artifact_client_id=artifact_client_id,
            relative_path=relative_path,
            absolute_path=resolved_workspace / relative_path,
            workspace_root=resolved_workspace,
            size_bytes=size_bytes,
            content_sha256=content_sha256,
            content_type=content_type,
            is_text=is_text,
        )
        manifest_items.append(manifest_item)
        files_by_client_id[artifact_client_id] = scanned

    return ArtifactManifestScanResult(
        manifest_items=tuple(manifest_items),
        files_by_client_id=files_by_client_id,
        warnings=tuple(warnings),
        warnings_truncated_count=warnings_truncated_count,
        skipped_count=skipped_count,
    )


def _normalize_artifact_reference(
    candidate: object,
    *,
    index: int,
) -> tuple[str, str, str] | None:
    if isinstance(candidate, str):
        text = candidate.strip()
        if not text:
            return None
        return text, _DEFAULT_ARTIFACT_KIND, _build_artifact_client_id(index=index, relative_path=text)
    if not isinstance(candidate, Mapping):
        return None
    path_value = (
        str(
            candidate.get("relative_path")
            or candidate.get("path")
            or candidate.get("artifact_path")
            or ""
        ).strip()
    )
    if not path_value:
        return None
    artifact_kind = str(candidate.get("artifact_kind") or _DEFAULT_ARTIFACT_KIND).strip() or _DEFAULT_ARTIFACT_KIND
    artifact_client_id = str(candidate.get("artifact_client_id") or "").strip() or _build_artifact_client_id(
        index=index,
        relative_path=path_value,
    )
    return path_value, artifact_kind, artifact_client_id


def _normalize_workspace_artifact_path(
    *,
    workspace_root: Path,
    artifact_path: str,
) -> str | None:
    raw_path = artifact_path.strip()
    if not raw_path:
        return None
    posix_raw = raw_path.replace("\\", "/")
    if posix_raw.startswith("/workspace/") or posix_raw == "/workspace":
        relative = PurePosixPath(posix_raw).relative_to(_WORKSPACE_MOUNT_ROOT)
        candidate = relative.as_posix()
    else:
        path = Path(posix_raw)
        if path.is_absolute():
            try:
                candidate = path.relative_to(workspace_root).as_posix()
            except ValueError:
                return None
        else:
            candidate = posix_raw
    try:
        return normalize_workspace_relative_path(candidate)
    except WorkspacePathError:
        return None


def _guess_content_type(relative_path: str) -> str:
    guessed, _ = mimetypes.guess_type(relative_path)
    normalized = str(guessed or "").strip().lower()
    return normalized or _DEFAULT_CONTENT_TYPE


def _detect_text_content(content: bytes, *, content_type: str) -> bool:
    if content_type.startswith("text/") or content_type in _TEXT_LIKE_CONTENT_TYPES:
        return True
    if content_type.startswith(("image/", "audio/", "video/")):
        return False
    head = content[:4096]
    return b"\x00" not in head


def _build_artifact_client_id(*, index: int, relative_path: str) -> str:
    relative_bytes = relative_path.encode("utf-8", errors="replace")
    short_hash = hashlib.sha256(relative_bytes).hexdigest()[:10]
    return f"artifact-{index + 1}-{short_hash}"


def _append_warning(
    warnings: list[ArtifactScanWarning],
    warning: ArtifactScanWarning,
    *,
    max_warnings: int,
    warnings_truncated_count: int,
) -> int:
    if len(warnings) >= max(1, max_warnings):
        return warnings_truncated_count + 1
    warnings.append(warning)
    return warnings_truncated_count


def _as_warning_ref(value: object) -> str:
    if isinstance(value, Mapping):
        keys = ",".join(sorted(str(key) for key in value.keys()))
        return f"mapping:{keys}"
    return str(value)
