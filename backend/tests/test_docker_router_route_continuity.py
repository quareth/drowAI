"""Route continuity checks for docker router modularization."""

from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from backend.main import app


def test_docker_rest_routes_are_still_mounted() -> None:
    http_paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/docker/docker-compose/logs/{task_id}" in http_paths
    assert "/api/docker/docker-compose/progress/{task_id}" in http_paths
    assert "/api/docker/docker-compose/status" in http_paths
    assert "/api/docker/execute-command/{task_id}" in http_paths
    assert "/api/docker/stop-container/{task_id}" in http_paths
    assert "/api/docker/container/metrics/{task_id}" in http_paths
    assert "/api/docker/container/status/{task_id}" in http_paths
    assert "/api/docker/terminal/sessions" in http_paths
    assert "/api/docker/terminal/sessions/{task_id}" in http_paths
    assert "/api/docker/terminal/sessions/{session_id}" in http_paths


def test_docker_ws_alias_routes_are_still_mounted() -> None:
    ws_paths = {route.path for route in app.routes if isinstance(route, WebSocketRoute)}
    assert "/api/docker/ws/logs/{task_id}" in ws_paths
    assert "/api/docker/ws/terminal/{task_id}" in ws_paths
