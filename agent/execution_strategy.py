"""Leaf module owning the ExecutionStrategy enum.

Extracted out of ``agent/models.py`` so both that module (which holds
``ActionPlan``) and ``agent/tool_runtime/batch/types.py`` (which holds
``ToolBatch``) can depend on the enum without forming an import cycle.

``ExecutionStrategy`` is data-only — no behavior — so it has no other
dependencies and is safe to live at the bottom of the import graph.

See ``docs/plans/tool-batch-execution-implementation-guide.md`` Task 1.1.5
for the full rationale.
"""
from __future__ import annotations

from enum import Enum


class ExecutionStrategy(Enum):
    """How committed tool calls in a batch are scheduled."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
