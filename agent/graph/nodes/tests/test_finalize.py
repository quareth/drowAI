"""Tests for unified finalizer special-case branches."""

from __future__ import annotations

from typing import Any

import pytest

from agent.graph.nodes.finalize import _build_prompts, finalize_results
from agent.graph.state import FactsState, InteractiveState, TraceState


class _FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def emit_message_start(self) -> None:
        self.events.append(("start", None))

    def emit_message_delta(self, content: str) -> None:
        self.events.append(("delta", content))

    def emit_section_end(self, section_name: str = "final_answer") -> None:
        self.events.append(("end", section_name))


def _state(final_response: str) -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Pentest it",
            capability="deep_reasoning",
            metadata={
                "bootstrap_mode": "todo_failed",
                "todo_failed_final_response": final_response,
            },
        ),
        trace=TraceState(),
    )


@pytest.mark.asyncio
async def test_todo_failed_finalization_returns_exact_response_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_response = "Please choose an authorized target before I continue."
    monkeypatch.setattr("agent.graph.nodes.finalize.get_stream_writer", lambda: None)

    def _fail_resolve_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("normal finalizer LLM must not be resolved")

    monkeypatch.setattr("agent.graph.nodes.finalize.resolve_llm_client", _fail_resolve_llm)

    result = await finalize_results(_state(final_response).as_graph_state(), context=None, config={})

    assert result["trace"]["final_text"] == final_response


@pytest.mark.asyncio
async def test_todo_failed_finalization_emits_normal_final_answer_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_response = "Please choose an authorized target before I continue."
    emitter = _FakeEmitter()
    monkeypatch.setattr("agent.graph.nodes.finalize.get_stream_writer", lambda: object())
    monkeypatch.setattr("agent.graph.nodes.finalize._create_emitter", lambda **_kwargs: emitter)

    result = await finalize_results(_state(final_response).as_graph_state(), context=None, config={})

    assert result["trace"]["final_text"] == final_response
    assert emitter.events == [
        ("start", None),
        ("delta", final_response),
        ("end", "final_answer"),
    ]


def test_deep_reasoning_capability_selects_dr_finalizer_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    state = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="Check the target",
            capability="deep_reasoning",
            metadata={},
        ),
        trace=TraceState(),
    )

    def _capture_build_messages(**kwargs: Any) -> tuple[str, str, dict[str, Any]]:
        captured["capability"] = kwargs["capability"]
        return "system", "user", {}

    monkeypatch.setattr(
        "agent.graph.nodes.finalize._build_messages",
        _capture_build_messages,
    )

    _build_prompts(state)

    assert captured["capability"] == "deep_reasoning"
