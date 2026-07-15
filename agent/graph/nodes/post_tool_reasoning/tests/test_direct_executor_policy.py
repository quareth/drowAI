"""Targeted tests for ``apply_direct_executor_policy``.

Purpose
-------
Characterize the bounded continuation rules for the direct-executor branch in
isolation, without invoking the full PTR node. The policy module is the single
seat of responsibility for:

    (1) goal_achieved coercion to finalize,
    (2) budget_exhausted coercion to finalize,
    (3) repeated_no_progress downgrade to reflect for todo-free flows using
        exact ``tool_intent`` repetition (description, target, focus), because
        empty ``todo_progress`` is EXPECTED in todo-free direct-executor chains
        (e.g. ping -> nmap) and todo-backed no-progress is owned by
        ``todo_stall_guard``,
    (4) passthrough for failure-recovery retries,
    (5) no-op for non direct-executor capabilities.

These tests exercise each rule directly against ``PostToolReasoningOutput`` and
``InteractiveState`` fixtures. They also verify that the policy does NOT
introduce a dedicated ``direct_executor_tracking`` metadata key — loop tracking
remains grounded in existing state (``last_post_tool_action``, prior
``tool_intent`` metadata, ``tool_calls_used``, ``todo_progress``,
``user_goal_achieved``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningOutput,
    ToolIntent,
)
from agent.graph.nodes.post_tool_reasoning.policies.direct_executor import (
    apply_direct_executor_policy,
)
from agent.graph.state import (
    BudgetState,
    FactsState,
    InteractiveState,
    TodoItem,
    TodoStatus,
    TraceState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(
    *,
    metadata: Optional[Dict[str, Any]] = None,
    capability: str = "simple_tool_execution",
    tool_calls_used: int = 0,
    max_tool_calls: Optional[int] = None,
    todo_list: Optional[List[TodoItem]] = None,
) -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message="goal",
        capability=capability,
        conversation_id="conv-1",
        metadata=dict(metadata or {}),
        budgets=BudgetState(max_tool_calls=max_tool_calls),
        tool_calls_used=tool_calls_used,
        todo_list=list(todo_list or []),
    )
    return InteractiveState(facts=facts, trace=TraceState())


_DEFAULT_INTENT_DESCRIPTION = "probe next surface"
_DEFAULT_INTENT_TARGET: Optional[str] = None
_DEFAULT_INTENT_FOCUS = "direct_executor_test"


def _make_call_tool_output(
    *,
    user_goal_achieved: bool = False,
    todo_progress: Optional[List[Any]] = None,
    failure_detected: bool = False,
    retry_suggested: bool = False,
    description: str = _DEFAULT_INTENT_DESCRIPTION,
    target: Optional[str] = _DEFAULT_INTENT_TARGET,
    focus: str = _DEFAULT_INTENT_FOCUS,
) -> PostToolReasoningOutput:
    return PostToolReasoningOutput(
        observation="Ran a tool and observed a result of some kind here.",
        next_action="call_tool",
        action_reasoning="follow-up tool call is justified",
        tool_intent=ToolIntent(
            description=description,
            target=target,
            focus=focus,
        ),
        user_goal_achieved=user_goal_achieved,
        todo_progress=todo_progress or [],
        failure_detected=failure_detected,
        retry_suggested=retry_suggested,
    )


def _prior_intent_metadata(
    *,
    description: str = _DEFAULT_INTENT_DESCRIPTION,
    target: Optional[str] = _DEFAULT_INTENT_TARGET,
    focus: str = _DEFAULT_INTENT_FOCUS,
) -> Dict[str, Any]:
    """Build the ``metadata["tool_intent"]`` dict PTR would persist previously."""
    return {
        "description": description,
        "target": target,
        "focus": focus,
    }


def _make_finalize_output() -> PostToolReasoningOutput:
    return PostToolReasoningOutput(
        observation="All required information is in hand; ready to finalize.",
        next_action="finalize",
        action_reasoning="user goal satisfied",
        tool_intent=None,
        user_goal_achieved=True,
    )


# ---------------------------------------------------------------------------
# Goal achieved
# ---------------------------------------------------------------------------


def test_one_shot_success_untouched() -> None:
    """A clean one-shot finalize remains a finalize (no mutation)."""
    state = _make_state()
    output = _make_finalize_output()

    apply_direct_executor_policy(state, output)

    assert output.next_action == "finalize"
    assert output.user_goal_achieved is True
    assert output.tool_intent is None
    # No new metadata key was introduced.
    assert "direct_executor_tracking" not in state.facts.metadata


def test_goal_achieved_coerces_trailing_call_tool_to_finalize() -> None:
    """LLM flagged goal achieved but still emitted call_tool -> finalize."""
    state = _make_state()
    output = _make_call_tool_output(user_goal_achieved=True)

    apply_direct_executor_policy(state, output)

    assert output.next_action == "finalize"
    assert output.tool_intent is None
    assert output.retry_suggested is False
    assert output.user_goal_achieved is True
    assert "Override: direct-executor goal_achieved" in output.action_reasoning


# ---------------------------------------------------------------------------
# Budget exhausted
# ---------------------------------------------------------------------------


def test_budget_exhaustion_forces_finalize() -> None:
    """call_tool with used >= max_tool_calls is coerced to finalize."""
    state = _make_state(tool_calls_used=3, max_tool_calls=3)
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    assert output.next_action == "finalize"
    assert output.tool_intent is None
    assert output.retry_suggested is False
    assert "Override: direct-executor budget_exhausted" in output.action_reasoning


def test_missing_budget_does_not_trigger_exhaustion() -> None:
    """A None max_tool_calls means no budget; policy must not coerce."""
    state = _make_state(tool_calls_used=99, max_tool_calls=None)
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    # Still call_tool — budget is not exhausted when none is set.
    assert output.next_action == "call_tool"


# ---------------------------------------------------------------------------
# Repeated no-progress
# ---------------------------------------------------------------------------


def test_todo_driven_flow_with_no_progress_defers_to_todo_stall_guard() -> None:
    """Todo-backed no-progress is not reflected by direct-executor policy."""
    pending_todo = TodoItem(description="do the thing", status=TodoStatus.PENDING)
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},
        todo_list=[pending_todo],
    )
    output = _make_call_tool_output()  # no todo_progress reported

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"
    assert output.tool_intent is not None
    assert "repeated_no_progress" not in output.action_reasoning


def test_first_tool_call_is_not_flagged_as_no_progress() -> None:
    """With no previous action marker, call_tool must NOT be coerced."""
    state = _make_state(metadata={})  # no last_post_tool_action
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"
    assert output.tool_intent is not None


def test_progress_this_turn_blocks_no_progress_override_in_todo_flow() -> None:
    """Any reported todo progress this turn clears the no-progress flag."""
    from agent.graph.nodes.post_tool_reasoning.models import TodoProgress

    pending_todo = TodoItem(description="do the thing", status=TodoStatus.PENDING)
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},
        todo_list=[pending_todo],
    )
    progress = TodoProgress(index=0, status="in_progress")
    output = _make_call_tool_output(todo_progress=[progress])

    apply_direct_executor_policy(state, output)

    # Policy must respect reported progress.
    assert output.next_action == "call_tool"


def test_todo_free_flow_different_next_step_is_not_flagged() -> None:
    """ping -> nmap style chain: different tool_intent must NOT be flagged.

    Todo-free direct-executor flows are expected to have empty
    ``todo_progress``. The only legitimate stuck signal is re-proposing the
    SAME step. A genuinely different next step (different description /
    target / focus) is exactly the progressive execution this branch exists
    to support.
    """
    state = _make_state(
        metadata={
            "last_post_tool_action": "call_tool",
            # Previous turn executed a ping-style intent.
            "tool_intent": _prior_intent_metadata(
                description="ping the host",
                target="10.0.0.5",
                focus="reachability",
            ),
        },
        todo_list=[],
    )
    # Current turn proposes a clearly different step (nmap on the live host).
    output = _make_call_tool_output(
        description="scan open ports",
        target="10.0.0.5",
        focus="port enumeration",
    )

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"
    assert output.tool_intent is not None
    assert output.tool_intent.description == "scan open ports"


def test_seeded_simple_tool_todo_with_no_progress_defers_to_todo_stall_guard() -> None:
    """Task-seeded todos do not make direct-executor reflect immediately.

    Working-memory seeding gives simple-tool the existing todo progression
    model. Reflection for empty todo progress must still wait for the shared
    active-todo stall guard threshold.
    """
    seeded_todo = TodoItem(
        description="Find online hosts",
        status=TodoStatus.IN_PROGRESS,
    )
    state = _make_state(
        metadata={
            "last_post_tool_action": "call_tool",
            "tool_intent": _prior_intent_metadata(
                description="discover live hosts",
                target="10.0.0.0/24",
                focus="host discovery",
            ),
            "working_memory": {
                "intent_brief": {
                    "original_goal": (
                        "Find online hosts, then scan one online host for PostgreSQL"
                    ),
                    "resolved_user_intent": "Scan for PostgreSQL after host discovery",
                }
            },
        },
        todo_list=[seeded_todo],
    )
    output = _make_call_tool_output(
        description="retry host discovery with a different probe",
        target="10.0.0.0/24",
        focus="host discovery",
    )

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"
    assert output.tool_intent is not None
    assert output.tool_intent.focus == "host discovery"
    assert state.facts.todo_list == [seeded_todo]
    assert "direct_executor_tracking" not in state.facts.metadata


def test_todo_free_flow_same_next_step_triggers_reflect() -> None:
    """Todo-free flow where the LLM re-proposes the exact step just executed.

    Same description + target + focus as the previously-executed intent is
    the repetition signal that replaces the old "empty todo_progress"
    heuristic for todo-free direct-executor flows.
    """
    state = _make_state(
        metadata={
            "last_post_tool_action": "call_tool",
            "tool_intent": _prior_intent_metadata(),  # defaults match output
        },
        todo_list=[],
    )
    output = _make_call_tool_output()  # default intent matches prior exactly

    apply_direct_executor_policy(state, output)

    assert output.next_action == "reflect"
    assert "Override: direct-executor repeated_no_progress" in output.action_reasoning


def test_todo_free_flow_missing_prior_intent_is_not_flagged() -> None:
    """Without a recorded prior tool_intent, policy cannot claim repetition."""
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},  # no tool_intent key
        todo_list=[],
    )
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    # Empty todo_progress alone is NOT a stuck signal for todo-free flows.
    assert output.next_action == "call_tool"


def test_multi_turn_budget_eventually_stops_todo_free_repetition() -> None:
    """Layered defense: same-step repetition -> reflect; budget -> finalize.

    Turn 1: LLM re-proposes the exact intent that was just executed -> reflect.
    Turn 2: prior action is now reflect, policy stays hands-off on call_tool.
    Turn 3: budget saturated -> finalize regardless of intent comparison.
    """
    # Turn 1: todo-free, prior intent matches current -> reflect.
    state = _make_state(
        metadata={
            "last_post_tool_action": "call_tool",
            "tool_intent": _prior_intent_metadata(),
        },
        tool_calls_used=1,
        max_tool_calls=3,
        todo_list=[],
    )
    output1 = _make_call_tool_output()
    apply_direct_executor_policy(state, output1)
    assert output1.next_action == "reflect"

    # Turn 2: previous action was reflect, not call_tool -> passthrough.
    state.facts.metadata["last_post_tool_action"] = "reflect"
    output2 = _make_call_tool_output()
    apply_direct_executor_policy(state, output2)
    assert output2.next_action == "call_tool"

    # Turn 3: budget saturated -> finalize regardless.
    state.facts.tool_calls_used = 3
    state.facts.metadata["last_post_tool_action"] = "call_tool"
    output3 = _make_call_tool_output()
    apply_direct_executor_policy(state, output3)
    assert output3.next_action == "finalize"
    assert "budget_exhausted" in output3.action_reasoning


def test_all_todos_terminal_coerces_call_tool_to_finalize() -> None:
    """Every todo terminal + stray call_tool -> finalize directly.

    Direct-executor cannot defer this case to ``request_contract.py`` because
    that policy only forces finalize for ``terminal_when == "determined"``
    requests. For ordinary ``all_steps_done`` plans the defer would leave the
    stray ``call_tool`` in place and re-enter the graph loop.
    """
    complete_todo = TodoItem(
        description="done",
        status=TodoStatus.COMPLETE_POSITIVE,
    )
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},
        todo_list=[complete_todo],
    )
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    assert output.next_action == "finalize"
    assert output.tool_intent is None
    assert output.retry_suggested is False
    # We intentionally do NOT force user_goal_achieved: the terminal status
    # mix may include skips/negatives. If the request-contract policy decides
    # this was a determined ask it will set the flag downstream.
    assert "Override: direct-executor todos_terminal" in output.action_reasoning


def test_all_todos_terminal_does_not_fire_when_first_pass() -> None:
    """``todos_terminal`` is gated on ``next_action == "call_tool"``.

    A turn that already finalized (or reflected) must not be touched by the
    new stop criterion even if every todo happens to be terminal.
    """
    complete_todo = TodoItem(
        description="done",
        status=TodoStatus.COMPLETE_POSITIVE,
    )
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},
        todo_list=[complete_todo],
    )
    output = _make_finalize_output()  # already finalize

    apply_direct_executor_policy(state, output)

    assert output.next_action == "finalize"
    assert "todos_terminal" not in output.action_reasoning


def test_todo_free_flow_without_prior_call_tool_marker_is_untouched() -> None:
    """A todo-free flow where the prior PTR was not call_tool -> passthrough.

    Even if a stale ``tool_intent`` lingers from an earlier turn, the
    ``last_post_tool_action`` guard ensures the repetition signal only fires
    immediately after a real tool call.
    """
    state = _make_state(
        metadata={
            "last_post_tool_action": "reflect",
            "tool_intent": _prior_intent_metadata(),
        },
        todo_list=[],
    )
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"


# ---------------------------------------------------------------------------
# Failure-recovery passthrough
# ---------------------------------------------------------------------------


def test_failure_recovery_retry_is_untouched() -> None:
    """Failure-recovery retries yield to the generic failure pipeline."""
    state = _make_state(
        metadata={"last_post_tool_action": "call_tool"},
        tool_calls_used=5,
        max_tool_calls=5,  # exhausted, but must NOT override a failure retry
    )
    output = _make_call_tool_output(
        failure_detected=True,
        retry_suggested=True,
    )

    apply_direct_executor_policy(state, output)

    # Policy is a no-op: it must not touch a failure-recovery retry even
    # when budget is exhausted. The retry pipeline owns that decision.
    assert output.next_action == "call_tool"
    assert output.failure_detected is True
    assert output.retry_suggested is True


# ---------------------------------------------------------------------------
# Non-applicable capability
# ---------------------------------------------------------------------------


def test_deep_reasoning_capability_is_ignored() -> None:
    """Policy must NOT apply outside the direct-executor capability."""
    state = _make_state(capability="deep_reasoning", tool_calls_used=99, max_tool_calls=1)
    output = _make_call_tool_output(user_goal_achieved=True)

    apply_direct_executor_policy(state, output)

    # No coercion — deep_reasoning owns its own policies.
    assert output.next_action == "call_tool"
    assert output.user_goal_achieved is True


def test_missing_capability_is_ignored() -> None:
    """A missing capability is treated as non-applicable (defense-in-depth)."""
    state = _make_state(capability="", tool_calls_used=99, max_tool_calls=1)
    output = _make_call_tool_output(user_goal_achieved=True)

    apply_direct_executor_policy(state, output)

    assert output.next_action == "call_tool"


# ---------------------------------------------------------------------------
# State hygiene
# ---------------------------------------------------------------------------


def test_policy_never_writes_direct_executor_tracking_metadata() -> None:
    """Contract: no ``direct_executor_tracking`` key is ever introduced."""
    state = _make_state(metadata={"last_post_tool_action": "call_tool"})
    output = _make_call_tool_output()

    apply_direct_executor_policy(state, output)

    assert "direct_executor_tracking" not in state.facts.metadata
