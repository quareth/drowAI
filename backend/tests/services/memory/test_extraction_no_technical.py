"""Validate extraction prompt boundaries for technical-finding exclusion."""

from __future__ import annotations

from backend.services.memory.memory_extraction_prompts import build_extraction_messages


def test_extraction_prompt_contains_exclusion_clause() -> None:
    system_prompt = build_extraction_messages("hello", "world")[0]["content"]
    assert "DO NOT EXTRACT" in system_prompt
    assert "Port scan results, service versions, CVEs" in system_prompt


def test_extraction_prompt_contains_knowledge_pipeline_reference() -> None:
    system_prompt = build_extraction_messages("hello", "world")[0]["content"]
    assert "knowledge pipeline" in system_prompt
