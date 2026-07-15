"""Live runtime workspace file explorer orchestration.

Responsibilities:
- Serve task file-browser operations from the task runtime workspace.
- Route runner-placement file reads through the runtime-provider boundary.
- Keep artifact/data-plane browsing separate from live workspace browsing.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import tempfile
from typing import Any
import zipfile

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspacePathError,
)

from backend.models import Task
from backend.services.runtime_provider.operations import (
    RuntimeOperationService,
    provider_result_detail,
)
from backend.services.runtime_provider.contracts import RuntimeCallScope

from .file_browser_service import FileBrowserService


_RUNNER_WAIT_TIMEOUT_SECONDS = 10.0
_RUNNER_NOT_FOUND_ERROR_CODES = frozenset({"RUNNER_ARTIFACT_NOT_FOUND", "not_found"})
_RUNNER_PATH_OUTSIDE_SCOPE_CODES = frozenset(
    {
        "RUNNER_ARTIFACT_PATH_OUTSIDE_SCOPE",
        "RUNNER_WORKSPACE_PATH_OUTSIDE_SCOPE",
    }
)
_RUNNER_ENTRY_UNSAFE_CODES = frozenset({"RUNNER_WORKSPACE_ENTRY_UNSAFE"})


@dataclass(frozen=True, slots=True)
class RuntimeDownloadPath:
    """Resolved file download path plus ownership of temporary cleanup."""

    path: Path
    cleanup_after_response: bool = False


class RuntimeFileExplorerService:
    """Application service for live task runtime file-browser operations."""

    def __init__(
        self,
        db: Session,
        *,
        file_browser_service: FileBrowserService | None = None,
        runtime_operations: RuntimeOperationService | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> None:
        self._runtime_operations = runtime_operations or RuntimeOperationService(db)
        self._file_browser_service = file_browser_service or FileBrowserService()
        self._runtime_call_scope = runtime_call_scope

    @staticmethod
    def _normalize_relative_path(path: str | None) -> str:
        raw = (path or "").strip().replace("\\", "/")
        if not raw or raw == "." or raw == "/":
            return ""
        return raw.lstrip("/").rstrip("/")

    @staticmethod
    def _iso_or_now(value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value
        return datetime.now(tz=UTC).isoformat()

    @staticmethod
    def _is_runner_context(*, context: Any) -> bool:
        return str(getattr(context, "runtime_placement_mode", "")).strip().lower() == "runner"

    async def _context_for_task(self, *, task: Task, user_id: int):
        return RuntimeOperationService.context_from_authorized_task(
            task=task,
            user_id=user_id,
            runtime_call_scope=self._runtime_call_scope,
        )

    async def _resolve_local_workspace_path(self, *, task: Task, user_id: int) -> Path:
        context = await self._context_for_task(task=task, user_id=user_id)
        result = await self._runtime_operations.run_for_context(
            context=context,
            operation="materialize_runtime_workspace",
            call=lambda provider, request: provider.materialize_runtime_workspace(request),
        )
        if not result.ok:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=provider_result_detail("Runtime workspace is unavailable", result),
            )
        delegate = result.metadata.get("delegate_result") if result.metadata else None
        workspace_path = delegate.get("workspace_path") if isinstance(delegate, Mapping) else None
        if not isinstance(workspace_path, (str, Path)) or not str(workspace_path).strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime provider did not return a materialized workspace path.",
            )
        return Path(workspace_path)

    def _raise_provider_failure(self, *, prefix: str, result: Any, path: str | None = None) -> None:
        if result.error_code in _RUNNER_NOT_FOUND_ERROR_CODES:
            raise FileNotFoundError(f"File not found: {path or '/'}")
        if result.error_code in _RUNNER_PATH_OUTSIDE_SCOPE_CODES:
            raise WorkspacePathError(
                str(result.error_message or "Path resolves outside workspace.")
            )
        if result.error_code in _RUNNER_ENTRY_UNSAFE_CODES:
            raise WorkspaceEntryUnsafeError(
                str(result.error_message or "Workspace entry is unsafe.")
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=provider_result_detail(prefix, result),
        )

    async def _query_runner_workspace_items(
        self,
        *,
        task: Task,
        user_id: int,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        context = await self._context_for_task(task=task, user_id=user_id)
        result = await self._runtime_operations.run_for_context(
            context=context,
            operation="query_runtime_artifacts",
            call=lambda provider, request: provider.query_runtime_artifacts(request),
            payload={"prefix": self._normalize_relative_path(path)},
            metadata={
                "wait_for_result": True,
                "wait_timeout_seconds": _RUNNER_WAIT_TIMEOUT_SECONDS,
            },
        )
        if not result.ok:
            self._raise_provider_failure(
                prefix="Runtime workspace query failed",
                result=result,
                path=path,
            )
        delegate = result.metadata.get("delegate_result") if result.metadata else None
        items = delegate.get("items") if isinstance(delegate, Mapping) else None
        if not isinstance(items, list):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime workspace query returned an invalid item payload.",
            )

        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            raw_path = str(item.get("path") or item.get("artifact_path") or "").strip()
            if not raw_path:
                continue
            normalized.append(
                {
                    "path": self._normalize_relative_path(raw_path),
                    "size": int(item.get("size") or 0),
                    "modified": self._iso_or_now(item.get("modified")),
                }
            )
        return normalized

    async def _read_runner_workspace_file(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
        binary: bool = False,
    ) -> Mapping[str, Any]:
        normalized_path = self._normalize_relative_path(path)
        context = await self._context_for_task(task=task, user_id=user_id)
        result = await self._runtime_operations.run_for_context(
            context=context,
            operation="read_runtime_artifact_file",
            call=lambda provider, request: provider.read_runtime_artifact_file(request),
            payload={
                "path": normalized_path,
                "artifact_path": normalized_path,
                "binary": binary,
            },
            metadata={
                "wait_for_result": True,
                "wait_timeout_seconds": _RUNNER_WAIT_TIMEOUT_SECONDS,
            },
        )
        if not result.ok:
            self._raise_provider_failure(
                prefix="Runtime workspace file read failed",
                result=result,
                path=path,
            )
        delegate = result.metadata.get("delegate_result") if result.metadata else None
        if not isinstance(delegate, Mapping):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime workspace read returned an invalid file payload.",
            )
        return delegate

    async def _read_runner_workspace_file_bytes(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> bytes:
        delegate = await self._read_runner_workspace_file(
            task=task,
            user_id=user_id,
            path=path,
            binary=True,
        )
        encoded = delegate.get("content_base64")
        if not isinstance(encoded, str):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime workspace binary read returned an invalid file payload.",
            )
        return base64.b64decode(encoded.encode("ascii"), validate=True)

    def _build_tree_from_items(
        self,
        *,
        items: list[dict[str, Any]],
        path: str | None,
    ) -> dict[str, Any]:
        root = self._normalize_relative_path(path)
        tree: dict[str, Any] = {
            "name": Path(root).name if root else "workspace",
            "type": "folder",
            "path": "/" if not root else f"/{root}",
            "size": None,
            "modified": self._iso_or_now(None),
            "children": [],
        }
        folders: dict[str, dict[str, Any]] = {"": tree}
        for file_row in sorted(items, key=lambda row: row["path"]):
            rel_path = str(file_row["path"])
            if root and not (rel_path == root or rel_path.startswith(f"{root}/")):
                continue
            relative_to_root = rel_path[len(root) + 1 :] if root else rel_path
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
                        "modified": self._iso_or_now(None),
                        "children": [],
                    }
                    parent_node["children"].append(folder)
                    folders[parent_key] = folder
                parent_node = folder
            file_name = parts[-1]
            file_path = f"/{root}/{relative_to_root}".replace("//", "/")
            parent_node["children"].append(
                {
                    "name": file_name,
                    "type": "file",
                    "path": file_path,
                    "size": file_row["size"],
                    "modified": file_row["modified"],
                    "children": [],
                }
            )
        return tree

    async def get_directory_tree(
        self,
        *,
        task: Task,
        user_id: int,
        path: str | None = None,
    ) -> dict[str, Any]:
        context = await self._context_for_task(task=task, user_id=user_id)
        if self._is_runner_context(context=context):
            items = await self._query_runner_workspace_items(task=task, user_id=user_id, path=path)
            return self._build_tree_from_items(items=items, path=path)
        workspace_path = await self._resolve_local_workspace_path(task=task, user_id=user_id)
        return self._file_browser_service.get_directory_tree_at_workspace(
            workspace_path=workspace_path,
            path=path,
        )

    async def get_file_content(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> dict[str, Any]:
        context = await self._context_for_task(task=task, user_id=user_id)
        if self._is_runner_context(context=context):
            delegate = await self._read_runner_workspace_file(
                task=task,
                user_id=user_id,
                path=path,
            )
            content = delegate.get("content")
            if not isinstance(content, str):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Runtime workspace read returned an invalid text payload.",
                )
            return self._file_browser_service.build_text_file_content_response(
                file_path=str(delegate.get("path") or path),
                raw_content=content,
                size=delegate.get("size") if isinstance(delegate.get("size"), int) else None,
                encoding=str(delegate.get("encoding") or "utf-8"),
            )
        workspace_path = await self._resolve_local_workspace_path(task=task, user_id=user_id)
        return self._file_browser_service.get_file_content_at_workspace(
            workspace_path=workspace_path,
            file_path=path,
        )

    async def resolve_download_path(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> RuntimeDownloadPath:
        context = await self._context_for_task(task=task, user_id=user_id)
        if self._is_runner_context(context=context):
            payload = await self._read_runner_workspace_file_bytes(
                task=task,
                user_id=user_id,
                path=path,
            )
            suffix = Path(self._normalize_relative_path(path)).suffix or ".bin"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(payload)
                return RuntimeDownloadPath(path=Path(handle.name), cleanup_after_response=True)

        workspace_path = await self._resolve_local_workspace_path(task=task, user_id=user_id)
        snapshot_path = self._file_browser_service.snapshot_download_at_workspace(
            workspace_path=workspace_path,
            file_path=path,
        )
        return RuntimeDownloadPath(path=snapshot_path, cleanup_after_response=True)

    async def create_zip_archive(
        self,
        *,
        task: Task,
        user_id: int,
        file_paths: list[str],
    ) -> Path:
        context = await self._context_for_task(task=task, user_id=user_id)
        if not self._is_runner_context(context=context):
            workspace_path = await self._resolve_local_workspace_path(task=task, user_id=user_id)
            return self._file_browser_service.create_zip_archive_at_workspace(
                workspace_path=workspace_path,
                file_paths=file_paths,
            )

        if not file_paths:
            raise ValueError("At least one path is required to create an archive.")

        selected: dict[str, bytes] = {}
        for raw_path in file_paths:
            normalized = self._normalize_relative_path(raw_path)
            descendants = await self._query_runner_workspace_items(
                task=task,
                user_id=user_id,
                path=normalized,
            )
            if descendants:
                for item in descendants:
                    item_path = str(item["path"])
                    selected[item_path] = await self._read_runner_workspace_file_bytes(
                        task=task,
                        user_id=user_id,
                        path=item_path,
                    )
                continue
            selected[normalized] = await self._read_runner_workspace_file_bytes(
                task=task,
                user_id=user_id,
                path=normalized,
            )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
            zip_path = Path(handle.name)
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for rel_path, payload in sorted(selected.items()):
                    archive.writestr(rel_path, payload)
            return zip_path
        except Exception:
            zip_path.unlink(missing_ok=True)
            raise

    async def search_files(
        self,
        *,
        task: Task,
        user_id: int,
        query: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        context = await self._context_for_task(task=task, user_id=user_id)
        if self._is_runner_context(context=context):
            query_lower = (query or "").strip().lower()
            items = await self._query_runner_workspace_items(task=task, user_id=user_id, path=path)
            results = []
            for item in items:
                file_path = str(item.get("path") or "")
                name = Path(file_path).name
                if query_lower and query_lower not in name.lower():
                    continue
                results.append(
                    {
                        "name": name,
                        "type": "file",
                        "path": f"/{file_path}",
                        "size": int(item.get("size") or 0),
                        "modified": self._iso_or_now(item.get("modified")),
                    }
                )
            return {
                "query": query,
                "results": results,
                "total_count": len(results),
                "truncated": False,
            }

        workspace_path = await self._resolve_local_workspace_path(task=task, user_id=user_id)
        return self._file_browser_service.search_files_at_workspace(
            workspace_path=workspace_path,
            query=query,
            path=path,
        )


__all__ = ["RuntimeDownloadPath", "RuntimeFileExplorerService"]
