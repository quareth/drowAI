"""Dataclasses for the tool-batch contract.

Owned by the batch package. The runtime, validator, executor, aggregator,
and emitter all share these shapes. No business logic lives here — only
data definitions. Keeping types in a leaf module makes the rest of the
package safe to import from anywhere in ``agent.tool_runtime`` without
risking cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from agent.execution_strategy import ExecutionStrategy  # leaf module — see Task 1.1.5


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A single committed tool invocation inside a batch."""

    tool_call_id: str
    tool_id: str
    parameters: Mapping[str, Any]
    intent: str = ""


@dataclass(frozen=True, slots=True)
class ToolBatch:
    """An ordered, immutable bundle of committed tool calls.

    ``requested_execution_strategy`` is the strategy the selector asked for;
    the validator may downgrade to ``SEQUENTIAL`` based on runtime
    metadata or concurrency caps. The post-validation ``effective`` strategy
    is recorded in ``BatchValidationResult`` (Phase 4).
    """

    tool_batch_id: str
    tool_calls: Sequence[ToolCall]
    requested_execution_strategy: ExecutionStrategy
    deferred_followups: Sequence[str] = field(default_factory=tuple)
    selection_rationale: str = ""


class ToolCallStatus(str, Enum):
    """Terminal status of a single tool call within a batch."""

    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class BatchStatus(str, Enum):
    """Aggregated terminal status of a batch."""

    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Outcome of a single tool call inside a batch.

    ``raw_result`` is the underlying tool output (schema is tool-specific
    and intentionally untyped here — sanitization happens in the
    aggregator before it enters compact metadata). ``failure_category`` is
    populated only when ``status`` is not ``SUCCESS``.
    """

    tool_call_id: str
    tool_id: str
    status: ToolCallStatus
    duration_ms: int = 0
    raw_result: Optional[Mapping[str, Any]] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Roll-up of per-call results plus the aggregated batch status.

    ``effective_execution_strategy`` is the strategy actually used at
    runtime (after any validator downgrade). ``downgrade_reason`` is set
    iff ``effective`` differs from the batch's ``requested`` strategy.
    """

    tool_batch_id: str
    status: BatchStatus
    call_results: Sequence[ToolCallResult]
    effective_execution_strategy: ExecutionStrategy
    requested_execution_strategy: ExecutionStrategy
    downgrade_reason: Optional[str] = None
    duration_ms: int = 0
