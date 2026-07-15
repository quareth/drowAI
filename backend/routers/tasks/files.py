"""Task workspace file browser and download routes.

Responsibilities:
- Expose task file tree/content/search endpoints.
- Expose single and multi-file download endpoints.
- Apply shared ownership and file error mapping helpers.
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ...auth import get_current_user
from ...config import E2E_DETERMINISTIC_MODE, E2E_RUNTIME_LOCAL_MODE
from ...database import get_db
from ...models import User
from ...services.tenant.authorization import (
    ACTION_FILE_BROWSE,
    ACTION_FILE_DOWNLOAD,
    ACTION_FILE_READ,
)
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.workspace.runtime_file_explorer_service import RuntimeFileExplorerService
from ...services.runtime_provider.contracts import RuntimeCallScope
from .deps import (
    enforce_tenant_action,
    get_tenant_task_or_404,
    map_file_browser_exception,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class MultiFileDownloadRequest(BaseModel):
    """Request payload for multiple file download."""

    paths: list[str] = Field(..., min_length=1)


def _cleanup_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.warning("Failed to cleanup temp archive file: %s", path, exc_info=True)


def _file_explorer_service(db: Session) -> RuntimeFileExplorerService:
    """Use test scope only for explicit suite-owned local-workspace journeys."""
    is_e2e_local = E2E_DETERMINISTIC_MODE or E2E_RUNTIME_LOCAL_MODE
    scope = RuntimeCallScope.TEST if is_e2e_local else RuntimeCallScope.PRODUCT_TASK
    return RuntimeFileExplorerService(db, runtime_call_scope=scope)


@router.get("/{task_id}/files/tree")
async def get_task_file_tree(
    task_id: int,
    path: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Return nested directory tree for a task workspace."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_FILE_BROWSE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = _file_explorer_service(db)
    try:
        return await query_service.get_directory_tree(
            task=task,
            user_id=current_user.id,
            path=path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise map_file_browser_exception(exc) from exc


@router.get("/{task_id}/files/content")
async def get_task_file_content(
    task_id: int,
    path: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Return sanitized preview content and metadata for a workspace file."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_FILE_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = _file_explorer_service(db)
    try:
        return await query_service.get_file_content(
            task=task,
            user_id=current_user.id,
            path=path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise map_file_browser_exception(exc) from exc


@router.get("/{task_id}/files/download")
async def download_task_file(
    task_id: int,
    path: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Download a single file from the task workspace."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_FILE_DOWNLOAD)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = _file_explorer_service(db)
    try:
        download = await query_service.resolve_download_path(
            task=task,
            user_id=current_user.id,
            path=path,
        )
        requested_name = Path(str(path).replace("\\", "/").rstrip("/")).name
        download_name = requested_name or download.path.name
        background = (
            BackgroundTask(_cleanup_temp_file, download.path)
            if download.cleanup_after_response
            else None
        )
        return FileResponse(
            path=download.path,
            filename=download_name,
            media_type="application/octet-stream",
            background=background,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise map_file_browser_exception(exc) from exc


@router.post("/{task_id}/files/download-multiple")
async def download_task_files_as_zip(
    task_id: int,
    body: MultiFileDownloadRequest,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Download selected workspace paths as a temporary ZIP archive."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_FILE_DOWNLOAD)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = _file_explorer_service(db)
    try:
        zip_path = await query_service.create_zip_archive(
            task=task,
            user_id=current_user.id,
            file_paths=body.paths,
        )
        return FileResponse(
            path=zip_path,
            filename=f"task-{task_id}-files.zip",
            media_type="application/zip",
            background=BackgroundTask(_cleanup_temp_file, zip_path),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise map_file_browser_exception(exc) from exc


@router.get("/{task_id}/files/search")
async def search_task_files(
    task_id: int,
    q: str = Query(..., min_length=1),
    path: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Search workspace files by case-insensitive filename match."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_FILE_BROWSE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = _file_explorer_service(db)
    try:
        return await query_service.search_files(
            task=task,
            user_id=current_user.id,
            query=q,
            path=path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise map_file_browser_exception(exc) from exc
