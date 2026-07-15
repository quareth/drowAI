"""Routing guard tests for simple tool post-tool decisions.

Under the bounded direct-executor contract (Task 1.2 + Task 3.2), the builder
is routing-only: it honors the decision already produced by PTR instead of
reconstructing the legacy single-step "failure-recovery-only" policy inline.
Stop criteria (goal achieved, budget exhausted, repeated no-progress) are
enforced upstream by
``policies.direct_executor.apply_direct_executor_policy``, so any ``call_tool``
decision that reaches the builder is expected to route to
``select_tool_categories``. Terminal finalize for short/binary asks is handled
by the request-contract policy before the builder sees the decision.
"""

from agent.graph.builders.simple_tool_builder import _route_after_router
from agent.graph.state import InteractiveInput


def test_route_after_post_tool_skipped() -> None:
    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={"tool_skipped": True},
    )
    interactive = payload.to_state()
    interactive.facts.metadata["router_outcome"] = {"action": "finalize"}
    assert _route_after_router(interactive) == "format_results"


def test_route_allows_call_tool_for_retry_recovery() -> None:
    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={"failure_detected": True, "retry_suggested": True},
    )
    interactive = payload.to_state()
    interactive.facts.metadata["router_outcome"] = {"action": "call_tool"}
    assert _route_after_router(interactive) == "select_tool_categories"


def test_route_allows_bounded_continuation_call_tool_without_retry_context() -> None:
    """Bounded direct-executor contract: ``call_tool`` decisions that reach the
    builder are honored, regardless of retry context. The legacy single-step
    guardrail (non-recovery ``call_tool`` → ``format_results``) has been
    replaced by upstream stop criteria in the direct-executor policy.
    """
    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={"failure_detected": False, "retry_suggested": False},
    )
    interactive = payload.to_state()
    interactive.facts.metadata["router_outcome"] = {"action": "call_tool"}
    assert _route_after_router(interactive) == "select_tool_categories"


def test_route_allows_call_tool_when_retry_is_rejected() -> None:
    """When the retry pipeline rejects the suggestion (budget exhausted or
    request-contract terminal), it rewrites the PTR decision away from
    ``call_tool`` before routing. If ``call_tool`` still reaches the builder,
    the bounded-continuation contract routes it forward rather than forcing
    finalize here.
    """
    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={"failure_detected": True, "retry_suggested": False},
    )
    interactive = payload.to_state()
    interactive.facts.metadata["router_outcome"] = {"action": "call_tool"}
    assert _route_after_router(interactive) == "select_tool_categories"
