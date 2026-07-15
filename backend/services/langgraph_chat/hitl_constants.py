"""Shared constants for HITL interrupt flow and graph execution."""

from agent.graph.graph_names import (
    DEFAULT_GRAPH_NAME,
    GRAPH_NAME_DEEP_REASONING,
    GRAPH_NAME_INTERRUPT_RESUME,
    GRAPH_NAME_NORMAL_CHAT,
    GRAPH_NAME_SIMPLE_TOOL,
)

# Maximum number of cumulative node transitions allowed across the entire
# thread lifetime (initial turn + all resume/retry continuations).
# LangGraph's step counter persists in the checkpoint and is NOT reset on
# resume, so this budget is shared across every invocation on the same thread.
GRAPH_RECURSION_LIMIT = 100

__all__ = [
    "DEFAULT_GRAPH_NAME",
    "GRAPH_NAME_DEEP_REASONING",
    "GRAPH_NAME_INTERRUPT_RESUME",
    "GRAPH_NAME_NORMAL_CHAT",
    "GRAPH_NAME_SIMPLE_TOOL",
    "GRAPH_RECURSION_LIMIT",
]
