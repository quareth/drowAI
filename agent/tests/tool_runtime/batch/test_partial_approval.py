"""Phase 7 Task 7.2 unit tests for partial-approval downgrade.

Locks the post-approval contract:

- ``BatchValidator.validate_after_approval`` returns ``denied_aggregate``
  when every original call was denied.
- A surviving subset under partial approval forces sequential execution
  with ``downgrade_reason="partial_approval"``.
- A fully-approved subset preserves the requested strategy (no spurious
  downgrade).
- ``extract_approved_call_ids`` reads the multi-call approval response
  shape (``decisions`` mapping/list) and falls back to all-approve for
  legacy single-tool responses.
"""

from __future__ import annotations

from agent.execution_strategy import ExecutionStrategy
from agent.graph.subgraphs.tool_execution_runtime.approval_and_idempotency import (
    extract_approved_call_ids,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tool_runtime.batch.validator import BatchValidator


def _batch(
    tool_ids,
    *,
    strategy=ExecutionStrategy.PARALLEL,
    tool_call_ids=None,
):
    if tool_call_ids is None:
        tool_call_ids = [f"tc_{i}" for i in range(len(tool_ids))]
    return ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=tuple(
            ToolCall(
                tool_call_id=call_id,
                tool_id=tid,
                parameters={},
            )
            for call_id, tid in zip(tool_call_ids, tool_ids)
        ),
        requested_execution_strategy=strategy,
    )


def test_partial_approval_downgrades_parallel_to_sequential():
    batch = _batch(
        ["tool.a", "tool.b", "tool.c"],
        strategy=ExecutionStrategy.PARALLEL,
        tool_call_ids=["tc_a", "tc_b", "tc_c"],
    )
    result = BatchValidator().validate_after_approval(
        batch, approved_call_ids=["tc_a", "tc_c"]
    )
    assert result.admitted is True
    assert [c.tool_call_id for c in result.batch.tool_calls] == ["tc_a", "tc_c"]
    assert result.requested_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.effective_execution_strategy is ExecutionStrategy.SEQUENTIAL
    assert result.strategy_downgraded is True
    assert result.downgrade_reason == "partial_approval"


def test_full_denial_rejects_with_denied_aggregate():
    batch = _batch(["tool.a", "tool.b"], strategy=ExecutionStrategy.PARALLEL)
    result = BatchValidator().validate_after_approval(batch, approved_call_ids=[])
    assert result.admitted is False
    assert result.rejected_reason == "denied_aggregate"


def test_full_approval_preserves_requested_strategy():
    batch = _batch(
        ["tool.a", "tool.b"],
        strategy=ExecutionStrategy.PARALLEL,
        tool_call_ids=["tc_0", "tc_1"],
    )
    result = BatchValidator().validate_after_approval(
        batch, approved_call_ids=["tc_0", "tc_1"]
    )
    assert result.admitted is True
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL
    assert result.strategy_downgraded is False
    assert result.downgrade_reason is None


def test_partial_approval_keeps_sequential_when_originally_sequential():
    batch = _batch(
        ["tool.a", "tool.b", "tool.c"],
        strategy=ExecutionStrategy.SEQUENTIAL,
        tool_call_ids=["tc_a", "tc_b", "tc_c"],
    )
    result = BatchValidator().validate_after_approval(
        batch, approved_call_ids=["tc_a"]
    )
    assert result.admitted is True
    # Sequential stays sequential — no downgrade flag because there's no
    # parallel→sequential change.
    assert result.effective_execution_strategy is ExecutionStrategy.SEQUENTIAL
    assert result.strategy_downgraded is False
    assert result.downgrade_reason is None


def test_validate_after_approval_preserves_manifest_order():
    batch = _batch(
        ["tool.a", "tool.b", "tool.c"],
        tool_call_ids=["tc_a", "tc_b", "tc_c"],
    )
    # Approval ids passed out of order — survivor batch must keep manifest order.
    result = BatchValidator().validate_after_approval(
        batch, approved_call_ids=["tc_c", "tc_a"]
    )
    assert [c.tool_call_id for c in result.batch.tool_calls] == ["tc_a", "tc_c"]


def test_extract_approved_call_ids_top_level_approve_keeps_all():
    survivors = extract_approved_call_ids(
        {"action": "approve"},
        all_call_ids=["tc_0", "tc_1"],
    )
    assert survivors == ["tc_0", "tc_1"]


def test_extract_approved_call_ids_top_level_skip_returns_empty():
    survivors = extract_approved_call_ids(
        {"action": "skip"},
        all_call_ids=["tc_0", "tc_1"],
    )
    assert survivors == []


def test_extract_approved_call_ids_per_item_decisions_mapping():
    survivors = extract_approved_call_ids(
        {
            "action": "approve",
            "decisions": {
                "tc_0": {"action": "approve"},
                "tc_1": {"action": "skip"},
                "tc_2": {"action": "edit"},
            },
        },
        all_call_ids=["tc_0", "tc_1", "tc_2"],
    )
    assert survivors == ["tc_0", "tc_2"]


def test_extract_approved_call_ids_per_item_decisions_list():
    survivors = extract_approved_call_ids(
        {
            "action": "approve",
            "decisions": [
                {"tool_call_id": "tc_0", "action": "approve"},
                {"tool_call_id": "tc_1", "action": "skip"},
            ],
        },
        all_call_ids=["tc_0", "tc_1"],
    )
    assert survivors == ["tc_0"]


def test_extract_approved_call_ids_missing_response_keeps_all():
    survivors = extract_approved_call_ids(None, all_call_ids=["tc_0", "tc_1"])
    assert survivors == ["tc_0", "tc_1"]
