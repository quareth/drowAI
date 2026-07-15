"""Graph builder scaffolding for LangGraph integration.

This package intentionally does NOT eagerly import its builder modules.

Each builder pulls in heavy dependencies (the full subgraph stack, the
deep-reasoning policy chain, etc.). When ``builders/__init__.py`` eagerly
imported them, ANY ``from agent.graph.builders.X import Y`` (including
deep utility imports like ``..builders.common_edges``) triggered a load of
``simple_tool_builder.py``, which back-imported ``approval_gate_node``
from ``agent/graph/subgraphs/tool_execution.py``. If that module was
itself partially loaded (test_state_contract_enforcement.py is one
trigger), Python raised a ``cannot import name`` ImportError because the
target name was not yet bound.

Consumers that need the public builder factories must import them via the
explicit module path, e.g.::

    from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
    from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph

This keeps the import graph acyclic without any lazy/import-tricks at the
call sites. ``build_simple_chat_graph`` lives in ``graph_builder.py``
(parent module) for the same reason.
"""
