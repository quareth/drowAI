"""Tests for shared LLM timeout configuration constants and env precedence."""

from __future__ import annotations

import importlib

import pytest

import core.llm.timeouts as timeout_module


def _reload_timeouts() -> object:
    return importlib.reload(timeout_module)


def test_shared_llm_timeouts_default_to_120_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC",
        "LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC",
        "LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC",
        "LLM_TIMEOUT_REFLECT_SEC",
    ):
        monkeypatch.delenv(key, raising=False)
    for legacy_key in (
        "PLANNER_TOOL_CALL_TIMEOUT_SEC",
        "TOOL_CALL_TIMEOUT",
        "TOOL_PROCESSOR_TIMEOUT",
    ):
        monkeypatch.delenv(legacy_key, raising=False)

    reloaded = _reload_timeouts()

    assert reloaded.DEFAULT_LLM_TIMEOUT_SEC == 120
    assert reloaded.LLM_TIMEOUT_INTENT_CLASSIFIER_SEC == 120
    assert reloaded.LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC == 120
    assert reloaded.LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC == 120
    assert reloaded.LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC == 120
    assert reloaded.LLM_TIMEOUT_REFLECT_SEC == 300


def test_shared_llm_timeouts_honor_legacy_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC", raising=False)
    monkeypatch.setenv("PLANNER_TOOL_CALL_TIMEOUT_SEC", "72")
    monkeypatch.setenv("TOOL_PROCESSOR_TIMEOUT", "73")

    reloaded = _reload_timeouts()

    assert reloaded.LLM_TIMEOUT_INTENT_CLASSIFIER_SEC == 120
    assert reloaded.LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC == 72
    assert reloaded.LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC == 73


def test_shared_llm_timeouts_prefer_primary_env_over_legacy_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC", "82")
    monkeypatch.setenv("PLANNER_TOOL_CALL_TIMEOUT_SEC", "72")
    monkeypatch.setenv("TOOL_CALL_TIMEOUT", "74")
    monkeypatch.setenv("LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC", "83")
    monkeypatch.setenv("TOOL_PROCESSOR_TIMEOUT", "73")

    reloaded = _reload_timeouts()

    assert reloaded.LLM_TIMEOUT_INTENT_CLASSIFIER_SEC == 120
    assert reloaded.LLM_TIMEOUT_PLANNER_PARAMETER_RESOLUTION_SEC == 82
    assert reloaded.LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC == 83


def test_reflect_timeout_honors_primary_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_REFLECT_SEC", "301")

    reloaded = _reload_timeouts()

    assert reloaded.LLM_TIMEOUT_REFLECT_SEC == 301


def test_read_positive_int_env_ignores_invalid_and_non_positive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIMARY_TIMEOUT", "0")
    monkeypatch.setenv("FALLBACK_TIMEOUT", "abc")

    reloaded = _reload_timeouts()

    assert (
        reloaded._read_positive_int_env(
            "PRIMARY_TIMEOUT",
            60,
            fallback_keys=("FALLBACK_TIMEOUT",),
        )
        == 60
    )
