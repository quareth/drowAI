"""Global CVE settings router with manual sync trigger endpoints.

Scope:
- Exposes dedicated API endpoints for CVE indexing settings/status reads and updates.
- Returns truthful dispatch outcomes for manual sync trigger requests.

Boundary:
- Delegates settings initialization/validation/response shaping to the CVE settings service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import (
    CvePurgeResponse,
    CveSettingsStaticResponse,
    CveSettingsResponse,
    CveSettingsStatusResponse,
    CveSyncDispatchResponse,
    CveSettingsUpdateRequest,
    User,
)
from ..services.cve_indexing.settings_service import (
    CveSettingsConflictError,
    CveSettingsService,
    CveSettingsValidationError,
)
from ..services.cve_indexing.contracts import CveSyncTriggerKind
from ..services.cve_indexing.runtime import cve_sync_scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/cve", tags=["settings"])


@router.get("", response_model=CveSettingsResponse)
async def get_cve_settings(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CveSettingsResponse:
    """Return global CVE settings plus current sync status summary."""
    service = CveSettingsService(db)
    return service.get_settings_response()


@router.get("/config", response_model=CveSettingsStaticResponse)
async def get_cve_settings_config(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CveSettingsStaticResponse:
    """Return static CVE settings used for instant panel rendering."""
    service = CveSettingsService(db)
    return service.get_settings_config_response()


@router.get("/status", response_model=CveSettingsStatusResponse)
async def get_cve_settings_status(
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CveSettingsStatusResponse:
    """Return live CVE sync status and latest run summary."""
    service = CveSettingsService(db)
    return service.get_status_response()


@router.put("", response_model=CveSettingsResponse)
async def update_cve_settings(
    payload: CveSettingsUpdateRequest,
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CveSettingsResponse:
    """Update mutable global CVE settings and return merged status response."""
    service = CveSettingsService(db)
    try:
        return service.update_settings(payload)
    except CveSettingsValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/sync", response_model=CveSyncDispatchResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_cve_sync(
    _current_user: User = Depends(get_current_user),
) -> CveSyncDispatchResponse:
    """Attempt manual CVE sync dispatch and return deterministic queue status."""
    dispatch = await cve_sync_scheduler.dispatch_sync_once(trigger_kind=CveSyncTriggerKind.MANUAL)
    if not dispatch.dispatched:
        logger.info(
            "Manual CVE sync dispatch skipped: reason=%s active_run_id=%s",
            dispatch.reason,
            dispatch.active_run_id,
        )
    return CveSyncDispatchResponse.model_validate(dispatch.to_api_payload())


@router.post("/sync/cancel", response_model=CveSyncDispatchResponse)
async def cancel_cve_sync(
    _current_user: User = Depends(get_current_user),
) -> CveSyncDispatchResponse:
    """Force-cancel any active CVE sync run."""
    dispatch = await cve_sync_scheduler.cancel_active_run()
    return CveSyncDispatchResponse.model_validate(dispatch.to_api_payload())


@router.post("/purge", response_model=CvePurgeResponse)
async def purge_cve_index(
    force: bool = Query(False),
    _current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CvePurgeResponse:
    """Purge indexed CVE records/history and reset operational sync state."""
    service = CveSettingsService(db)
    try:
        if force:
            await cve_sync_scheduler.cancel_active_run()
        return service.purge_index(force=force)
    except CveSettingsConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

