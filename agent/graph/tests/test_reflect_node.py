"""Tests for reflect-node structured response parsing."""

from __future__ import annotations

from agent.graph.nodes.reflect import (
    _apply_fallback_reflection,
    _parse_reflection_response,
    _record_reflect_phase_memory,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.utils.iteration_memory import get_ledger, render_phase_memory_section


def test_parse_reflection_response_prefers_structured_payload() -> None:
    interactive = InteractiveState(
        facts=FactsState(task_id=3, message="reflect on failures"),
        trace=TraceState(),
    )

    success = _parse_reflection_response(
        response="not-json",
        interactive=interactive,
        structured_payload={
            "root_cause": "No target host was selected before service probing.",
            "alternative_approaches": ["Run host discovery first"],
        },
    )

    assert success is True
    assert interactive.facts.plan == []
    assert interactive.facts.todo_list == []
    assert any("Root cause" in item for item in interactive.trace.reasoning)


def test_record_reflect_phase_memory_appends_canonical_record() -> None:
    """Reflect handoff writes section snapshots for PTR."""
    interactive = InteractiveState(
        facts=FactsState(task_id=4, message="reflect", metadata={"turn_sequence": 2}),
        trace=TraceState(reasoning=["Reflection - Root cause: wrong host targeting"]),
    )

    _record_reflect_phase_memory(
        interactive,
        turn_sequence=2,
        parsed_payload={
            "root_cause": "Wrong host targeting",
            "alternative_approaches": ["Resolve host inventory first"],
        },
        next_action="call_tool",
        used_fallback=False,
    )

    ledger = get_ledger(interactive.facts.metadata)
    assert len(ledger) == 1
    record = ledger[0]
    assert record["source"] == "reflect"
    assert record["turn_sequence"] == 2
    assert record["phase_sequence"] == 0
    assert set(record) == {"turn_sequence", "phase_sequence", "source", "sections"}
    assert record["sections"] == [
        {
            "heading": "Reflection",
            "body": "status: completed",
        },
        {"heading": "Root Cause", "body": "Wrong host targeting"},
        {
            "heading": "Alternative Approaches",
            "body": "- Resolve host inventory first",
        },
        {"heading": "Next Action", "body": "call_tool"},
    ]

    rendered = render_phase_memory_section(interactive.facts.metadata, turn_sequence=2)
    assert "## Prior Current-Turn Phase Memory" in rendered
    assert "<phase turn=2 phase=0 source=reflect>" in rendered
    assert "## Reflection\nstatus: completed" in rendered
    assert "## Root Cause\nWrong host targeting" in rendered
    assert "## Alternative Approaches\n- Resolve host inventory first" in rendered
    assert "## Updated Plan" not in rendered
    assert "## Next Action\ncall_tool" in rendered


def test_record_reflect_phase_memory_drops_old_semantic_keys() -> None:
    """Stored reflect phase records do not append legacy semantic fields."""
    interactive = InteractiveState(
        facts=FactsState(task_id=5, message="reflect", metadata={"turn_sequence": 3}),
        trace=TraceState(reasoning=["Reflection (fallback): retry with focused target"]),
    )

    _record_reflect_phase_memory(
        interactive,
        turn_sequence=3,
        parsed_payload=None,
        next_action="think_more",
        used_fallback=True,
    )

    ledger = get_ledger(interactive.facts.metadata)
    assert len(ledger) == 1
    record = ledger[0]
    assert set(record) == {"turn_sequence", "phase_sequence", "source", "sections"}
    for old_key in ("kind", "status", "action", "result", "summary", "target", "hypothesis"):
        assert old_key not in record

    rendered = render_phase_memory_section(interactive.facts.metadata, turn_sequence=3)
    assert "## Reflection\nstatus: fallback" in rendered
    assert "## Root Cause\nReflection (fallback): retry with focused target" in rendered
    assert "## Next Action\nthink_more" in rendered


def test_fallback_reflection_guidance_renders_in_phase_memory() -> None:
    """Fallback reflect records prompt-owned guidance for PTR continuity."""
    interactive = InteractiveState(
        facts=FactsState(task_id=6, message="reflect", metadata={"turn_sequence": 4}),
        trace=TraceState(),
    )

    guidance = _apply_fallback_reflection(
        interactive,
        "Active todo stalled without meaningful progress",
    )
    _record_reflect_phase_memory(
        interactive,
        turn_sequence=4,
        parsed_payload=None,
        next_action="call_tool",
        used_fallback=True,
        fallback_guidance=guidance,
    )

    rendered = render_phase_memory_section(interactive.facts.metadata, turn_sequence=4)
    assert "## Reflection\nstatus: fallback" in rendered
    assert "reflection LLM call failed" in rendered
    assert "Assume the current direction is not working." in rendered
    assert "information currently available" in rendered
    assert "finalize/synthesize" in rendered
    assert "Current stuck pattern:" in rendered
    assert "available tool" not in rendered
