"""Tests for think_more fallback behavior and resilience."""

from agent.graph.nodes.think_more import (
    _apply_fallback_thinking,
    _parse_thinking_response,
    _record_think_more_phase_memory,
)
from agent.graph.state import FactsState, InteractiveState, ToolExecutionRecord, TraceState
from agent.graph.utils.iteration_memory import get_ledger, render_phase_memory_section


def test_apply_fallback_thinking_handles_tool_execution_record_objects() -> None:
    """Fallback thinking should not crash when executed_tools stores model objects."""
    facts = FactsState(task_id=1, message="Analyze result", capability="deep_reasoning")
    trace = TraceState(
        executed_tools=[
            ToolExecutionRecord(
                tool_id="information_gathering.network_discovery.nmap",
                observation="Host is up; target port is closed",
            )
        ]
    )
    interactive = InteractiveState(facts=facts, trace=trace)

    _apply_fallback_thinking(interactive)

    assert any(
        entry.startswith("Thinking (fallback):")
        and "Executed information_gathering.network_discovery.nmap." in entry
        for entry in (interactive.trace.reasoning or [])
    )


def test_parse_thinking_response_prefers_structured_payload() -> None:
    """Structured payload should be consumed even when textual content is malformed."""
    facts = FactsState(task_id=2, message="Reason")
    interactive = InteractiveState(facts=facts, trace=TraceState())

    success = _parse_thinking_response(
        response="not-json",
        interactive=interactive,
        structured_payload={
            "reasoning": "Observed a reachable host and need focused follow-up.",
            "updated_plan": ["Step 1: Validate host reachability", "Step 2: Probe open ports"],
            "next_goal": "Validate host reachability",
            "key_observations": ["Host 10.0.0.5 responded to ping"],
        },
    )

    assert success is True
    assert interactive.facts.current_goal == "Validate host reachability"
    assert interactive.facts.plan == ["Step 1: Validate host reachability", "Step 2: Probe open ports"]


def test_record_think_more_phase_memory_appends_canonical_record() -> None:
    """Think-more phase handoff writes section snapshots for PTR."""
    interactive = InteractiveState(
        facts=FactsState(task_id=9, message="continue", metadata={"turn_sequence": 4}),
        trace=TraceState(reasoning=["Thinking: inspect host service banners"]),
    )

    _record_think_more_phase_memory(
        interactive,
        turn_sequence=4,
        parsed_payload={
            "reasoning": "Need to inspect banners before selecting exploit path.",
            "updated_plan": ["Inspect banners", "Map versions"],
            "next_goal": "Inspect banners",
            "key_observations": ["HTTP service seems to be nginx"],
        },
        used_fallback=False,
    )

    ledger = get_ledger(interactive.facts.metadata)
    assert len(ledger) == 1
    record = ledger[0]
    assert record["source"] == "think_more"
    assert record["turn_sequence"] == 4
    assert record["phase_sequence"] == 0
    assert set(record) == {"turn_sequence", "phase_sequence", "source", "sections"}
    assert record["sections"] == [
        {
            "heading": "Think More",
            "body": "status: completed\nupdated_plan_steps: 2",
        },
        {
            "heading": "Reasoning",
            "body": "Need to inspect banners before selecting exploit path.",
        },
        {
            "heading": "Key Observations",
            "body": "- HTTP service seems to be nginx",
        },
        {"heading": "Next Goal", "body": "Inspect banners"},
        {"heading": "Updated Plan", "body": "1. Inspect banners\n2. Map versions"},
    ]

    rendered = render_phase_memory_section(interactive.facts.metadata, turn_sequence=4)
    assert "## Prior Current-Turn Phase Memory" in rendered
    assert "<phase turn=4 phase=0 source=think_more>" in rendered
    assert "## Think More\nstatus: completed\nupdated_plan_steps: 2" in rendered
    assert "## Reasoning\nNeed to inspect banners before selecting exploit path." in rendered
    assert "## Key Observations\n- HTTP service seems to be nginx" in rendered
    assert "## Next Goal\nInspect banners" in rendered
    assert "## Updated Plan\n1. Inspect banners\n2. Map versions" in rendered


def test_record_think_more_phase_memory_drops_old_semantic_keys() -> None:
    """Stored think-more phase records do not append legacy semantic fields."""
    interactive = InteractiveState(
        facts=FactsState(task_id=10, message="continue", metadata={"turn_sequence": 5}),
        trace=TraceState(reasoning=["Thinking (fallback): continue from current evidence"]),
    )

    _record_think_more_phase_memory(
        interactive,
        turn_sequence=5,
        parsed_payload=None,
        used_fallback=True,
    )

    ledger = get_ledger(interactive.facts.metadata)
    assert len(ledger) == 1
    record = ledger[0]
    assert set(record) == {"turn_sequence", "phase_sequence", "source", "sections"}
    for old_key in ("kind", "status", "action", "result", "summary", "target", "hypothesis"):
        assert old_key not in record
