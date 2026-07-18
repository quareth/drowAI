"""Deployment baseline tests for the switch facade and container secrets."""

from __future__ import annotations

import inspect

from backend.routers import llm as llm_routes
from backend.services.llm_provider import environment_service


def test_task_switch_route_is_deprecated_selection_facade_without_signal() -> None:
    source = inspect.getsource(llm_routes.switch_task_model)

    assert "LLMProviderSelectionService" in source
    assert '"deprecated": True' in source
    assert '"effective_from": "next_submitted_turn"' in source
    assert '"signal_sent": False' in source
    assert "append_and_signal" not in source


def test_container_environment_service_has_no_llm_secret_resolution() -> None:
    source = inspect.getsource(environment_service.LLMProviderEnvironmentService)

    assert "require_enabled_credential=False" in source
    assert '"OPENAI_API_KEY"' not in source
    assert "resolve_secret" not in source
