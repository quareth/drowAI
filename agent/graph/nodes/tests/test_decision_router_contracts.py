"""Contract tests for decision-router candidate and outcome metadata behavior."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from agent.graph.nodes.reflect import reflect_node
from agent.graph.nodes.decision_router.router import decision_router
from agent.providers.llm.core.exceptions import LLMConfigurationError
from agent.graph.state import (
    BudgetState,
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
)


def _base_state() -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue execution",
            capability="deep_reasoning",
            todo_list=[TodoItem(description="Continue", status=TodoStatus.IN_PROGRESS)],
            metadata={
                "turn_sequence": 7,
                "phase_sequence": 3,
            },
        ),
        trace=TraceState(),
    )


async def _route_from_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Run router from a deep-copied snapshot to avoid shared mutation."""
    return await decision_router(copy.deepcopy(snapshot))


@pytest.mark.asyncio
async def test_candidate_contract_is_canonical_over_decision_history() -> None:
    state = _base_state()
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 8,
        "remaining_tool_calls": 4,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "Need one more scan",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.decision_history = ["finalize: legacy fallback"]

    result = await decision_router(state.as_graph_state())
    facts = result["facts"]
    metadata = facts["metadata"]

    assert metadata["router_outcome"]["action"] == "call_tool"
    assert metadata["router_outcome"]["reason"] == "candidate_history_conflict"
    assert metadata["router_observability"]["last_final_action"] == "call_tool"
    assert metadata.get("candidate_decision") is None
    # No duplicate append for accepted PTR candidate.
    assert facts["decision_history"] == ["finalize: legacy fallback"]


@pytest.mark.asyncio
async def test_invalid_candidate_contract_falls_back_to_history() -> None:
    state = _base_state()
    state.facts.metadata["candidate_decision"] = {
        "next_action": "reflect",
        "decision_source": "ptr",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.decision_history = ["think_more: keep progressing"]

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]

    assert metadata["router_outcome"]["action"] == "think_more"
    assert metadata["router_outcome"]["reason"] == "candidate_invalid_candidate_id_history_fallback"
    assert metadata["router_outcome"]["candidate_source"] == "legacy_compatibility"
    assert metadata.get("candidate_decision") is None


@pytest.mark.asyncio
async def test_candidate_is_consumed_after_single_router_pass() -> None:
    state = _base_state()
    state.facts.metadata["candidate_decision"] = {
        "next_action": "think_more",
        "action_reasoning": "Need one planning step",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    first = await decision_router(state.as_graph_state())
    assert first["facts"]["metadata"]["router_outcome"]["action"] == "think_more"
    assert first["facts"]["metadata"].get("candidate_decision") is None

    second = await decision_router(first)
    assert second["facts"]["metadata"]["router_outcome"]["action"] == "finalize"
    assert second["facts"]["metadata"]["router_outcome"]["reason"] == "candidate_missing"


@pytest.mark.asyncio
async def test_binding_mismatch_candidate_falls_back_to_history_reason() -> None:
    state = _base_state()
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "stale candidate",
        "decision_source": "ptr",
        "candidate_id": "ptr-old",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 6,
        "phase_sequence": 2,
    }
    state.facts.decision_history = ["think_more: continue with prior plan"]

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]

    assert metadata["router_outcome"]["action"] == "think_more"
    assert metadata["router_outcome"]["reason"] == "candidate_invocation_binding_mismatch_history_fallback"
    assert metadata["router_outcome"]["candidate_source"] == "legacy_compatibility"
    assert metadata["router_outcome"]["resolution_source"] == "fallback"
    assert metadata.get("candidate_decision") is None


