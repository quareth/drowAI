"""Post-processing node for simple chat responses."""

from __future__ import annotations

from typing import Mapping, Optional, Any

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState


def post_process_simple_chat(
    state: Mapping[str, Any] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """Placeholder safety/formatting pass for simple chat responses.

    Currently trims trailing whitespace and records a reasoning note so future
    enhancements (safety filters, formatting guards) have a dedicated hook.
    """

    interactive = InteractiveState.from_mapping(state)
    final_text = interactive.trace.final_text
    if final_text is not None:
        interactive.trace.final_text = final_text.strip()

    note = "Simple chat post-processing applied."
    if context and context.feature_flags:
        enabled = ", ".join(sorted(flag for flag, value in context.feature_flags.items() if value))
        if enabled:
            note += f" Active flags: {enabled}."
    interactive.trace.reasoning.append(note)

    return interactive.as_graph_update()


__all__ = ["post_process_simple_chat"]
