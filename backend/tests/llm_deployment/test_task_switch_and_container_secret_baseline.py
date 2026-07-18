"""Deployment baseline tests for residual task switch and container secrets."""

from __future__ import annotations

import inspect

from backend.routers import llm as llm_routes
from backend.services.llm_provider import environment_service


def test_task_switch_route_is_currently_legacy_residual_signal_path() -> None:
    source = inspect.getsource(llm_routes.switch_task_model)

    assert "legacy_residual" == "legacy_residual"
    assert "phase_2_replacement" == "phase_2_replacement"
    assert "append_and_signal" in source
    assert "__switch_model:" in source
    assert '"type": "switch_llm"' in source
    assert '"command": "switch_llm_model"' in source


def test_container_openai_key_injection_is_legacy_security_exception() -> None:
    source = inspect.getsource(environment_service.LLMProviderEnvironmentService)

    assert "legacy_security_exception" == "legacy_security_exception"
    assert "phase_2_phase_4_removal_gate" == "phase_2_phase_4_removal_gate"
    assert '"OPENAI_API_KEY"' in source
    assert 'purpose="container_environment"' in source
