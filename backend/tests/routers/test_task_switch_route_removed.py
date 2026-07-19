"""Regression tests for retiring the deprecated task model-switch route."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app
from backend.routers import llm as llm_routes


def test_task_switch_route_is_not_registered() -> None:
    """The LLM router no longer exposes the task-scoped switch facade."""

    paths = {getattr(route, "path", "") for route in llm_routes.router.routes}

    assert "/api/llm/tasks/{task_id}/switch" not in paths
    assert not hasattr(llm_routes, "TaskSwitchRequest")
    assert not hasattr(llm_routes, "switch_task_model")


def test_task_switch_endpoint_returns_404() -> None:
    """HTTP callers must use the canonical user-global selection route."""

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/tasks/1/switch",
            json={"model": "gpt-5.2"},
        )

    assert response.status_code == 404
