"""Scratchpad synchronization helpers for deterministic working-memory rendering.

The scratchpad is a rendered projection of canonical working memory, not an
append-only free-text log. This module centralizes refresh behavior so nodes
do not mutate `trace.scratchpad` ad hoc.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..state import InteractiveState
from .render import render_working_memory


def render_scratchpad_from_metadata(metadata: Mapping[str, Any] | None) -> str:
    """Render scratchpad text from `facts.metadata.working_memory`."""
    payload = metadata.get("working_memory") if isinstance(metadata, Mapping) else None
    if isinstance(payload, Mapping):
        return render_working_memory(payload)
    return render_working_memory(None)


def refresh_trace_scratchpad(interactive: InteractiveState) -> None:
    """Refresh `trace.scratchpad` from canonical metadata working memory."""
    metadata = interactive.facts.safe_metadata
    interactive.trace.scratchpad = render_scratchpad_from_metadata(metadata)


__all__ = ["render_scratchpad_from_metadata", "refresh_trace_scratchpad"]
