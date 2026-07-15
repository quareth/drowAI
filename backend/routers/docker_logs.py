"""Aggregated docker router preserving canonical `/api/docker` route paths.

Responsibilities:
- Provide a stable import/mount surface for docker-related routes.
- Compose focused sub-routers for REST logs/status, terminal sessions, and WS aliases.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.routers.docker_logs_rest import router as docker_logs_rest_router
from backend.routers.docker_terminal_sessions import router as docker_terminal_sessions_router
from backend.routers.docker_ws_alias import router as docker_ws_alias_router

router = APIRouter()
router.include_router(docker_logs_rest_router)
router.include_router(docker_terminal_sessions_router)
router.include_router(docker_ws_alias_router)