@pytest.mark.asyncio
async def test_terminal_path_writes_router_outcome_contract() -> None:
    state = _base_state()
    state.facts.metadata["user_goal_achieved"] = True

    result = await decision_router(state.as_graph_state())
    outcome = result["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "finalize"
    assert outcome["candidate_action"] is None
    assert outcome["candidate_source"] == "terminal_state"
    assert outcome["resolution_source"] == "terminal_state"


@pytest.mark.asyncio
async def test_reflect_recovery_prefers_canonical_metadata_hint() -> None:
    state = _base_state()
    state.facts.iterations = 5
    state.facts.metadata["next_after_reflect"] = {
        "action": "think_more",
        "hint_id": "reflect-7",
        "issued_at_iteration": 5,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "stale ptr candidate during reflect recovery",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.post_reflect_action = "call_tool"

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "think_more"
    assert metadata["router_outcome"]["candidate_source"] == "reflect_hint"
    assert metadata["router_outcome"]["reason"] == "post_reflect_hint_consumed"
    assert metadata.get("candidate_decision") is None
    assert metadata.get("next_after_reflect") is None
    assert result["facts"]["post_reflect_action"] is None
    assert observability["last_consumed_reflect_hint_id"] == "reflect-7"


@pytest.mark.asyncio
async def test_reflect_stamps_post_budget_iteration_and_router_consumes_hint() -> None:
    state = _base_state()
    state.facts.iterations = 4
    state.facts.todo_list = []

    with patch(
        "agent.graph.nodes.reflect.resolve_llm_client",
        side_effect=LLMConfigurationError("missing llm config"),
    ):
        reflected = await reflect_node(state.as_graph_state())

    reflected_facts = reflected["facts"]
    reflected_metadata = reflected_facts["metadata"]
    hint = reflected_metadata["next_after_reflect"]
    current_iteration = reflected_facts["iterations"]

    assert current_iteration == 5
    assert hint["issued_at_iteration"] == current_iteration
    assert hint["hint_id"] == f"reflect-{current_iteration}-think_more"

    routed = await decision_router(reflected)
    outcome = routed["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "think_more"
    assert outcome["candidate_source"] == "reflect_hint"
    assert outcome["reason"] == "post_reflect_hint_consumed"
    assert outcome["reason"] != "reflect_recovery_invalid_hint"


@pytest.mark.asyncio
async def test_reflect_compat_hint_is_normalized_to_canonical_id() -> None:
    state = _base_state()
    state.facts.iterations = 9
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 6,
        "remaining_tool_calls": 3,
    }
    state.facts.post_reflect_action = " Call Tool "

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "call_tool"
    assert metadata["router_outcome"]["candidate_source"] == "reflect_hint"
    assert result["facts"]["post_reflect_action"] is None
    assert (
        observability["last_consumed_reflect_hint_id"]
        == "compat-reflect-9-call_tool"
    )


@pytest.mark.asyncio
async def test_reflect_hint_is_consumed_before_guardrail_early_return() -> None:
    state = _base_state()
    state.facts.iterations = 3
    state.facts.metadata["next_after_reflect"] = {
        "action": "call_tool",
        "hint_id": "reflect-loop-3",
        "issued_at_iteration": 3,
    }
    state.facts.metadata["runtime_budgets"] = {"remaining_iterations": 0}
    state.facts.post_reflect_action = "think_more"

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "finalize"
    assert metadata["router_outcome"]["reason"] == "budget_exhausted_conflict_iterations"
    assert metadata["router_outcome"]["candidate_action"] == "call_tool"
    assert metadata["router_outcome"]["candidate_source"] == "reflect_hint"
    assert metadata["router_outcome"]["resolution_source"] == "guardrail"
    assert metadata.get("next_after_reflect") is None
    assert result["facts"]["post_reflect_action"] is None
    assert observability["last_consumed_reflect_hint_id"] == "reflect-loop-3"


@pytest.mark.asyncio
async def test_budget_conflict_uses_most_restrictive_model_with_reason_tag() -> None:
    state = _base_state()
    state.facts.budgets = BudgetState(max_tool_calls=0, max_iterations=10)
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 7,
        "remaining_tool_calls": 2,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "Need to run one more tool",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    outcome = result["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "finalize"
    assert outcome["candidate_action"] == "call_tool"
    assert outcome["reason"] == "budget_exhausted_conflict_tool_calls"
    assert outcome["resolution_source"] == "guardrail"


@pytest.mark.asyncio
async def test_bootstrapped_runtime_budgets_allow_call_tool_for_simple_tool() -> None:
    from agent.graph.builders.common_edges import ensure_metadata_runtime_budgets

    state = _base_state()
    state.facts.capability = "simple_tool_execution"
    ensure_metadata_runtime_budgets(state.facts.metadata)
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "Need one more scan",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    outcome = result["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "call_tool"
    assert outcome["reason"] == "candidate_decision_accepted"
    assert outcome["resolution_source"] == "candidate"


@pytest.mark.asyncio
async def test_unknown_budget_fails_closed_for_call_tool_only() -> None:
    state = _base_state()
    state.facts.budgets = BudgetState(max_tool_calls=None, max_iterations=None)
    state.facts.metadata["runtime_budgets"] = {}
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "Need to run one more tool",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    outcome = result["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "finalize"
    assert outcome["candidate_action"] == "call_tool"
    assert outcome["reason"] == "budget_unknown_call_tool"
    assert outcome["resolution_source"] == "guardrail"


@pytest.mark.asyncio
async def test_unknown_budget_does_not_block_non_tool_actions() -> None:
    state = _base_state()
    state.facts.budgets = BudgetState(max_tool_calls=None, max_iterations=None)
    state.facts.metadata["runtime_budgets"] = {}
    state.facts.metadata["candidate_decision"] = {
        "next_action": "reflect",
        "action_reasoning": "Need another reflection step",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    outcome = result["facts"]["metadata"]["router_outcome"]

    assert outcome["action"] == "reflect"
    assert outcome["reason"] == "candidate_decision_accepted"
    assert outcome["resolution_source"] == "candidate"


@pytest.mark.asyncio
async def test_guardrail_override_updates_observability_from_final_action() -> None:
    state = _base_state()
    state.facts.budgets = BudgetState(max_tool_calls=0, max_iterations=10)
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 7,
        "remaining_tool_calls": 2,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "Need to run one more tool",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    outcome = metadata["router_outcome"]
    observability = metadata["router_observability"]

    assert outcome["action"] == "finalize"
    assert outcome["candidate_action"] == "call_tool"
    assert observability["last_final_action"] == "finalize"
    assert observability["stuck_progression"]["action"] == "finalize"
    assert observability["stuck_progression"]["same_action_streak"] == 1


@pytest.mark.asyncio
async def test_observability_tracks_consecutive_final_actions() -> None:
    state = _base_state()
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 8,
        "remaining_tool_calls": 4,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "reflect",
        "action_reasoning": "Need another pass",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    first = await decision_router(state.as_graph_state())
    first_observability = first["facts"]["metadata"]["router_observability"]
    assert first_observability["stuck_progression"]["same_action_streak"] == 1

    first["facts"]["metadata"]["candidate_decision"] = {
        "next_action": "reflect",
        "action_reasoning": "Need another pass",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3-replay",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    second = await decision_router(first)
    second_observability = second["facts"]["metadata"]["router_observability"]

    assert second["facts"]["metadata"]["router_outcome"]["action"] == "reflect"
    assert second_observability["last_final_action"] == "reflect"
    assert second_observability["stuck_progression"]["action"] == "reflect"
    assert second_observability["stuck_progression"]["same_action_streak"] == 2


@pytest.mark.asyncio
async def test_deep_reasoning_reflection_loop_forces_synthesis_guardrail() -> None:
    state = _base_state()
    state.facts.capability = "deep_reasoning"
    state.facts.metadata["candidate_decision"] = {
        "next_action": "reflect",
        "action_reasoning": "keep reflecting",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.decision_history = [
        "reflect: first retry",
        "reflect: second retry",
    ]

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    outcome = metadata["router_outcome"]

    assert outcome["action"] == "synthesis"
    assert outcome["reason"] == "reflection_loop_synthesis"
    assert outcome["candidate_action"] == "reflect"
    assert outcome["candidate_source"] == "ptr"
    assert outcome["resolution_source"] == "guardrail"
    assert metadata.get("candidate_decision") is None
    assert result["facts"]["decision_history"][-1].startswith("synthesis:")


@pytest.mark.asyncio
async def test_invalid_reflect_hint_still_updates_consumed_hint_tracking() -> None:
    state = _base_state()
    state.facts.iterations = 4
    state.facts.metadata["next_after_reflect"] = {
        "action": "reflect",
        "hint_id": "reflect-invalid-4",
        "issued_at_iteration": 4,
    }

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "finalize"
    assert metadata["router_outcome"]["reason"] == "reflect_recovery_invalid_hint"
    assert metadata.get("next_after_reflect") is None
    assert observability["last_consumed_reflect_hint_id"] == "reflect-invalid-4"


@pytest.mark.asyncio
async def test_stale_reflect_hint_iteration_mismatch_fails_closed() -> None:
    state = _base_state()
    state.facts.iterations = 6
    state.facts.metadata["next_after_reflect"] = {
        "action": "call_tool",
        "hint_id": "reflect-stale-5",
        "issued_at_iteration": 5,
    }

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "finalize"
    assert metadata["router_outcome"]["reason"] == "reflect_recovery_invalid_hint"
    assert metadata["router_outcome"]["candidate_source"] == "reflect_hint"
    assert metadata["router_outcome"]["resolution_source"] == "fallback"
    assert metadata.get("next_after_reflect") is None
    assert observability["last_consumed_reflect_hint_id"] == "reflect-stale-5"


@pytest.mark.asyncio
async def test_replayed_reflect_hint_id_fails_closed() -> None:
    state = _base_state()
    state.facts.iterations = 6
    state.facts.metadata["next_after_reflect"] = {
        "action": "think_more",
        "hint_id": "reflect-replay-6",
        "issued_at_iteration": 6,
    }
    state.facts.metadata["router_observability"] = {
        "last_consumed_reflect_hint_id": "reflect-replay-6"
    }

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    observability = metadata["router_observability"]

    assert metadata["router_outcome"]["action"] == "finalize"
    assert metadata["router_outcome"]["reason"] == "reflect_recovery_invalid_hint"
    assert metadata["router_outcome"]["candidate_source"] == "reflect_hint"
    assert metadata["router_outcome"]["resolution_source"] == "fallback"
    assert metadata.get("next_after_reflect") is None
    assert observability["last_consumed_reflect_hint_id"] == "reflect-replay-6"


@pytest.mark.asyncio
async def test_planner_entrypoint_starts_execution_once() -> None:
    state = _base_state()
    state.facts.metadata["planner_mode"] = "plan_ready"

    first = await decision_router(state.as_graph_state())
    first_metadata = first["facts"]["metadata"]
    assert first_metadata["router_outcome"]["action"] == "call_tool"
    assert first_metadata["router_outcome"]["reason"] == "planner_entrypoint_start_execution"
    assert first_metadata["planner_entrypoint_consumed"] is True

    second = await decision_router(first)
    assert second["facts"]["metadata"]["router_outcome"]["action"] == "finalize"
    assert second["facts"]["metadata"]["router_outcome"]["reason"] == "budget_unknown_call_tool"


@pytest.mark.asyncio
async def test_reflect_recovery_invalid_hint_consumes_stale_candidate() -> None:
    state = _base_state()
    state.facts.iterations = 4
    state.facts.metadata["next_after_reflect"] = {
        "action": "reflect",
        "hint_id": "reflect-invalid-hint",
        "issued_at_iteration": 4,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "candidate should be ignored in reflect recovery",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.decision_history = ["think_more: legacy compatibility fallback"]

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]

    assert metadata["router_outcome"]["action"] == "finalize"
    assert metadata["router_outcome"]["reason"] == "reflect_recovery_invalid_hint"
    assert metadata.get("candidate_decision") is None
    assert result["facts"]["decision_history"][-1].startswith("finalize:")


@pytest.mark.asyncio
async def test_simple_tool_synthesis_candidate_is_profile_normalized_to_finalize() -> None:
    state = _base_state()
    state.facts.capability = "simple_tool_execution"
    state.facts.metadata["candidate_decision"] = {
        "next_action": "synthesis",
        "action_reasoning": "Summarize tool output",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }

    result = await decision_router(state.as_graph_state())
    metadata = result["facts"]["metadata"]
    outcome = metadata["router_outcome"]

    assert outcome["action"] == "finalize"
    assert outcome["reason"] == "profile_normalized_simple_tool_synthesis_to_finalize"
    assert outcome["candidate_action"] == "synthesis"
    assert outcome["candidate_source"] == "ptr"
    assert outcome["resolution_source"] == "profile_normalization"
    assert metadata.get("candidate_decision") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "candidate_action", "expected_action", "expected_reason"),
    [
        ("deep_reasoning", "reflect", "reflect", "candidate_decision_accepted"),
        (
            "simple_tool_execution",
            "synthesis",
            "finalize",
            "profile_normalized_simple_tool_synthesis_to_finalize",
        ),
    ],
)
async def test_identical_snapshots_produce_identical_outcome_for_profiles(
    capability: str,
    candidate_action: str,
    expected_action: str,
    expected_reason: str,
) -> None:
    state = _base_state()
    state.facts.capability = capability
    state.facts.metadata["candidate_decision"] = {
        "next_action": candidate_action,
        "action_reasoning": "deterministic profile replay",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    snapshot = state.as_graph_state()

    first = await _route_from_snapshot(snapshot)
    second = await _route_from_snapshot(snapshot)

    first_outcome = first["facts"]["metadata"]["router_outcome"]
    second_outcome = second["facts"]["metadata"]["router_outcome"]
    assert first_outcome == second_outcome
    assert first_outcome["action"] == expected_action
    assert first_outcome["reason"] == expected_reason


@pytest.mark.asyncio
async def test_identical_conflict_snapshots_keep_reason_and_resolution_stable() -> None:
    state = _base_state()
    state.facts.metadata["runtime_budgets"] = {
        "remaining_iterations": 8,
        "remaining_tool_calls": 4,
    }
    state.facts.metadata["candidate_decision"] = {
        "next_action": "call_tool",
        "action_reasoning": "need another scan",
        "decision_source": "ptr",
        "candidate_id": "ptr-7-3",
        "producer_node": "post_tool_reasoning",
        "turn_sequence": 7,
        "phase_sequence": 3,
    }
    state.facts.decision_history = ["reflect: stale history action"]
    snapshot = state.as_graph_state()

    first = await _route_from_snapshot(snapshot)
    second = await _route_from_snapshot(snapshot)

    first_outcome = first["facts"]["metadata"]["router_outcome"]
    second_outcome = second["facts"]["metadata"]["router_outcome"]
    assert first_outcome["reason"] == "candidate_history_conflict"
    assert first_outcome["resolution_source"] == "candidate"
    assert second_outcome == first_outcome
