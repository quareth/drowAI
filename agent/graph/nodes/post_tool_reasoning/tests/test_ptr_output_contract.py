"""Tests for the PTR structured output schema contract.

This module validates that post-tool reasoning outputs expose only
decision fields. Current-turn phase records are runtime-derived, so the
LLM-facing schema must not request a phase-memory payload.
"""

from __future__ import annotations

from importlib import import_module

from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningDecisionOutput,
    PostToolReasoningOutput,
)


def test_ptr_output_models_do_not_expose_phase_memory_field() -> None:
    """PTR output schemas must not ask the LLM to produce phase memory."""
    assert "phase_memory" not in PostToolReasoningOutput.model_fields
    assert "phase_memory" not in PostToolReasoningDecisionOutput.model_fields

    full_schema = PostToolReasoningOutput.model_json_schema()
    decision_schema = PostToolReasoningDecisionOutput.model_json_schema()
    assert "phase_memory" not in full_schema.get("properties", {})
    assert "phase_memory" not in decision_schema.get("properties", {})


def test_post_tool_reasoning_package_does_not_export_iteration_memory_payload() -> None:
    """Removed PTR phase-memory model is absent from package exports."""
    post_tool_reasoning = import_module("agent.graph.nodes.post_tool_reasoning")

    assert not hasattr(post_tool_reasoning, "IterationMemoryPayload")
    assert "IterationMemoryPayload" not in post_tool_reasoning.__all__
