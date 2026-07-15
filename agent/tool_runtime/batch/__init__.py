"""Tool-batch runtime package.

Houses the dataclasses, id minting, compatibility checks, validation,
execution scheduling, result aggregation, and stream-event payload
builders for the batched tool-call contract described in
``docs/architecture/tool-batch-execution.md``.

Each submodule has a single responsibility:

- ``types``: dataclasses shared across the package.
- ``ids``: canonical mint sites for ``tool_batch_id`` and ``tool_call_id``.
- ``compatibility``: parallel/avoid-with/audit checks across calls.
- ``validator``: structural + budget validation prior to execution.
- ``executor``: callback-driven scheduler (sequential or gated parallel).
- ``aggregator``: per-call results → batch-level rollup + compact metadata.
- ``emitter``: stream-event payload builders for batch start/end events.

Files in this package start as scaffolds — bodies are filled in across
Phases 3–6 of the implementation guide. Keeping them empty (rather than
inlining logic into existing monoliths) makes the eventual wiring cheap.
"""

from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)

__all__ = [
    "BatchResult",
    "BatchStatus",
    "ToolBatch",
    "ToolCall",
    "ToolCallResult",
    "ToolCallStatus",
]
