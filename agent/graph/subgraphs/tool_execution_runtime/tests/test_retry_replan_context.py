"""Unit tests for checkpoint-retry tool-plan invalidation behavior.

These tests lock Task 5.2 retry/idempotency guarantees by asserting that a
checkpoint retry clears stale dispatch identities and pre-dispatch cache
markers so the next attempt cannot bind to an old command/result pair.
"""

from __future__ import annotations

from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution_runtime.retry_replanning import (
    _apply_checkpoint_retry_tool_replanning_context,
)


def test_checkpoint_retry_clears_stale_dispatch_identity_markers() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="retry",
            tool_parameters={"shell.exec": {"command": "echo stale"}},
            selected_tool="shell.exec",
            next_tool_hint="existing hint",
            metadata={
                "planner_plan": {"selected_tools": ["shell.exec"]},
                "plan_context": {"target": "example"},
                "planner_context_snapshot": {"foo": "bar"},
                "tool_plan_prepared": True,
                "tool_dispatch_cache": {"tc_old": {"status": "success"}},
                "tool_call_id": "tc_old",
                "tool_batch_id": "tb_old",
                "tool_approval_gate_completed": True,
                "tool_approval_response": {"action": "approve"},
            },
        )
    )
    metadata = state.facts.metadata_copy()

    updated = _apply_checkpoint_retry_tool_replanning_context(
        state,
        config={
            "configurable": {
                "retry_attempt": 2,
                "retry_max_attempts": 5,
                "previous_failure": {
                    "error_code": "TOOL_TIMEOUT",
                    "failure_stage": "tool_execution",
                    "tool_name": "shell.exec",
                    "tool_call_id": "tc_old",
                    "summary": "timed out",
                },
            }
        },
        metadata=metadata,
        deps={
            "_TOOL_DISPATCH_CACHE_KEY": "tool_dispatch_cache",
            "_TOOL_CALL_ID_KEY": "tool_call_id",
            "_APPROVAL_GATE_COMPLETED_KEY": "tool_approval_gate_completed",
            "_APPROVAL_GATE_RESPONSE_KEY": "tool_approval_response",
            "safe_inc": lambda _metric: None,
            "logger": None,
        },
    )

    assert "planner_plan" not in updated
    assert "planner_context_snapshot" not in updated
    assert "plan_context" not in updated
    assert "tool_plan_prepared" not in updated
    assert "tool_dispatch_cache" not in updated
    assert "tool_call_id" not in updated
    assert "tool_batch_id" not in updated
    assert "tool_approval_gate_completed" not in updated
    assert "tool_approval_response" not in updated

    assert state.facts.tool_parameters.get("shell.exec") is None
    assert state.facts.selected_tool is None
    retry_payload = updated.get("checkpoint_retry_context")
    assert isinstance(retry_payload, dict)
    assert retry_payload.get("retry_attempt") == 2
    assert "Checkpoint retry:" in str(updated.get("next_tool_hint"))
