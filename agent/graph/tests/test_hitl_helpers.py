"""Tests for HITL approval policy helpers and interrupt payload builders."""

import importlib

import pytest


def _reload_helpers():
    import backend.config as config_module
    from agent.graph.nodes import hitl_helpers

    importlib.reload(config_module)
    return importlib.reload(hitl_helpers)


def test_should_require_approval_full_access(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert helpers.should_require_approval({"agent_mode": "full_access"}) is False


def test_should_require_approval_agent_mode(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert helpers.should_require_approval({"agent_mode": "agent"}) is True


def test_should_require_approval_feature_flag_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "false")
    helpers = _reload_helpers()
    assert helpers.should_require_approval({"agent_mode": "agent"}) is False


def test_should_require_approval_agent_plus_plan_mode_keeps_approvals(monkeypatch) -> None:
    """Phase 6 Task 6.5: ``agent`` + ``plan_mode=True`` keeps tool approvals.

    Plan is a route overlay — it must not change autonomy / HITL
    semantics. A Plan-overlay turn on top of ``agent`` still produces
    the same tool-approval prompts as a plain ``agent`` turn.
    """
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_approval({"agent_mode": "agent", "plan_mode": True})
        is True
    )


def test_should_require_approval_full_access_plus_plan_mode_skips_approvals(
    monkeypatch,
) -> None:
    """Phase 6 Task 6.5: ``full_access`` + ``plan_mode=True`` skips approvals.

    ``Full Access + Plan`` preserves full-access autonomy: deep
    reasoning routing with no tool-use approval prompts. The overlay
    must not collapse into ``agent`` semantics.
    """
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_approval({"agent_mode": "full_access", "plan_mode": True})
        is False
    )


def test_should_require_approval_plan_mode_alone_does_not_trigger_approvals(
    monkeypatch,
) -> None:
    """Phase 6 Task 6.5: ``plan_mode=True`` with ``full_access`` does not trigger approvals.

    The approval decision keys off ``agent_mode`` alone; route overlay
    presence is never sufficient by itself. This guards against
    accidental coupling of route preference to autonomy in the future.
    """
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_approval(
            {"agent_mode": "full_access", "plan_mode": True}
        )
        is False
    )


def test_should_require_approval_ignores_plan_review_required(monkeypatch) -> None:
    """Tool approval remains controlled by agent_mode only."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_approval(
            {"agent_mode": "agent", "plan_review_required": False}
        )
        is True
    )
    assert (
        helpers.should_require_approval(
            {"agent_mode": "full_access", "plan_review_required": True}
        )
        is False
    )


def test_build_tool_approval_payload() -> None:
    from agent.graph.nodes.hitl_helpers import build_tool_approval_payload

    payload = build_tool_approval_payload(
        tool_id="network.nmap",
        tool_name="Nmap",
        parameters={"target": "192.168.1.1"},
        turn_sequence=12,
        turn_id="task-1-turn-12",
        reserved_message_id=34,
    )
    assert payload["type"] == "tool_approval"
    assert payload["tool_id"] == "network.nmap"
    assert payload["turn_sequence"] == 12
    assert payload["turn_id"] == "task-1-turn-12"
    assert payload["reserved_message_id"] == 34


def test_request_tool_approval_uses_interrupt(monkeypatch) -> None:
    from agent.graph.nodes import hitl_helpers

    monkeypatch.setattr(hitl_helpers, "interrupt", lambda payload: {"action": "approve"})

    response = hitl_helpers.request_tool_approval(
        tool_id="network.nmap",
        tool_name="Nmap",
        parameters={"target": "192.168.1.1"},
    )
    assert response["action"] == "approve"


def test_normalize_tool_approval_response_defaults_to_approve() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_tool_approval_response

    assert normalize_tool_approval_response(None) == {"action": "approve"}
    assert normalize_tool_approval_response({"action": "unexpected"})["action"] == "approve"


def test_normalize_tool_approval_response_edit_requires_dict() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_tool_approval_response

    normalized = normalize_tool_approval_response({"action": "edit", "edited_parameters": "bad"})
    assert normalized["action"] == "edit"
    assert normalized["edited_parameters"] == {}


def test_should_require_plan_approval_explicit_plan_profile(monkeypatch) -> None:
    """Plan profile requires plan approval when HITL is enabled."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_plan_approval(
            {"agent_mode": "agent", "plan_review_required": True}
        )
        is True
    )


def test_should_require_plan_approval_disabled_when_not_required(monkeypatch) -> None:
    """Non-Plan turns do not request plan review interrupts."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_plan_approval(
            {"agent_mode": "agent", "plan_review_required": False}
        )
        is False
    )


def test_should_require_plan_approval_feature_flag_disabled(monkeypatch) -> None:
    """HITL feature flag disables plan approval even for Plan profile."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "false")
    helpers = _reload_helpers()
    assert helpers.should_require_plan_approval({"plan_review_required": True}) is False


