"""Lazy public exports for the LangGraph runtime package.

This package previously imported the full graph builder stack at import time,
which pulled in database-backed backend services even for light consumers such
as prompt builders and memory helpers. The package surface remains the same,
but exports are now resolved lazily to keep package import side effects small.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "InteractiveInput",
    "InteractiveState",
    "FactsState",
    "TraceState",
    "BudgetState",
    "ExtendedFactsState",
    "ExtendedTraceState",
    "PersonaState",
    "GraphRuntimeContext",
    "build_minimal_interactive_graph",
    "build_simple_chat_graph",
    "build_initial_state",
    "get_compiled_minimal_graph",
    "get_compiled_simple_chat_graph",
    "get_default_checkpointer",
    "GraphRegistry",
    "get_default_graph_registry",
    "StreamEventType",
]

_EXPORT_MAP: dict[str, tuple[str, str]] = {
    "InteractiveInput": ("agent.graph.state", "InteractiveInput"),
    "InteractiveState": ("agent.graph.state", "InteractiveState"),
    "FactsState": ("agent.graph.state", "FactsState"),
    "TraceState": ("agent.graph.state", "TraceState"),
    "BudgetState": ("agent.graph.state", "BudgetState"),
    "build_initial_state": ("agent.graph.graph_builder", "build_initial_state"),
    "build_minimal_interactive_graph": (
        "agent.graph.graph_builder",
        "build_minimal_interactive_graph",
    ),
    "build_simple_chat_graph": ("agent.graph.graph_builder", "build_simple_chat_graph"),
    "get_compiled_minimal_graph": (
        "agent.graph.graph_builder",
        "get_compiled_minimal_graph",
    ),
    "get_compiled_simple_chat_graph": (
        "agent.graph.graph_builder",
        "get_compiled_simple_chat_graph",
    ),
    "GraphRegistry": ("agent.graph.infrastructure", "GraphRegistry"),
    "StreamEventType": ("agent.graph.infrastructure", "StreamEventType"),
    "get_default_graph_registry": (
        "agent.graph.infrastructure",
        "get_default_graph_registry",
    ),
    "ExtendedFactsState": (
        "agent.graph.infrastructure.state_models",
        "ExtendedFactsState",
    ),
    "ExtendedTraceState": (
        "agent.graph.infrastructure.state_models",
        "ExtendedTraceState",
    ),
    "GraphRuntimeContext": (
        "agent.graph.infrastructure.state_models",
        "GraphRuntimeContext",
    ),
    "PersonaState": ("agent.graph.infrastructure.state_models", "PersonaState"),
    "get_default_checkpointer": ("agent.graph.persistence", "get_default_checkpointer"),
}


def __getattr__(name: str) -> Any:
    """Resolve public package exports lazily."""
    target = _EXPORT_MAP.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return stable package exports for introspection."""
    return sorted(set(globals()) | set(__all__))
