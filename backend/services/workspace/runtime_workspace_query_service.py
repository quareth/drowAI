"""Provider-bound workspace query helpers for scope and file-browser routes.

Responsibilities:
- Resolve authorized task runtime workspaces through RuntimeOperationService.
- Keep route handlers from reconstructing provider-local workspace paths.
- Delegate local file operations to FileBrowserService behind provider context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime_shared.workspace_filesystem import WorkspaceFilesystem

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from backend.models import Task
from backend.services.runtime_provider.operations import (
    RuntimeOperationService,
    provider_result_detail,
)
from backend.services.runtime_provider.contracts import RuntimeCallScope
from backend.services.data_plane.artifact_file_browser_service import ArtifactFileBrowserService

from .file_browser_service import FileBrowserService
from .runtime_file_explorer_service import RuntimeFileExplorerService


class TaskWorkspaceQueryService:
    """Provider-aware workspace query boundary for task scope/file reads."""

    def __init__(
        self,
        db: Session,
        *,
        file_browser_service: FileBrowserService | None = None,
        artifact_file_browser_service: ArtifactFileBrowserService | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> None:
        self._db = db
        self._runtime_operations = RuntimeOperationService(db)
        self._file_browser_service = file_browser_service or FileBrowserService()
        self._artifact_file_browser_service = artifact_file_browser_service or ArtifactFileBrowserService(db)
        self._runtime_call_scope = runtime_call_scope

    def _file_explorer_service(self) -> RuntimeFileExplorerService:
        return RuntimeFileExplorerService(
            self._db,
            file_browser_service=self._file_browser_service,
            runtime_operations=self._runtime_operations,
            runtime_call_scope=self._runtime_call_scope,
        )

    @staticmethod
    def _normalize_relative_path(path: str | None) -> str:
        raw = (path or "").strip().replace("\\", "/")
        if not raw or raw == "." or raw == "/":
            return ""
        normalized = raw.lstrip("/")
        if normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        return normalized

    def _build_content_payload(self, *, file_path: str, raw_content: str, size: int | None) -> dict[str, Any]:
        return self._file_browser_service.build_text_file_content_response(
            file_path=file_path,
            raw_content=raw_content,
            size=size,
        )

    async def _context_for_task(self, *, task: Task, user_id: int):
        return RuntimeOperationService.context_from_authorized_task(
            task=task,
            user_id=user_id,
            runtime_call_scope=self._runtime_call_scope,
        )

    @staticmethod
    def _is_runner_context(*, context: Any) -> bool:
        return str(getattr(context, "runtime_placement_mode", "")).strip().lower() == "runner"

    async def _read_workspace_file(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> dict[str, Any] | None:
        context = await self._context_for_task(task=task, user_id=user_id)
        normalized_path = self._normalize_relative_path(path)
        result = await self._runtime_operations.run_for_context(
            context=context,
            operation="read_runtime_artifact_file",
            call=lambda provider, request: provider.read_runtime_artifact_file(request),
            payload={"path": normalized_path},
        )
        if not result.ok:
            if context.runtime_placement_mode == "local":
                return None
            if result.error_code in {"RUNNER_ARTIFACT_NOT_FOUND", "not_found"}:
                return {"not_found": True}
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=provider_result_detail("Runtime workspace file read failed", result),
            )
        delegate = result.metadata.get("delegate_result") if result.metadata else None
        if not isinstance(delegate, dict):
            return None
        content = delegate.get("content")
        if not isinstance(content, str):
            return None
        file_path = str(delegate.get("path") or delegate.get("artifact_path") or normalized_path)
        return self._build_content_payload(
            file_path=file_path,
            raw_content=content,
            size=delegate.get("size") if isinstance(delegate.get("size"), int) else None,
        )

    async def resolve_workspace_path(self, *, task: Task, user_id: int) -> Path:
        """Resolve provider-owned local workspace path for compatibility reads."""
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
        workspace_path = delegate.get("workspace_path") if isinstance(delegate, dict) else None
        if not isinstance(workspace_path, (str, Path)) or not str(workspace_path).strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Runtime provider did not return a materialized workspace path.",
            )
        return Path(workspace_path)

    async def read_scope_markdown(self, *, task: Task, user_id: int) -> str | None:
        """Return runtime scope markdown content when provider workspace has `scope.md`."""
        context = await self._context_for_task(task=task, user_id=user_id)
        if self._is_runner_context(context=context):
            try:
                content = self._artifact_file_browser_service.get_file_content(
                    tenant_id=int(context.tenant_id),
                    task_id=int(task.id),
                    path="scope.md",
                )
            except FileNotFoundError:
                return None
            text_content = content.get("content")
            if not isinstance(text_content, str):
                return None
            return text_content

        provider_content = await self._read_workspace_file(task=task, user_id=user_id, path="scope.md")
        if isinstance(provider_content, dict):
            if provider_content.get("not_found"):
                return None
            content = provider_content.get("content")
            if isinstance(content, str):
                return content
        workspace_path = await self.resolve_workspace_path(task=task, user_id=user_id)
        try:
            return WorkspaceFilesystem(workspace_path).read_bytes("scope.md").decode("utf-8")
        except FileNotFoundError:
            return None

    async def get_directory_tree(
        self,
        *,
        task: Task,
        user_id: int,
        path: str | None = None,
    ) -> dict[str, Any]:
        return await self._file_explorer_service().get_directory_tree(
            task=task,
            user_id=user_id,
            path=path,
        )

    async def get_file_content(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> dict[str, Any]:
        return await self._file_explorer_service().get_file_content(
            task=task,
            user_id=user_id,
            path=path,
        )

    async def resolve_download_path(
        self,
        *,
        task: Task,
        user_id: int,
        path: str,
    ) -> Path:
        download = await self._file_explorer_service().resolve_download_path(
            task=task,
            user_id=user_id,
            path=path,
        )
        return download.path

    async def create_zip_archive(
        self,
        *,
        task: Task,
        user_id: int,
        file_paths: list[str],
    ) -> Path:
        return await self._file_explorer_service().create_zip_archive(
            task=task,
            user_id=user_id,
            file_paths=file_paths,
        )

    async def search_files(
        self,
        *,
        task: Task,
        user_id: int,
        query: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        return await self._file_explorer_service().search_files(
            task=task,
            user_id=user_id,
            query=query,
            path=path,
        )
