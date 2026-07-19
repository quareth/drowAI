"""Deployment baseline tests for retired settings LLM mirror routes."""

from __future__ import annotations

from backend.tests.routers.test_settings_legacy_text_llm_retirement import (
    test_settings_read_write_exclude_openai_text_llm_mirrors,
)


def test_deployment_baseline_settings_openai_mirrors_are_retired() -> None:
    """Legacy baseline now points at the Phase 6 retirement contract."""

    test_settings_read_write_exclude_openai_text_llm_mirrors()
