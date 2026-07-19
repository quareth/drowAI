"""Deployment baseline tests for retired task switch and container secrets."""

from __future__ import annotations

import inspect

from backend.services.llm_provider import environment_service


def test_task_switch_facade_is_retired() -> None:
    from backend.routers import llm as llm_routes

    source = inspect.getsource(llm_routes)

    assert not hasattr(llm_routes, "switch_task_model")
    assert not hasattr(llm_routes, "TaskSwitchRequest")
    assert '"/tasks/{task_id}/switch"' not in source


def test_container_environment_service_has_no_llm_secret_resolution() -> None:
    source = inspect.getsource(environment_service.LLMProviderEnvironmentService)

    assert "require_enabled_credential=False" in source
    assert '"OPENAI_API_KEY"' not in source
    assert "resolve_secret" not in source
