"""Data-plane file browser for runner-cloud task artifacts.

Scope:
- Build task file tree/search/content/download responses from `execution_artifacts`.
- Read preview/download bytes from object storage or inline text, not runtime workspace paths.

Boundaries:
- Cloud-runner compatibility surface only; does not call runtime providers.
- Never exposes object keys or backend-local filesystem paths in API payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import tempfile
from typing import Any
import zipfile

from sqlalchemy.orm import Session

from backend.config.data_plane import DataPlaneConfig, get_data_plane_config
from backend.models.provenance import ExecutionArtifact
from backend.services.data_plane.artifact_read_service import ArtifactReadService
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from backend.services.workspace.file_browser_service import FileBrowserService

_NON_READY_UPLOAD_STATUSES = frozenset({"upload_pending", "upload_failed", "failed"})
_MAX_PREVIEW_BYTES = 200000


@dataclass(frozen=True, slots=True)
class _ArtifactFileRow:
    """Normalized artifact-backed virtual file row."""

    artifact_id: str
    relative_path: str
    byte_size: int
    modified: str
    upload_status: str
    mime_type: str | None
    is_text: bool
    content_text: str | None
    object_key: str | None


class ArtifactFileBrowserService:
    """Task-scoped virtual file browser backed by artifact metadata/object storage."""

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
        file_browser_service: FileBrowserService | None = None,
        object_read_service: ArtifactReadService | None = None,
        data_plane_config: DataPlaneConfig | None = None,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()
        self._file_browser_service = file_browser_service or FileBrowserService()
        self._object_read_service = object_read_service or ArtifactReadService(db, object_store=self._object_store)
        self._data_plane_config = data_plane_config or get_data_plane_config()
        self._max_file_download_size_bytes = int(self._data_plane_config.max_artifact_size_bytes)
        self._max_zip_download_size_bytes = int(self._data_plane_config.max_zip_download_size_bytes)

    def get_directory_tree(
        self,
        *,
        tenant_id: int,
        task_id: int,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Return a virtual file tree derived from task artifact metadata."""
        root = self._normalize_relative_path(path, allow_root=True)
        rows = self._load_artifact_file_rows(tenant_id=tenant_id, task_id=task_id)
        scoped_rows = self._rows_for_directory(
            rows=rows,
            directory_path=root,
            directory_error_message="Directory tree path must be a folder.",
        )

        tree: dict[str, Any] = {
            "name": Path(root).name if root else "workspace",
            "type": "folder",
            "path": "/" if not root else f"/{root}",
            "size": None,
            "modified": self._iso_now(),
            "children": [],
        }

        folders: dict[str, dict[str, Any]] = {"": tree}
        for row in sorted(scoped_rows, key=lambda item: item.relative_path):
            relative_to_root = (
                row.relative_path[len(root) + 1 :] if root else row.relative_path
            )
            parts = [part for part in relative_to_root.split("/") if part]
            if not parts:
                continue
            parent_key = ""
            parent_node = tree
            for part in parts[:-1]:
                parent_key = f"{parent_key}/{part}" if parent_key else part
                folder = folders.get(parent_key)
                if folder is None:
                    folder_path = f"/{root}/{parent_key}".replace("//", "/")
                    folder = {
                        "name": part,
                        "type": "folder",
                        "path": folder_path,
                        "size": None,
                        "modified": self._iso_now(),
                        "children": [],
                    }
                    parent_node["children"].append(folder)
                    folders[parent_key] = folder
                parent_node = folder
            file_path = "/" + row.relative_path
            parent_node["children"].append(
                {
                    "name": parts[-1],
                    "type": "file",
                    "path": file_path,
                    "size": row.byte_size,
                    "modified": row.modified,
                    "children": [],
                    "content_availability": self._content_availability(row),
                }
            )
        return tree

    def get_file_content(
        self,
        *,
        tenant_id: int,
        task_id: int,
        path: str,
    ) -> dict[str, Any]:
        """Return sanitized preview payload for one ready text artifact."""
        rows = self._load_artifact_file_rows(tenant_id=tenant_id, task_id=task_id)
        file_row = self._resolve_file_row(rows=rows, path=path)
        raw_content = self._resolve_text_preview_content(
            tenant_id=tenant_id,
            task_id=task_id,
            row=file_row,
        )
        return self._build_content_payload(
            file_path=file_row.relative_path,
            raw_content=raw_content,
            size=file_row.byte_size,
        )

    def resolve_download_path(
        self,
        *,
        tenant_id: int,
        task_id: int,
        path: str,
    ) -> Path:
        """Materialize one artifact-backed file to a bounded temporary download path."""
        rows = self._load_artifact_file_rows(tenant_id=tenant_id, task_id=task_id)
        file_row = self._resolve_file_row(rows=rows, path=path)
        payload = self._resolve_download_bytes(file_row)
        suffix = Path(file_row.relative_path).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(payload)
            return Path(handle.name)

    def create_zip_archive(
        self,
        *,
        tenant_id: int,
        task_id: int,
        file_paths: list[str],
    ) -> Path:
        """Build a bounded temporary ZIP from selected virtual files/directories."""
        if not file_paths:
            raise ValueError("At least one path is required to create an archive.")

        rows = self._load_artifact_file_rows(tenant_id=tenant_id, task_id=task_id)
        selected: dict[str, _ArtifactFileRow] = {}
        for raw_path in file_paths:
            normalized = self._normalize_relative_path(raw_path, allow_root=True)
            if normalized and normalized in rows:
                selected[normalized] = rows[normalized]
                continue

            prefix = f"{normalized}/" if normalized else ""
            descendants = [row for key, row in rows.items() if key.startswith(prefix)]
            if not descendants:
                raise FileNotFoundError(f"Path not found: {raw_path}")
            for row in descendants:
                selected[row.relative_path] = row

        total_size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
            zip_path = Path(handle.name)
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for row in sorted(selected.values(), key=lambda item: item.relative_path):
                    payload = self._resolve_download_bytes(row)
                    total_size += len(payload)
                    if total_size > self._max_zip_download_size_bytes:
                        raise ValueError("Selected files exceed total ZIP download size limit.")
                    archive.writestr(row.relative_path, payload)
            return zip_path
        except Exception:
            zip_path.unlink(missing_ok=True)
            raise

    def search_files(
        self,
        *,
        tenant_id: int,
        task_id: int,
        query: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Search virtual artifact paths by case-insensitive file name or path match."""
        query_lower = str(query or "").strip().lower()
        root = self._normalize_relative_path(path, allow_root=True)
        rows = self._load_artifact_file_rows(tenant_id=tenant_id, task_id=task_id)
        scoped_rows = self._rows_for_directory(
            rows=rows,
            directory_path=root,
            directory_error_message="Search path must be a directory.",
        )

        results: list[dict[str, Any]] = []
        for row in sorted(scoped_rows, key=lambda item: item.relative_path):
            name = Path(row.relative_path).name
            full_path = row.relative_path.lower()
            if query_lower and query_lower not in name.lower() and query_lower not in full_path:
                continue
            results.append(
                {
                    "name": name,
                    "type": "file",
                    "path": f"/{row.relative_path}",
                    "size": row.byte_size,
                    "modified": row.modified,
                    "content_availability": self._content_availability(row),
                }
            )
        return {
            "query": query,
            "results": results,
            "total_count": len(results),
            "truncated": False,
        }

    def _resolve_file_row(self, *, rows: dict[str, _ArtifactFileRow], path: str) -> _ArtifactFileRow:
        normalized = self._normalize_relative_path(path, allow_root=False)
        file_row = rows.get(normalized)
        if file_row is not None:
            return file_row

        prefix = f"{normalized}/"
        if any(candidate.startswith(prefix) for candidate in rows):
            raise ValueError("Path points to a directory, not a file.")
        raise FileNotFoundError(f"File not found: {path}")

    def _rows_for_directory(
        self,
        *,
        rows: dict[str, _ArtifactFileRow],
        directory_path: str,
        directory_error_message: str,
    ) -> list[_ArtifactFileRow]:
        if directory_path in rows:
            raise ValueError(directory_error_message)

        if not directory_path:
            return list(rows.values())

        prefix = f"{directory_path}/"
        scoped = [row for key, row in rows.items() if key.startswith(prefix)]
        if scoped:
            return scoped
        raise FileNotFoundError(f"Path not found: {directory_path}")

    def _resolve_text_preview_content(
        self,
        *,
        tenant_id: int,
        task_id: int,
        row: _ArtifactFileRow,
    ) -> str:
        if row.is_text is False:
            raise ValueError("Binary file previews are not available as text.")

        if row.content_text is not None:
            return row.content_text

        if row.upload_status in _NON_READY_UPLOAD_STATUSES:
            raise ValueError("File content is not available while upload is pending or failed.")

        result = self._object_read_service.read_artifact_text(
            task_id=task_id,
            artifact_id=row.artifact_id,
            tenant_id=tenant_id,
            max_bytes=_MAX_PREVIEW_BYTES,
        )
        if result.status != "ready" or result.content is None:
            raise ValueError("File content is not available for preview.")
        return result.content

    def _resolve_download_bytes(self, row: _ArtifactFileRow) -> bytes:
        if row.upload_status in _NON_READY_UPLOAD_STATUSES:
            raise ValueError("File download is not available while upload is pending or failed.")

        payload: bytes | None = None
        if row.object_key:
            payload = self._object_store.read_bytes(
                row.object_key,
                max_bytes=self._max_file_download_size_bytes + 1,
            )
        elif row.content_text is not None and row.is_text is not False:
            payload = row.content_text.encode("utf-8")

        if payload is None:
            raise ValueError("File download is not available for this artifact.")
        if len(payload) > self._max_file_download_size_bytes:
            raise ValueError("Selected file exceeds per-file download size limit.")
        return payload

    def _load_artifact_file_rows(self, *, tenant_id: int, task_id: int) -> dict[str, _ArtifactFileRow]:
        rows = (
            self._db.query(ExecutionArtifact)
            .filter(
                ExecutionArtifact.tenant_id == int(tenant_id),
                ExecutionArtifact.task_id == int(task_id),
            )
            .order_by(ExecutionArtifact.created_at.desc(), ExecutionArtifact.id.desc())
            .all()
        )

        resolved: dict[str, _ArtifactFileRow] = {}
        for row in rows:
            relative_path = self._normalize_artifact_relative_path(row.relative_path)
            if relative_path is None or relative_path in resolved:
                continue
            resolved[relative_path] = _ArtifactFileRow(
                artifact_id=str(row.id),
                relative_path=relative_path,
                byte_size=max(0, int(row.byte_size or 0)),
                modified=self._serialize_datetime(row.uploaded_at or row.created_at),
                upload_status=str(row.upload_status or "").strip().lower(),
                mime_type=str(row.mime_type or "").strip().lower() or None,
                is_text=row.is_text is not False,
                content_text=row.content_text if isinstance(row.content_text, str) else None,
                object_key=str(row.object_key).strip() if row.object_key else None,
            )
        return resolved

    @staticmethod
    def _normalize_relative_path(path: str | None, *, allow_root: bool) -> str:
        raw = str(path or "").strip().replace("\\", "/")
        if not raw or raw in {".", "/"}:
            if allow_root:
                return ""
            raise ValueError("Path must not be empty.")
        normalized = raw.lstrip("/")
        parts: list[str] = []
        for part in normalized.split("/"):
            cleaned = part.strip()
            if cleaned in {"", "."}:
                continue
            if cleaned == "..":
                raise ValueError("Path traversal is not allowed.")
            parts.append(cleaned)
        if not parts and allow_root:
            return ""
        if not parts:
            raise ValueError("Path must not be empty.")
        return "/".join(parts)

    @classmethod
    def _normalize_artifact_relative_path(cls, value: str | None) -> str | None:
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            return None
        if raw.startswith("/workspace/"):
            raw = raw[len("/workspace/") :]
        elif raw == "/workspace":
            return None
        elif raw.startswith("/"):
            raw = raw.lstrip("/")
        try:
            return cls._normalize_relative_path(raw, allow_root=False)
        except ValueError:
            return None

    def _build_content_payload(self, *, file_path: str, raw_content: str, size: int | None) -> dict[str, Any]:
        name = Path(file_path).name
        preview_type = self._file_browser_service._preview_type_from_name(name)
        metadata = {
            "is_valid_json": None,
            "is_valid_xml": None,
            "line_count": None,
        }
        if preview_type == "markdown":
            content = self._file_browser_service._sanitize_markdown(raw_content)
            encoding = "utf-8"
            file_type = "text/markdown"
        elif preview_type == "json":
            content, metadata["is_valid_json"] = self._file_browser_service._sanitize_json(raw_content)
            encoding = "utf-8"
            file_type = "application/json"
        elif preview_type == "xml":
            content, metadata["is_valid_xml"] = self._file_browser_service._sanitize_xml(raw_content)
            encoding = "utf-8"
            file_type = "application/xml"
        else:
            content = self._file_browser_service._sanitize_text(raw_content)
            preview_type = "text"
            encoding = "utf-8"
            file_type = "text/plain"
        metadata["line_count"] = self._file_browser_service._line_count(raw_content)
        return {
            "path": "/" + self._normalize_relative_path(file_path, allow_root=False),
            "name": name,
            "size": int(size or len(raw_content.encode("utf-8"))),
            "type": file_type,
            "content": content,
            "encoding": encoding,
            "preview_type": preview_type,
            "is_truncated": False,
            "modified": self._iso_now(),
            "metadata": metadata,
        }

    @staticmethod
    def _content_availability(row: _ArtifactFileRow) -> str:
        if row.upload_status == "upload_pending":
            return "upload_pending"
        if row.upload_status in {"upload_failed", "failed"}:
            return "upload_failed"
        if row.content_text is not None:
            return "available_inline"
        if row.object_key:
            return "available_object"
        if row.is_text is False:
            return "unavailable_non_text"
        return "not_available"

    @staticmethod
    def _serialize_datetime(value: datetime | None) -> str:
        if value is None:
            return datetime.now(tz=UTC).isoformat()
        return value.isoformat()

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(tz=UTC).isoformat()


__all__ = ["ArtifactFileBrowserService"]
