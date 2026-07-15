"""Direct-executor bounded continuation policy for post-tool reasoning.

Purpose
-------
Enforce bounded continuation rules for the LLM-facing ``direct_executor``
branch (internal canonical key ``simple_tool_execution``). This module is the
single seat of responsibility for deciding whether a direct-executor turn may
continue with a follow-up tool call or must stop after the current step, based
exclusively on existing runtime state.

Responsibility boundary
-----------------------
- This module ONLY adjusts fields on a ``PostToolReasoningOutput`` instance:
  ``next_action``, ``retry_suggested``, ``tool_intent``, ``user_goal_achieved``,
  and ``action_reasoning``.
- It does NOT select tools, generate parameters, or produce observations.
- It does NOT duplicate logic owned by other policies:
    * Failure detection and retry accounting live in
      ``post_tool_reasoning/core/failure_detection.py`` and
      ``post_tool_reasoning/core/retry_logic.py``.
    * Short/binary terminal overrides live in
      ``policies/request_contract.py``.
- The module respects the documented policy composition order:
  (1) generic failure/retry analysis,
  (2) direct-executor continuation/stop (this module),
  (3) request-contract terminal override.

Note: a previous "explicit-request correction" policy that coerced LLM
``finalize`` decisions to ``call_tool`` based on regex/keyword matching
over the user message has been removed. The LLM is the sole authority
for intent classification; the contract evaluator
(``policies/intent_contract/matching._evaluate_simple_tool_intent_contract``)
still runs and surfaces "expected vs executed" via metadata so the LLM
can read it on the next turn.

Stop criteria encoded here
--------------------------
- ``goal_achieved``      : the LLM marked the user goal as satisfied. Any
                          lingering ``call_tool`` decision is coerced to
                          ``finalize``.
- ``budget_exhausted``   : ``facts.tool_calls_used`` has reached
                          ``facts.budgets.max_tool_calls``. Any ``call_tool``
                          decision is coerced to ``finalize``.
- ``todos_terminal``     : a ``todo_list`` exists and every item is already
                          terminal, yet the LLM still emitted ``call_tool``.
                          The plan is done; the policy coerces to
                          ``finalize`` directly. This is authoritative — it
                          does NOT defer to ``request_contract.py``, which
                          only overrides to finalize for
                          ``terminal_when == "determined"`` requests and
                          would leave ordinary ``all_steps_done`` plans
                          looping.
- ``repeated_no_progress`` : the previous PTR action was also ``call_tool``
                          and there is a concrete repetition signal. The
                          signal is branch-aware:
                          * If a ``todo_list`` exists (and has actionable
                            items — the all-terminal case is handled by
                            ``todos_terminal`` above), this policy does not
                            force reflection. Active-todo no-progress is
                            owned by ``todo_stall_guard`` and its 3-phase
                            threshold.
                          * If the flow is todo-free (the common case for
                            direct-executor chains like ping -> nmap), empty
                            ``todo_progress`` is EXPECTED and is NOT a
                            stuck signal; instead, the policy fires only
                            when the LLM re-proposes the SAME next step that
                            was just executed (same ``tool_intent``
                            description/target/focus).
                          Downgrades ``call_tool`` to ``reflect`` rather than
                          finalizing so the shared reasoning seam can
                          recover.
- ``determined_answer``  : delegated to ``_apply_request_contract_policy``;
                          this module does not reimplement it.

Failure-recovery retries are preserved untouched: when the current output
carries ``failure_detected`` and ``retry_suggested`` the policy yields to the
failure pipeline.

No dedicated metadata structure
-------------------------------
No ``direct_executor_tracking`` key is introduced. Every signal the policy
needs already exists elsewhere in the state:

- ``facts.tool_calls_used`` / ``facts.budgets.max_tool_calls`` for budget.
- ``facts.metadata["last_post_tool_action"]`` (written by PTR every turn)
  as the single source of truth for "was the previous step a tool call?".
- ``facts.metadata["tool_intent"]`` (also written by PTR every turn when a
  ``call_tool`` decision was made) as the single source of truth for
  "what tool step did the previous turn propose and execute?". Used to
  detect exact-repetition in todo-free flows.
- ``output.user_goal_achieved`` as this-turn success signal.
- ``output.todo_progress`` is consumed by the shared todo-progress and
  active-todo stall guard path after this policy runs; this module does not
  duplicate that no-progress threshold.
- ``facts.todo_list`` + ``TodoStatus`` for terminal-todo deferral.

Because ``tool_calls_used`` provides a hard upper bound per turn, reflect ->
call_tool cycles remain budget-bounded and cannot loop forever. If a future
requirement demands multi-turn consecutive counting, a minimal
``direct_executor_tracking`` dict is the documented extension point — but it
is deliberately NOT added here (per the implementation guide's
"reuse-existing-state-first" and "no-duplicate-loop-counters" rules).

This module is wired into the PTR node by a subsequent task; the entry point
stays intentionally small so the builder remains routing-only.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, Optional

from ..models import PostToolReasoningOutput
from ....state import InteractiveState, TodoItem, TodoStatus

logger = logging.getLogger(__name__)

# Internal canonical branch key. LLM-facing label is ``direct_executor`` —
# runtime keys are deliberately NOT renamed in this iteration.
_DIRECT_EXECUTOR_CAPABILITY = "simple_tool_execution"

# Stop-reason tags used only for override reasoning and boundary logs.
_STOP_REASON_GOAL_ACHIEVED = "goal_achieved"
_STOP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"
_STOP_REASON_TODOS_TERMINAL = "todos_terminal"
_STOP_REASON_REPEATED_NO_PROGRESS = "repeated_no_progress"


def _is_failure_recovery(output: PostToolReasoningOutput) -> bool:
    """Return True when the current output represents a failure-recovery retry.

    Failure recovery is owned by the generic retry pipeline; this policy must
    not override or reshape it.
    """
    return bool(output.failure_detected and output.retry_suggested)


def _budget_exhausted(interactive: InteractiveState) -> bool:
    """Return True when the turn's tool-call budget has been reached.

    Reads ``facts.tool_calls_used`` and ``facts.budgets.max_tool_calls``; does
    not introduce new counters. A missing budget (``None``) is treated as no
    budget and therefore never exhausted — that matches the existing
    ``guards.within_tool_budget`` contract.
    """
    budgets = interactive.facts.budgets
    max_calls: Optional[int] = getattr(budgets, "max_tool_calls", None)
    if max_calls is None:
        return False
    return int(interactive.facts.tool_calls_used or 0) >= int(max_calls)


def _any_todo_still_actionable(interactive: InteractiveState) -> bool:
    """Return True when at least one todo is still pending or in progress.

    Used as a weak signal — the request-contract terminal policy owns the
    strong version of this check for short/binary asks. Here it only feeds
    repeated-no-progress detection.
    """
    todos: List[TodoItem] = list(interactive.facts.safe_todo_list)
    if not todos:
        return False
    actionable_values = {
        TodoStatus.PENDING.value,
        TodoStatus.IN_PROGRESS.value,
    }
    for todo in todos:
        status = getattr(todo, "status", None)
        status_value = str(getattr(status, "value", status or "")).strip().lower()
        if status_value in actionable_values:
            return True
    return False


def _previous_action_was_call_tool(metadata: Mapping[str, Any]) -> bool:
    """Return True when the previous PTR decision was another tool call.

    Uses the existing ``last_post_tool_action`` marker written by the PTR node
    at the end of each iteration. No new metadata is introduced.
    """
    previous = str(metadata.get("last_post_tool_action") or "").strip().lower()
    return previous == "call_tool"


def _normalize_intent_field(value: Any) -> str:
    """Normalize a ToolIntent field for shallow equality comparison."""
    return str(value or "").strip().lower()


def _is_same_step_as_previous(
    output: PostToolReasoningOutput,
    metadata: Mapping[str, Any],
) -> bool:
    """Return True when the LLM is re-proposing the exact step just executed.

    The PTR node writes ``metadata["tool_intent"]`` (a ``model_dump()`` of the
    chosen intent) whenever it emits ``call_tool``. On the next turn, the
    previously-executed intent is visible here while ``output.tool_intent``
    carries the LLM's new proposal. When the two match on description, target,
    and focus, the agent is repeating the same step and is stuck.

    This is the repetition signal used for todo-free direct-executor flows
    where empty ``todo_progress`` is expected and cannot be treated as a
    stuck signal on its own.
    """
    current = output.tool_intent
    if current is None:
        return False
    previous = metadata.get("tool_intent")
    if not isinstance(previous, Mapping):
        return False
    return (
        _normalize_intent_field(getattr(current, "description", None))
        == _normalize_intent_field(previous.get("description"))
        and _normalize_intent_field(getattr(current, "target", None))
        == _normalize_intent_field(previous.get("target"))
        and _normalize_intent_field(getattr(current, "focus", None))
        == _normalize_intent_field(previous.get("focus"))
    )


def _is_repeated_no_progress(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
) -> bool:
    """Detect a back-to-back call_tool that represents real repetition.

    Common guards (always required):
    - current ``output.next_action`` is ``call_tool``
    - prior PTR action was also ``call_tool`` (``last_post_tool_action``)
    - the user goal is not marked achieved in the current output
    - the current output is not a failure-recovery retry (handled elsewhere)

    Branch-aware repetition signal:
    - If a ``todo_list`` exists, active-todo no-progress is handled by
      ``todo_stall_guard`` after todo progress is applied. This policy only
      owns all-terminal finalization for todo-backed direct-executor flows.
    - If the flow is todo-free (common for direct-executor chains such as
      ping -> nmap), empty ``todo_progress`` is EXPECTED and is ignored. The
      only legitimate stuck signal is the LLM re-proposing the exact same
      step (``tool_intent``) that was just executed. Genuinely different
      next steps — which is what progressive direct execution is designed
      for — always pass through.
    """
    if output.next_action != "call_tool":
        return False
    if _is_failure_recovery(output):
        return False
    if output.user_goal_achieved:
        return False

    metadata = interactive.facts.safe_metadata
    if not _previous_action_was_call_tool(metadata):
        return False

    todo_list = interactive.facts.todo_list
    if todo_list:
        return False

    # Todo-free flow: empty todo_progress is expected, not a stuck signal.
    # The only reliable repetition signal is an identical tool_intent.
    return _is_same_step_as_previous(output, metadata)


def _coerce_to_finalize(
    output: PostToolReasoningOutput,
    *,
    reason_tag: str,
    mark_goal_achieved: bool,
) -> None:
    """Flip ``output`` to a clean finalize decision.

    Keeps field mutation local so callers can reason about the exact
    transformation applied.
    """
    output.next_action = "finalize"
    output.retry_suggested = False
    output.tool_intent = None
    if mark_goal_achieved and not output.failure_detected:
        output.user_goal_achieved = True
    output.action_reasoning = (
        f"(Override: direct-executor {reason_tag}) " + output.action_reasoning
    )


def _coerce_to_reflect(
    output: PostToolReasoningOutput,
    *,
    reason_tag: str,
) -> None:
    """Downgrade a follow-up call_tool to a reflect step.

    Reflect keeps the shared reasoning seam in charge of the recovery path
    instead of forcing a premature finalize when progress stalls.
    """
    output.next_action = "reflect"
    output.retry_suggested = False
    output.tool_intent = None
    output.action_reasoning = (
        f"(Override: direct-executor {reason_tag}) " + output.action_reasoning
    )


def apply_direct_executor_policy(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
) -> None:
    """Enforce bounded continuation rules for direct executor flows.

    This function is a no-op for non direct-executor capabilities and for
    failure-recovery retries. When it does apply, it mutates ``output`` in
    place. It never chooses tools and never generates observation text.

    Composition notes:
    - Must run AFTER generic failure/retry analysis so the post-retry
      ``failure_detected`` / ``retry_suggested`` state is visible here.
    - Must run BEFORE the request-contract terminal override so short/binary
      ask finalization remains authoritative for those flows.
    """
    # Apply only when the state has been explicitly classified as
    # direct-executor (internal key ``simple_tool_execution``). A missing
    # or unexpected capability is treated as non-applicable so this policy
    # never leaks into other flows (e.g. deep_reasoning) if the field is
    # unset for any reason.
    capability_raw = interactive.facts.capability
    if not capability_raw:
        return
    if capability_raw.strip().lower() != _DIRECT_EXECUTOR_CAPABILITY:
        return

    if _is_failure_recovery(output):
        return

    # Stop criterion: goal achieved. Respect the LLM when it declared success
    # but still emitted a follow-up call_tool — clean that up deterministically.
    if output.user_goal_achieved and output.next_action == "call_tool":
        logger.info(
            "[POST_TOOL_REASONING] Direct-executor override: %s",
            _STOP_REASON_GOAL_ACHIEVED,
        )
        _coerce_to_finalize(
            output,
            reason_tag=_STOP_REASON_GOAL_ACHIEVED,
            mark_goal_achieved=True,
        )
        return

    # Stop criterion: tool-call budget exhausted. Only touches call_tool;
    # other next_action values are already terminal or handled elsewhere.
    if output.next_action == "call_tool" and _budget_exhausted(interactive):
        logger.info(
            "[POST_TOOL_REASONING] Direct-executor override: %s "
            "(tool_calls_used=%s, max_tool_calls=%s)",
            _STOP_REASON_BUDGET_EXHAUSTED,
            interactive.facts.tool_calls_used,
            getattr(interactive.facts.budgets, "max_tool_calls", None),
        )
        _coerce_to_finalize(
            output,
            reason_tag=_STOP_REASON_BUDGET_EXHAUSTED,
            mark_goal_achieved=False,
        )
        return

    # Stop criterion: every todo in the plan is terminal and the LLM still
    # emitted call_tool. Finalize directly — the request-contract terminal
    # override only fires for ``terminal_when == "determined"`` requests, so
    # deferring here would leave ordinary ``all_steps_done`` plans looping.
    # If request-contract DOES apply downstream it will still augment
    # ``user_goal_achieved`` when appropriate; that's why we leave it alone
    # here instead of forcing it to True.
    if (
        output.next_action == "call_tool"
        and interactive.facts.todo_list
        and not _any_todo_still_actionable(interactive)
    ):
        logger.info(
            "[POST_TOOL_REASONING] Direct-executor override: %s",
            _STOP_REASON_TODOS_TERMINAL,
        )
        _coerce_to_finalize(
            output,
            reason_tag=_STOP_REASON_TODOS_TERMINAL,
            mark_goal_achieved=False,
        )
        return

    # Stop criterion: repeated non-progress. Downgrade to reflect so the
    # shared reasoning seam can replan rather than blindly retry.
    if _is_repeated_no_progress(interactive, output):
        logger.info(
            "[POST_TOOL_REASONING] Direct-executor override: %s",
            _STOP_REASON_REPEATED_NO_PROGRESS,
        )
        _coerce_to_reflect(
            output,
            reason_tag=_STOP_REASON_REPEATED_NO_PROGRESS,
        )
        return

    # Otherwise, leave the decision as produced. Bounded follow-up tool calls
    # are explicitly allowed here — builder-level routing (Task 1.2) will
    # honor call_tool decisions that reach this point.


__all__ = ["apply_direct_executor_policy"]