def test_should_require_plan_approval_missing_flag_uses_plan_mode_fallback(
    monkeypatch,
) -> None:
    """Older metadata falls back to plan_mode during migration."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert helpers.should_require_plan_approval({"plan_mode": True}) is True
    assert helpers.should_require_plan_approval({"plan_mode": False}) is False
    assert helpers.should_require_plan_approval({}) is False


def test_should_require_plan_approval_explicit_flag_overrides_plan_mode(
    monkeypatch,
) -> None:
    """Explicit profile metadata is authoritative over legacy fallback keys."""
    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    helpers = _reload_helpers()
    assert (
        helpers.should_require_plan_approval(
            {"plan_review_required": False, "plan_mode": True}
        )
        is False
    )


def test_build_plan_review_payload() -> None:
    from agent.graph.nodes.hitl_helpers import build_plan_review_payload

    payload = build_plan_review_payload(
        goal="Test goal",
        plan_steps=["Step 1", "Step 2"],
        todo_list=["Todo 1", "Todo 2"],
        reasoning="Because...",
        turn_sequence=7,
        turn_id="task-2-turn-7",
        reserved_message_id=89,
    )
    assert payload["type"] == "plan_review"
    assert payload["goal"] == "Test goal"
    assert len(payload["plan_steps"]) == 2
    assert len(payload["todo_list"]) == 2
    assert "id" in payload["todo_list"][0]
    assert payload["todo_list"][0]["status"] == "pending"
    assert payload["turn_sequence"] == 7
    assert payload["turn_id"] == "task-2-turn-7"
    assert payload["reserved_message_id"] == 89


def test_request_plan_approval_calls_interrupt(monkeypatch) -> None:
    from agent.graph.nodes import hitl_helpers

    monkeypatch.setattr(hitl_helpers, "interrupt", lambda payload: {"action": "approve"})

    response = hitl_helpers.request_plan_approval(
        goal="Test",
        plan_steps=["Step 1"],
        todo_list=["Todo 1"],
    )
    assert response["action"] == "approve"


def test_build_clarify_request_payload() -> None:
    from agent.graph.nodes.hitl_helpers import build_clarify_request_payload

    payload = build_clarify_request_payload(
        required_blockers=[
            {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["10.0.0.1", "10.0.0.2"],
            },
            {
                "slot": "scan_mode",
                "question": "Which mode?",
                "input_type": "select",
                "options": ["quick", "full"],
            },
        ],
        context_metadata={"source": "planner"},
        turn_sequence=3,
        turn_id="task-1-turn-3",
        reserved_message_id=55,
    )
    assert payload["type"] == "clarify_request"
    assert len(payload["questions"]) == 2
    assert payload["questions"][0]["question_id"] == "target"
    assert payload["questions"][0]["input_type"] == "select"
    assert payload["questions"][0]["options"] == ["10.0.0.1", "10.0.0.2"]
    assert payload["questions"][1]["input_type"] == "select"
    assert payload["questions"][1]["options"] == ["quick", "full"]
    assert payload["turn_sequence"] == 3
    assert payload["turn_id"] == "task-1-turn-3"
    assert payload["reserved_message_id"] == 55


def test_request_clarify_answers_uses_interrupt(monkeypatch) -> None:
    from agent.graph.nodes import hitl_helpers

    monkeypatch.setattr(
        hitl_helpers,
        "interrupt",
        lambda payload: {"action": "answer", "answers": {"target": "10.0.0.2"}},
    )
    response = hitl_helpers.request_clarify_answers(
        required_blockers=[
            {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["10.0.0.1", "10.0.0.2"],
            }
        ],
    )
    assert response["action"] == "answer"
    assert response["answers"]["target"] == "10.0.0.2"


def test_normalize_clarify_response_defaults() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_clarify_response

    assert normalize_clarify_response(None) == {"action": "answer", "answers": {}}
    normalized = normalize_clarify_response({"action": "unexpected", "answers": "bad"})
    assert normalized["action"] == "answer"
    assert normalized["answers"] == {}


def test_normalize_required_blockers_rejects_text_input_type() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_required_blockers

    normalized = normalize_required_blockers(
        [
            {"slot": "target", "question": "What host should I scan?", "input_type": "text", "options": ["a"]},
        ],
        max_questions=2,
    )
    assert normalized == []


def test_normalize_required_blockers_rejects_empty_options() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_required_blockers

    normalized = normalize_required_blockers(
        [
            {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": [],
            },
        ],
        max_questions=2,
    )
    assert normalized == []


def test_normalize_required_blockers_rejects_more_than_four_options() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_required_blockers

    normalized = normalize_required_blockers(
        [
            {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["1", "2", "3", "4", "5"],
            },
        ],
        max_questions=2,
    )
    assert normalized == []


def test_normalize_required_blockers_rejects_duplicate_options() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_required_blockers

    normalized = normalize_required_blockers(
        [
            {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["10.0.0.1", "10.0.0.1"],
            },
        ],
        max_questions=2,
    )
    assert normalized == []


def test_normalize_required_blockers_accepts_valid_select_options_range() -> None:
    from agent.graph.nodes.hitl_helpers import normalize_required_blockers

    normalized = normalize_required_blockers(
        [
            {
                "slot": "target_network_cidr",
                "question": "Which /24 network CIDR should be scanned?",
                "input_type": "select",
                "options": ["172.17.0.0/24"],
            },
            {
                "slot": "target_host_selection",
                "question": "Which host should be scanned for PostgreSQL?",
                "input_type": "select",
                "options": ["first_live_host", "172.17.0.10", "172.17.0.11", "172.17.0.12"],
            },
        ],
        max_questions=2,
    )

    assert len(normalized) == 2
    assert normalized[0] == {
        "slot": "target_network_cidr",
        "question": "Which /24 network CIDR should be scanned?",
        "input_type": "select",
        "options": ["172.17.0.0/24"],
    }
    assert normalized[1] == {
        "slot": "target_host_selection",
        "question": "Which host should be scanned for PostgreSQL?",
        "input_type": "select",
        "options": ["first_live_host", "172.17.0.10", "172.17.0.11", "172.17.0.12"],
    }
