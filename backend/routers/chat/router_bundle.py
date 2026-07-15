"""Compose chat sub-routers behind the existing `/api` mount point."""

from __future__ import annotations

from fastapi import APIRouter

from .cancel import router as cancel_router
from .history import router as history_router
from .prewarm_ready import router as prewarm_ready_router
from .status import router as status_router
from .submit import router as submit_router

router = APIRouter()

router.include_router(prewarm_ready_router)
router.include_router(history_router)
router.include_router(submit_router)
router.include_router(cancel_router)
router.include_router(status_router)

__all__ = ["router"]
