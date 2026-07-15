"""Phase 1 tests for ``agent.tool_runtime.batch.types`` and ``ExecutionStrategy``.

Covers the Phase 1 Tests bucket from
``docs/plans/tool-batch-execution-implementation-guide.md``:

- ``test_tool_batch_immutable``: ``ToolBatch`` cannot be mutated.
- ``test_execution_strategy_values``: enum has exactly ``SEQUENTIAL`` and
  ``PARALLEL``; no ``CONCURRENT`` value or attribute remains.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.types import ToolBatch, ToolCall


def test_execution_strategy_values():
    """Enum carries exactly the two post-rename members."""
    members = {member.name for member in ExecutionStrategy}
    assert members == {"SEQUENTIAL", "PARALLEL"}
    assert ExecutionStrategy.SEQUENTIAL.value == "sequential"
    assert ExecutionStrategy.PARALLEL.value == "parallel"
    assert not hasattr(ExecutionStrategy, "CONCURRENT")


def test_tool_batch_immutable():
    """``ToolBatch`` is a frozen dataclass — fields cannot be reassigned."""
    batch = ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=(
            ToolCall(
                tool_call_id="tc_1",
                tool_id="net.nmap",
                parameters={"target": "127.0.0.1"},
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )

    with pytest.raises(FrozenInstanceError):
        batch.tool_batch_id = "tb_other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        batch.requested_execution_strategy = ExecutionStrategy.PARALLEL  # type: ignore[misc]
