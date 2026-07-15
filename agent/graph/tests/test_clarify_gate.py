from __future__ import annotations

import pytest

from agent.graph.state import FactsState, InteractiveState, TraceState


def _build_state() -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message="test request",
        capability="deep_reasoning",
    )
    facts.metadata = {}
    return InteractiveState(facts=facts, trace=TraceState())


@pytest.mark.asyncio
async def test_clarify_gate_fast_path_when_no_pending_request(monkeypatch) -> None:
    from agent.graph.nodes import clarify_gate as clarify_gate_module

    monkeypatch.setattr(
        clarify_gate_module,
        "request_clarify_answers",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("interrupt should not be called")),
    )

    state = _build_state()
    result = await clarify_gate_module.clarify_gate_node(state)
    assert "pending_clarify_request" not in result["facts"]["metadata"]


@pytest.mark.asyncio
async def test_clarify_gate_requests_answers_and_clears_pending(monkeypatch) -> None:
    from agent.graph.nodes import clarify_gate as clarify_gate_module

    monkeypatch.setattr(
        clarify_gate_module,
        "request_clarify_answers",
        lambda **_kwargs: {"action": "answer", "answers": {"target": "10.0.0.1"}},
    )

    state = _build_state()
    state.facts.metadata = {
        "pending_clarify_request": {
            "required_blockers": [
                {
                    "slot": "target",
                    "question": "Which host should be scanned?",
                    "input_type": "select",
                    "options": ["10.0.0.1", "10.0.0.2"],
                },
            ]
        },
        "clarified_context": {},
    }

    result = await clarify_gate_module.clarify_gate_node(state)
    metadata = result["facts"]["metadata"]
    assert metadata["clarified_context"]["target"] == "10.0.0.1"
    assert "pending_clarify_request" not in metadata
    assert metadata["planner_mode"] == "plan_ready"
    assert "target" in metadata["asked_slots"]


@pytest.mark.asyncio
async def test_clarify_gate_invalid_option_triggers_retry(monkeypatch) -> None:
    from agent.graph.nodes import clarify_gate as clarify_gate_module

    monkeypatch.setattr(
        clarify_gate_module,
        "request_clarify_answers",
        lambda **_kwargs: {"action": "answer", "answers": {"target": "10.0.0.9"}},
    )

    state = _build_state()
    state.facts.metadata = {
        "pending_clarify_request": {
            "required_blockers": [
                {
                    "slot": "target",
                    "question": "Which host should be scanned?",
                    "input_type": "select",
                    "options": ["10.0.0.1", "10.0.0.2"],
                },
            ]
        },
        "clarified_context": {},
    }

    result = await clarify_gate_module.clarify_gate_node(state)
    metadata = result["facts"]["metadata"]
    assert "pending_clarify_request" in metadata
    assert metadata["planner_mode"] == "clarify_required"
    assert metadata["clarify_retry_counts"]["target"] == 1


@pytest.mark.asyncio
async def test_clarify_gate_fails_after_second_invalid_attempt(monkeypatch) -> None:
    from agent.graph.nodes import clarify_gate as clarify_gate_module

    monkeypatch.setattr(
        clarify_gate_module,
        "request_clarify_answers",
        lambda **_kwargs: {"action": "answer", "answers": {"target": "10.0.0.9"}},
    )

    state = _build_state()
    state.facts.metadata = {
        "pending_clarify_request": {
            "required_blockers": [
                {
                    "slot": "target",
                    "question": "Which host should be scanned?",
                    "input_type": "select",
                    "options": ["10.0.0.1", "10.0.0.2"],
                },
            ]
        },
        "clarified_context": {},
        "clarify_retry_counts": {"target": 1},
    }

    result = await clarify_gate_module.clarify_gate_node(state)
    metadata = result["facts"]["metadata"]
    assert metadata["planner_mode"] == "plan_failed"
    assert metadata["clarify_phase_status"] == "failed"
    assert metadata["plan_rejected"] is True
    assert "pending_clarify_request" not in metadata
    assert "retry limit" in metadata["clarify_failure_message"]


@pytest.mark.asyncio
async def test_clarify_gate_clears_invalid_pending_payload_without_interrupt(monkeypatch) -> None:
    from agent.graph.nodes import clarify_gate as clarify_gate_module

    monkeypatch.setattr(
        clarify_gate_module,
        "request_clarify_answers",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("interrupt should not be called")),
    )

    state = _build_state()
    state.facts.metadata = {"pending_clarify_request": {"required_blockers": []}}
    result = await clarify_gate_module.clarify_gate_node(state)
    metadata = result["facts"]["metadata"]
    assert "pending_clarify_request" not in metadata
    assert metadata["planner_mode"] == "plan_ready"
