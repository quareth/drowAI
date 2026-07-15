"""Aggregate engagement reporting API routers under one namespace."""

from fastapi import APIRouter as _APIRouter

from .inputs import router as _inputs_router
from .jobs import router as _jobs_router
from .memos import router as _memos_router
from .reports import router as _reports_router

router = _APIRouter(prefix="/api/reporting", tags=["reporting"])
router.include_router(_inputs_router)
router.include_router(_reports_router)
router.include_router(_jobs_router)
router.include_router(_memos_router)

__all__ = ["router"]
