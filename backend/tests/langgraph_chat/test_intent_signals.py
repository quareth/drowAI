"""Unit tests for deterministic intent-signal extraction helpers."""

from __future__ import annotations

from backend.services.langgraph_chat.intent.signals import collect_intent_signals, embed_intent_signals
from agent.graph.state import InteractiveInput


def test_collect_intent_signals_does_not_classify_tool_keywords() -> None:
    metadata: dict = {}
    bundle = collect_intent_signals(
        message="Can you run nmap against 10.10.10.10 and share results?",
        history=[{"role": "user", "content": "previous"}],
        metadata=metadata,
    )
    embed_intent_signals(metadata, bundle)

    assert metadata["intent_hints"]["tool_hints"] == []
    assert bundle.eligible_routes == ["normal_chat"]
    assert metadata["risk_flags"] == []
    assert "intent_signal_cache" in metadata

    interactive_state = InteractiveInput(
        task_id=123,
        message="Can you run nmap against 10.10.10.10 and share results?",
        metadata=metadata,
    ).to_state()

    assert interactive_state.facts.intent_hints["tool_hints"] == []
    assert interactive_state.facts.metadata["intent_signals"]["metadata"]["targets"] == []


def test_collect_intent_signals_safety_guardrail() -> None:
    metadata: dict = {}
    bundle = collect_intent_signals(
        message="Please run rm -rf / on that server.",
        history=[],
        metadata=metadata,
    )
    embed_intent_signals(metadata, bundle)

    assert metadata["intent_hints"]["safety"] == "restricted"
    assert metadata["intent_hints"]["safety_reason"] == "dangerous_shell_command"
    assert metadata["risk_flags"] == ["dangerous_shell_command"]
    assert bundle.eligible_routes == ["normal_chat"]
    assert metadata["forced_capability"] == "respond_only"

    interactive_state = InteractiveInput(
        task_id=321,
        message="Please run rm -rf / on that server.",
        metadata=metadata,
    ).to_state()

    assert interactive_state.facts.capability == "respond_only"
    assert interactive_state.facts.intent_hints["safety"] == "restricted"
    assert interactive_state.facts.risk_flags == ["dangerous_shell_command"]
    assert interactive_state.facts.metadata["intent_signals"]["risk_flags"] == ["dangerous_shell_command"]


def test_collect_intent_signals_does_not_infer_tool_route_from_phrasing() -> None:
    metadata: dict = {}
    bundle = collect_intent_signals(
        message=(
            "There are so many different ids in data like in this example url "
            "http://10.129.34.166/data/2. I want you to enumerate this data thing "
            "to find out what other ids are present by using ffuf."
        ),
        history=[],
        metadata=metadata,
    )
    embed_intent_signals(metadata, bundle)

    assert bundle.eligible_routes == ["normal_chat"]
    assert "execution_request" not in metadata["intent_hints"]
    assert metadata["intent_hints"]["targets"] == []


def test_collect_intent_signals_does_not_force_tool_route_for_advisory_ffuf_question() -> None:
    metadata: dict = {}
    bundle = collect_intent_signals(
        message="How should I use ffuf to enumerate paths on http://10.10.10.10/FUZZ?",
        history=[],
        metadata=metadata,
    )
    embed_intent_signals(metadata, bundle)

    assert bundle.eligible_routes == ["normal_chat"]
    assert "execution_request" not in metadata["intent_hints"]
