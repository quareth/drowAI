"""Reusable reasoning section lifecycle helper for graph node orchestration.

This module provides an async context manager that emits reasoning lifecycle
events (start, optional delta, end) around awaited internal reasoning work
using the existing unified emitter factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Optional

from agent.graph.emission.factory import EventEmitterFactory
from agent.graph.emission.unified_emitter import UnifiedEventEmitter
from agent.graph.state import InteractiveState

if TYPE_CHECKING:
    from agent.graph.infrastructure.state_models import GraphRuntimeContext


@asynccontextmanager
async def reasoning_section(
    writer: Any,
    *,
    state: Optional[Any],
    step: str,
    label: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    context: Optional["GraphRuntimeContext"] = None,
) -> AsyncIterator[Optional[UnifiedEventEmitter]]:
    """Emit a bounded reasoning section around awaited internal reasoning work.

    The helper is intentionally no-op when interactive streaming context is not
    available (for example, missing writer or missing state).
    """
    if writer is None or state is None:
        yield None
        return

    interactive_state: Optional[InteractiveState]
    if isinstance(state, InteractiveState):
        interactive_state = state
    else:
        try:
            interactive_state = InteractiveState.from_mapping(state)
        except Exception:
            yield None
            return

    emitter = EventEmitterFactory.create(writer, interactive_state, config, context)
    emitter.emit_reasoning_start(step)
    if label:
        emitter.emit_reasoning_delta(label)
    try:
        yield emitter
    finally:
        emitter.emit_reasoning_section_end(step)


__all__ = ["reasoning_section"]
