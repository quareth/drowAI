"""Regression tests for graph guard predicates and degradation metadata."""

from __future__ import annotations

import pytest

from agent.graph.guards import capability_in
from agent.graph.infrastructure.state_models import CapabilityType
from agent.graph.nodes.decision_router.router import record_decision
from agent.graph.nodes.handle_unavailable_tools import handle_unavailable_tools_node
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_capability_in_does_not_match_unrelated_unknown_labels() -> None:
    """Unknown routing labels must not collapse through RESPOND fallback."""

    facts = FactsState(task_id=1, message="test", capability="respond_only")

    assert capability_in(facts, ["respond_only"]) is True
    assert capability_in(facts, ["deep_reasoning"]) is False
    assert capability_in(facts, ["simple_tool_execution"]) is False


def test_capability_in_matches_exact_routing_and_enum_labels() -> None:
    """Exact routing labels and enum values still match after strict parsing."""

    deep = FactsState(task_id=1, message="test", capability="deep_reasoning")
    simple = FactsState(task_id=1, message="test", capability="simple-tool-execution")
    port_scan = FactsState(task_id=1, message="test", capability=CapabilityType.PORT_SCAN)

    assert capability_in(deep, ["deep_reasoning"]) is True
    assert capability_in(simple, ["simple_tool_execution"]) is True
    assert capability_in(port_scan, ["port_scan"]) is True
    assert capability_in(port_scan, [CapabilityType.PORT_SCAN]) is True


def test_decision_router_record_decision_normalizes_none_history() -> None:
    """Decision recording should use the canonical decision-history ensure helper."""

    facts = FactsState(task_id=1, message="test")
    facts.decision_history = None  # type: ignore[assignment]
    state = InteractiveState(facts=facts, trace=TraceState())

    record_decision(state, "finalize", "done")

    assert state.facts.decision_history == ["finalize: done"]


@pytest.mark.asyncio
async def test_handle_unavailable_tools_preserves_empty_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Degradation metadata must be written even when metadata starts empty."""

    facts = FactsState(task_id=1, message="test", capability="vuln_scan", metadata={})
    facts.decision_history = None  # type: ignore[assignment]
    state = InteractiveState(facts=facts, trace=TraceState())

    monkeypatch.setattr(
        "agent.graph.nodes.handle_unavailable_tools.are_scope_goals_achieved",
        lambda _state: False,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.handle_unavailable_tools.get_fallback_capability",
        lambda _capability: None,
    )
    monkeypatch.setattr(
        "agent.graph.nodes.handle_unavailable_tools.are_tools_available",
        lambda _capability: False,
    )

    result = await handle_unavailable_tools_node(state)

    metadata = result["facts"]["metadata"]
    assert metadata["tool_gaps"]
    assert metadata["limitations"]
    assert "finalize" in result["facts"]["decision_history"][-1]
