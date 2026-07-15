"""Validate memory extraction prompt builders as pure deterministic functions."""

from __future__ import annotations

from backend.services.memory.memory_extraction_prompts import (
    GATE_MAX_INPUT_CHARS,
    build_extraction_messages,
    build_gate_classifier_messages,
)


def test_gate_messages_structure() -> None:
    messages = build_gate_classifier_messages("hello", "world")
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "content" in messages[0]
    assert "content" in messages[1]


def test_gate_messages_truncates_long_input() -> None:
    # The gate input cap was scaled up (2026-04-14); use inputs that
    # comfortably exceed the cap so the truncation assertion stays
    # meaningful regardless of the numeric value.
    oversize = GATE_MAX_INPUT_CHARS + 1000
    messages = build_gate_classifier_messages("u" * oversize, "a" * oversize)
    assert len(messages[1]["content"]) == GATE_MAX_INPUT_CHARS


def test_extraction_messages_structure() -> None:
    messages = build_extraction_messages("hello", "world")
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_extraction_system_prompt_excludes_technical() -> None:
    system_prompt = build_extraction_messages("hello", "world")[0]["content"]
    assert "DO NOT EXTRACT" in system_prompt
    assert "Port scan results, service versions, CVEs" in system_prompt


def test_extraction_system_prompt_includes_tiers() -> None:
    system_prompt = build_extraction_messages("hello", "world")[0]["content"]
    assert "user_profile" in system_prompt
    assert "task_engagement" in system_prompt
