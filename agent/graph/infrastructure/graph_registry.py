"""Shared registry and cache for compiled LangGraph graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from typing import Any

try:
    from langgraph.graph import StateGraph
    # In LangGraph 0.4+, CompiledGraph is replaced by the result of StateGraph.compile()
    # We use Any as the type since the exact compiled type varies
    CompiledGraph = Any
except Exception:  # pragma: no cover - optional dependency during scaffolding
    CompiledGraph = object  # type: ignore[assignment]


GraphFactory = Callable[[], "CompiledGraph"]


@dataclass
class GraphRegistry:
    """Stores compiled graphs keyed by name with lazy construction."""

    _compiled: Dict[str, "CompiledGraph"] = field(default_factory=dict)
    _factories: Dict[str, GraphFactory] = field(default_factory=dict)

    def register(self, name: str, factory: GraphFactory) -> None:
        """Register a factory for a compiled graph."""

        self._factories[name] = factory

    def get(self, name: str) -> Optional["CompiledGraph"]:
        """Return the compiled graph if present."""

        compiled = self._compiled.get(name)
        if compiled is not None:
            return compiled
        factory = self._factories.get(name)
        if factory is None:
            return None
        graph = factory()
        self._compiled[name] = graph
        return graph


_DEFAULT_REGISTRY: Optional[GraphRegistry] = None


def get_default_graph_registry() -> GraphRegistry:
    """Return the process-wide graph registry instance."""

    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = GraphRegistry()
    return _DEFAULT_REGISTRY


def get_or_register_compiled_graph(
    *,
    registry: GraphRegistry,
    name: str,
    build_uncompiled: Callable[[], "StateGraph"],
    checkpointer_factory: Callable[[], Any],
    on_compiled: Optional[Callable[["StateGraph", Any], None]] = None,
) -> "CompiledGraph":
    """Return a lazily compiled graph from the registry.

    The helper accepts an *uncompiled* ``StateGraph`` factory and owns the
    single ``compile`` call. This avoids the latent double-compile bug where
    builder getters previously called ``build_*_graph()`` (which already
    compiles by default) and then re-invoked ``.compile(...)`` inside the
    registry factory.

    The helper is intentionally minimal: it does not introduce a third
    caching layer beyond ``GraphRegistry``, and it is unaware of which
    builder it is wrapping.

    Parameters
    ----------
    on_compiled:
        Optional callback invoked once at registry-compile time with the
        uncompiled ``StateGraph`` and the resolved checkpointer instance,
        before ``graph.compile`` runs. Builders can use this to emit
        diagnostic logging (graph name, checkpointer type, node count)
        without forcing the registry module to depend on the backend
        diagnostic logger. The callback is best-effort: it must not raise
        on the production path; if it does, the exception is suppressed
        so registry compile still succeeds.
    """

    existing = registry.get(name)
    if existing is not None:
        return existing

    def _factory() -> "CompiledGraph":
        graph = build_uncompiled()
        actual_checkpointer = checkpointer_factory()
        if on_compiled is not None:
            try:
                on_compiled(graph, actual_checkpointer)
            except Exception:
                # Diagnostic callback must never block compile.
                pass
        return graph.compile(checkpointer=actual_checkpointer)

    registry.register(name, _factory)
    compiled = registry.get(name)
    if compiled is None:
        raise RuntimeError(f"Graph registry did not return compiled graph: {name}")
    return compiled


__all__ = [
    "GraphRegistry",
    "GraphFactory",
    "get_default_graph_registry",
    "get_or_register_compiled_graph",
]
