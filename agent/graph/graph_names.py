"""Canonical graph-name constants shared by agent builders and backend runtime.

The values in this module are runtime identities used in checkpoint configs,
interrupt metadata, resume flows, registry keys, and diagnostics. Keeping them
in the agent package avoids backend-only constants drifting from graph-builder
identities.
"""

from __future__ import annotations

GRAPH_NAME_SIMPLE_TOOL = "simple_tool"
GRAPH_NAME_DEEP_REASONING = "deep_reasoning"
GRAPH_NAME_NORMAL_CHAT = "normal_chat"
GRAPH_NAME_INTERRUPT_RESUME = "interrupt_resume"

DEFAULT_GRAPH_NAME = GRAPH_NAME_SIMPLE_TOOL

__all__ = [
    "DEFAULT_GRAPH_NAME",
    "GRAPH_NAME_DEEP_REASONING",
    "GRAPH_NAME_INTERRUPT_RESUME",
    "GRAPH_NAME_NORMAL_CHAT",
    "GRAPH_NAME_SIMPLE_TOOL",
]
