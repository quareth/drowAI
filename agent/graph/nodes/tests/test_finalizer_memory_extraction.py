"""Tests for finalizer memory extraction runtime-selection propagation."""

from __future__ import annotations

from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.finalizer import finalize_turn


def test_finalize_turn_enqueues_memory_extraction_with_runtime_selection(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.memory.extraction_trigger.enqueue_memory_extraction",
        lambda **kwargs: calls.append(kwargs),
    )

    state = {
        "facts": {
            "task_id": 42,
            "message": "remember this",
            "conversation_id": "conv-1",
            "metadata": {
                "user_id": 7,
                "llm_runtime_selection": {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "credential_ref": {"user_id": 7, "provider": "openai"},
                    "reasoning_effort": "medium",
                },
            },
        },
        "trace": {"history": [], "final_text": "saved"},
    }

    finalize_turn(state)

    assert calls[0]["llm_runtime_selection"] == {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "medium",
    }


def test_finalize_turn_builds_memory_snapshot_from_context(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.memory.extraction_trigger.enqueue_memory_extraction",
        lambda **kwargs: calls.append(kwargs),
    )

    state = {
        "facts": {
            "task_id": 42,
            "message": "remember this",
            "metadata": {"user_id": 7},
        },
        "trace": {"history": [], "final_text": "saved"},
    }

    finalize_turn(
        state,
        context=GraphRuntimeContext(
            task_id=42,
            user_id=7,
            provider="openai",
            model="gpt-5.2",
            credential_ref={"user_id": 7, "provider": "openai"},
            reasoning_effort="medium",
        ),
    )

    assert calls[0]["llm_runtime_selection"] == {
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "reasoning_effort": "medium",
    }
