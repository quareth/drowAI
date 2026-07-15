"""Unit tests for the reusable reasoning-section emission helper.

These tests verify helper lifecycle behavior independent from graph node
instrumentation: success ordering, exception semantics, and no-op safety when
no writer is present.
"""

from __future__ import annotations

import pytest

from agent.graph.emission.reasoning_section import reasoning_section
from agent.graph.state import FactsState, InteractiveState, TraceState


def _make_state() -> InteractiveState:
    facts = FactsState(
        task_id=42,
        message="Test helper lifecycle",
        conversation_id="conv-reasoning-helper",
        capability="simple_tool_execution",
    )
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.mark.asyncio
async def test_reasoning_section_success_emits_start_delta_end_in_order() -> None:
    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    async with reasoning_section(
        _writer,
        state=_make_state(),
        step="plan_creation",
        label="Building the execution plan.",
    ) as emitter:
        assert emitter is not None
        timeline.append("inside")

    assert timeline == [
        "reasoning_start",
        "reasoning_delta",
        "inside",
        "reasoning_section_end",
    ]


@pytest.mark.asyncio
async def test_reasoning_section_exception_emits_end_and_reraises() -> None:
    timeline: list[str] = []

    def _writer(event: dict) -> None:
        timeline.append(str(event.get("type") or ""))

    with pytest.raises(RuntimeError, match="boom"):
        async with reasoning_section(
            _writer,
            state=_make_state(),
            step="reflection",
            label="Reviewing the latest result.",
        ):
            raise RuntimeError("boom")

    assert timeline == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]


@pytest.mark.asyncio
async def test_reasoning_section_missing_writer_is_noop() -> None:
    async with reasoning_section(
        None,
        state=_make_state(),
        step="tool_planning",
        label="Preparing tool execution.",
    ) as emitter:
        assert emitter is None
