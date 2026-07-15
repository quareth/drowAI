"""LangGraph DRY Tier 2 baseline guardrail tests.

Purpose
-------
Lock down Tier 2 invariants from
``docs/refactor/langgraph-dry-tier-2-implementation-guide.md`` *before*
production code is edited. This file accumulates tests across Phase 0
(Tasks 0.1, 0.2, 0.3) and is consumed by every later phase's verification
step.

Task 0.3 layers static-inventory guards on top of the accessor and
wrapper-factory contracts. Each guard runs a self-contained ``pathlib``
scan over the active production tree (skipping ``tests``, ``_archive``,
and ``__pycache__`` directories), collects ``(relative_path, line_number,
line_text)`` tuples for the patterns the migration must remove, and
asserts the inventory is empty. Each guard is marked
``pytest.mark.xfail(strict=True, ...)`` so the same assertion holds
across the migration: while the pattern still exists, the test xfails;
once the owning phase finishes the cleanup, ``strict=True`` flips an
unexpected pass into a failure, forcing removal of the marker. The guard
itself never needs to be rewritten.

Task 0.1 covers the ``FactsState`` accessor contract that Phase 1 will
add to ``agent/graph/state.py``:

- ``safe_metadata`` returns a read-only view of ``metadata``.
- ``metadata_copy()`` returns a detached mutable copy.
- ``ensure_metadata()`` returns a persisted mutable dict and normalizes
  ``None`` (runtime drift) back into a state-owned empty dict.
- ``safe_decision_history`` returns the current list for read-only access.
- ``ensure_decision_history()`` returns a persisted list that supports
  in-place append.
- ``safe_todo_list`` returns ``[]`` when the todo list is absent, and the
  current list otherwise.

Task 0.2 covers the wrapper factory contract that Phase 3 will land in
``agent/graph/builders/common_edges.py``:

- ``wrap_with_context`` (sync wrapper factory).
- ``wrap_with_context_async`` (async wrapper factory).

These factories generalize the deep-reasoning local
``_wrap_with_context`` / ``_wrap_with_context_async`` helpers and
forward ``config``/``writer`` only when the wrapped node signature
accepts them. An optional diagnostic callback runs independently of
whether the wrapped node accepts those kwargs. The wrapper-factory
tests deliberately exercise this contract before the helpers exist,
so Phase 3 cannot accidentally drop signature introspection or change
the diagnostic-callback shape without breaking a strict xfail.

Phase-aware activation
----------------------
The helpers themselves land in later phases (Phase 1 for FactsState
accessors, Phase 3 for wrapper factories), so these assertions
intentionally fail under the current contract. Each test is marked
``pytest.mark.xfail(strict=True, ...)`` per the phase-aware-tests
ownership rule in ``.claude/agents/implementation-state.md``: tests are
written now to lock in the future shape, but Phase 0/Phase 1
verification is not blocked by helpers that have not landed yet. When
the owning phase ships, the matching xfail markers must be removed
(``strict=True`` turns an unexpected pass into a failure, forcing the
cleanup).

The tests intentionally do not require network, Docker, a database, or
LLM calls. They construct ``FactsState`` directly via Pydantic and
exercise the wrapper factories with synthetic node callables and
synthetic ``graph_runtime_context`` payloads.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import warnings
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Tuple, get_type_hints

from langgraph.config import RunnableConfig
from langgraph.graph import END, StateGraph

from agent.graph.state import FactsState, TodoItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_facts(**overrides) -> FactsState:
    """Build a minimal ``FactsState`` for accessor contract tests."""

    base = {
        "task_id": 1,
        "message": "tier2 accessor contract",
    }
    base.update(overrides)
    return FactsState(**base)


# ---------------------------------------------------------------------------
# Task 0.1: FactsState accessor contract tests (Phase 1 target)
#
# These tests assert the future accessor surface defined by the Tier 2
# implementation guide. They are expected to fail until Phase 1 lands the
# accessors on ``FactsState`` in ``agent/graph/state.py``.
# ---------------------------------------------------------------------------


_PHASE1_REASON = "enabled in Phase 1.x: FactsState safe/ensure accessors"


def test_safe_metadata_returns_current_dict_for_read_only_access():
    """``safe_metadata`` exposes the current metadata dict for reads."""

    facts = _make_facts(metadata={"already_here": True})

    metadata = facts.safe_metadata

    assert metadata == {"already_here": True}
    # Read-only by convention: callers must not get a transient fallback
    # when a real (possibly empty) metadata object already exists. We
    # check identity to prove the state-owned object is returned, not a
    # freshly constructed one.
    assert metadata is facts.metadata


def test_safe_metadata_returns_empty_dict_when_metadata_is_none():
    """``safe_metadata`` tolerates runtime drift where metadata is ``None``."""

    facts = _make_facts()
    # Simulate runtime drift; pydantic without validate_assignment lets
    # raw assignment bypass the declared non-Optional contract.
    facts.metadata = None  # type: ignore[assignment]

    metadata = facts.safe_metadata

    assert metadata == {}
    # Read-only fallback must NOT persist a new dict on the facts object.
    # That is the job of ensure_metadata().
    assert facts.metadata is None


def test_safe_metadata_preserves_existing_empty_dict_identity():
    """``safe_metadata`` uses ``is None`` checks, not truthiness."""

    facts = _make_facts(metadata={})
    original = facts.metadata

    metadata = facts.safe_metadata

    # An existing empty dict must remain the state-owned object, not be
    # replaced by a transient ``{}`` fallback.
    assert metadata is original


def test_metadata_copy_returns_detached_mutable_dict():
    """``metadata_copy()`` returns a detached copy that does not mutate state."""

    facts = _make_facts(metadata={"k": "v"})

    detached = facts.metadata_copy()

    assert detached == {"k": "v"}
    assert detached is not facts.metadata

    detached["new_key"] = "value"

    # Mutating the copy must not leak back into the state-owned dict.
    assert "new_key" not in facts.metadata


def test_metadata_copy_returns_empty_copy_when_metadata_is_none():
    """``metadata_copy()`` tolerates ``None`` metadata without persisting."""

    facts = _make_facts()
    facts.metadata = None  # type: ignore[assignment]

    detached = facts.metadata_copy()

    assert detached == {}
    # Detached copy must not persist a new dict on the facts object.
    assert facts.metadata is None


def test_ensure_metadata_persists_mutation_on_facts():
    """``ensure_metadata()`` returns the state-owned dict for persisted writes."""

    facts = _make_facts(metadata={"existing": 1})

    metadata = facts.ensure_metadata()
    metadata["new_key"] = "value"

    # Mutation must persist on the facts object.
    assert facts.metadata is metadata
    assert facts.metadata == {"existing": 1, "new_key": "value"}


def test_ensure_metadata_normalizes_none_into_persisted_empty_dict():
    """``ensure_metadata()`` replaces ``None`` with a persisted empty dict."""

    facts = _make_facts()
    facts.metadata = None  # type: ignore[assignment]

    metadata = facts.ensure_metadata()

    assert metadata == {}
    # Unlike safe_metadata, ensure_metadata must persist the new dict so
    # subsequent mutations are visible on the facts object.
    assert facts.metadata is metadata

    metadata["k"] = "v"
    assert facts.metadata == {"k": "v"}


def test_safe_decision_history_returns_current_list_for_read_only_access():
    """``safe_decision_history`` exposes the current list for reads."""

    facts = _make_facts(decision_history=["alpha", "beta"])

    history = facts.safe_decision_history

    assert history == ["alpha", "beta"]
    assert history is facts.decision_history


def test_safe_decision_history_returns_empty_list_when_none():
    """``safe_decision_history`` tolerates ``None`` without persisting."""

    facts = _make_facts()
    facts.decision_history = None  # type: ignore[assignment]

    history = facts.safe_decision_history

    assert history == []
    # Read-only fallback must not persist a new list on the facts object.
    assert facts.decision_history is None


def test_safe_decision_history_preserves_existing_empty_list_identity():
    """``safe_decision_history`` uses ``is None`` checks, not truthiness."""

    facts = _make_facts(decision_history=[])
    original = facts.decision_history

    history = facts.safe_decision_history

    # An existing empty list must remain the state-owned list.
    assert history is original


def test_ensure_decision_history_supports_persisted_append():
    """``ensure_decision_history()`` returns a list that persists appends."""

    facts = _make_facts(decision_history=["alpha"])

    history = facts.ensure_decision_history()
    history.append("beta")

    # Append must persist on the facts object.
    assert facts.decision_history is history
    assert facts.decision_history == ["alpha", "beta"]


def test_ensure_decision_history_normalizes_none_into_persisted_empty_list():
    """``ensure_decision_history()`` replaces ``None`` with a persisted list."""

    facts = _make_facts()
    facts.decision_history = None  # type: ignore[assignment]

    history = facts.ensure_decision_history()

    assert history == []
    assert facts.decision_history is history

    history.append("first")
    assert facts.decision_history == ["first"]


def test_safe_todo_list_returns_empty_list_when_absent():
    """``safe_todo_list`` returns ``[]`` when the todo list is absent."""

    facts = _make_facts()
    facts.todo_list = None  # type: ignore[assignment]

    todos = facts.safe_todo_list

    assert todos == []
    # Read-only fallback must not persist a new list on the facts object.
    assert facts.todo_list is None


def test_safe_todo_list_returns_empty_list_when_default():
    """``safe_todo_list`` returns the existing empty list by default."""

    facts = _make_facts()
    original = facts.todo_list

    todos = facts.safe_todo_list

    assert todos == []
    # An existing empty list must remain the state-owned list, not be
    # replaced by a transient ``[]`` fallback.
    assert todos is original


def test_safe_todo_list_returns_current_list_when_populated():
    """``safe_todo_list`` exposes legacy strings and rich TodoItems alike."""

    legacy = _make_facts(todo_list=["scan port 80", "enumerate users"])
    assert legacy.safe_todo_list == ["scan port 80", "enumerate users"]
    assert legacy.safe_todo_list is legacy.todo_list

    rich_items = [TodoItem.from_string("scan port 80")]
    rich = _make_facts(todo_list=rich_items)
    assert rich.safe_todo_list is rich.todo_list
    assert len(rich.safe_todo_list) == 1


# ---------------------------------------------------------------------------
# Task 0.2: Wrapper factory tests (Phase 3 target)
#
# Phase 3 will move the deep-reasoning ``_wrap_with_context`` /
# ``_wrap_with_context_async`` helpers into
# ``agent/graph/builders/common_edges.py`` as ``wrap_with_context`` and
# ``wrap_with_context_async``. These tests lock in the public contract
# before the helpers exist:
#
# - The sync wrapper passes a ``context`` kwarg derived from the
#   state's ``facts.metadata.graph_runtime_context`` payload.
# - The async wrapper awaits coroutine nodes and calls sync nodes
#   directly, so a single async wrapper works for mixed node surfaces.
# - ``config`` and ``writer`` are forwarded only when the wrapped
#   node's signature actually accepts them. Inspection happens once
#   at wrap creation.
# - An optional diagnostic callback receives
#   ``(node_name, writer_available, config_available)`` even when the
#   wrapped node accepts neither kwarg, so observability is decoupled
#   from node signature.
#
# The wrappers do not yet exist in ``common_edges.py``, so each test is
# marked ``pytest.mark.xfail(strict=True, ...)``. Phase 3.1 must remove
# these markers; ``strict=True`` flips an unexpected pass into a failure
# to force the cleanup.
# ---------------------------------------------------------------------------


def _runtime_context_state(**context_overrides: Any) -> dict:
    """Return a synthetic state mapping shaped like ``extract_runtime_context``.

    ``agent/graph/builders/common_edges.extract_runtime_context`` reads
    ``state["facts"]["metadata"]["graph_runtime_context"]`` and validates
    it via ``GraphRuntimeContext.model_validate(...)``. The returned
    payload here matches that shape so the wrapper can hydrate a real
    ``GraphRuntimeContext`` without monkeypatching production code.
    """

    payload = {"task_id": 1}
    payload.update(context_overrides)
    return {
        "facts": {
            "metadata": {"graph_runtime_context": payload},
        }
    }


def _import_wrap_with_context():
    """Import ``wrap_with_context`` lazily so the module loads pre-Phase 3."""

    from agent.graph.builders import common_edges  # noqa: WPS433 (intentional lazy import)

    return common_edges.wrap_with_context  # type: ignore[attr-defined]


def _import_wrap_with_context_async():
    """Import ``wrap_with_context_async`` lazily so the module loads pre-Phase 3."""

    from agent.graph.builders import common_edges  # noqa: WPS433 (intentional lazy import)

    return common_edges.wrap_with_context_async  # type: ignore[attr-defined]


def _import_with_interactive_state():
    """Import ``with_interactive_state`` lazily so the module loads pre-Phase 2."""

    from agent.graph.builders import common_edges  # noqa: WPS433 (intentional lazy import)

    return common_edges.with_interactive_state  # type: ignore[attr-defined]


def test_with_interactive_state_preserves_branch_name_without_signature_forwarding():
    """Route adapter keeps handler names while exposing its own ``(state)`` signature."""

    with_interactive_state = _import_with_interactive_state()
    received: dict = {}

    def _route_probe(interactive) -> str:
        """Route probe docstring."""
        received["task_id"] = interactive.facts.task_id
        return "done"

    wrapped = with_interactive_state(_route_probe)

    assert wrapped.__name__ == "_route_probe"
    assert wrapped.__qualname__ == _route_probe.__qualname__
    assert wrapped.__doc__ == _route_probe.__doc__
    assert not hasattr(wrapped, "__wrapped__")
    assert list(inspect.signature(wrapped).parameters) == ["state"]

    state = {"facts": _make_facts().model_dump()}
    assert wrapped(state) == "done"
    assert received == {"task_id": 1}

    graph = StateGraph(dict)
    graph.add_node("source", lambda current_state: current_state)
    graph.set_entry_point("source")
    graph.add_conditional_edges("source", wrapped, {"done": END})

    branches = getattr(graph, "branches", {}) or {}
    assert "_route_probe" in branches["source"]
    assert "_runner" not in branches["source"]


def test_wrap_with_context_passes_context_to_sync_node():
    """Sync wrapper hydrates ``context`` from runtime metadata."""

    wrap_with_context = _import_wrap_with_context()

    received: dict = {}

    def node(state: Mapping[str, Any], *, context=None) -> dict:
        received["state"] = state
        received["context"] = context
        return {"ok": True}

    wrapped = wrap_with_context(node)
    state = _runtime_context_state(turn_id="turn-1")

    result = wrapped(state)

    assert result == {"ok": True}
    assert received["state"] is state
    # ``extract_runtime_context`` returns a ``GraphRuntimeContext`` with
    # ``task_id`` populated from the metadata payload above.
    assert received["context"] is not None
    assert received["context"].task_id == 1
    assert received["context"].turn_id == "turn-1"


def test_context_wrappers_expose_runnable_config_without_langgraph_warning():
    """Shared wrappers expose the config type expected by LangGraph 1.x."""

    wrap_with_context = _import_wrap_with_context()
    wrap_with_context_async = _import_wrap_with_context_async()

    def sync_node(state: Mapping[str, Any], *, context=None) -> dict:
        return dict(state)

    async def async_node(state: Mapping[str, Any], *, context=None) -> dict:
        return dict(state)

    sync_wrapper = wrap_with_context(sync_node)
    async_wrapper = wrap_with_context_async(async_node)

    assert get_type_hints(sync_wrapper)["config"] == RunnableConfig | None
    assert get_type_hints(async_wrapper)["config"] == RunnableConfig | None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = StateGraph(dict)
        graph.add_node("sync", sync_wrapper)
        graph.add_node("async", async_wrapper)

    config_warnings = [
        warning
        for warning in caught
        if "parameter should be typed as 'RunnableConfig'" in str(warning.message)
    ]
    assert config_warnings == []


def test_wrap_with_context_omits_config_when_node_does_not_accept_it():
    """Sync wrapper must not forward ``config`` to nodes that do not accept it."""

    wrap_with_context = _import_wrap_with_context()

    def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True}

    wrapped = wrap_with_context(node)
    # If the wrapper blindly forwarded ``config``, this call would raise
    # TypeError because ``node`` has no ``config`` parameter.
    result = wrapped(_runtime_context_state(), config={"thread_id": "t-1"})

    assert result == {"ok": True}


def test_wrap_with_context_forwards_config_when_node_accepts_it():
    """Sync wrapper forwards ``config`` when the wrapped node opts in."""

    wrap_with_context = _import_wrap_with_context()

    received: dict = {}

    def node(state: Mapping[str, Any], config=None, *, context=None) -> dict:
        received["config"] = config
        return {"ok": True}

    wrapped = wrap_with_context(node)
    sentinel_config = {"thread_id": "t-1"}
    wrapped(_runtime_context_state(), config=sentinel_config)

    assert received["config"] is sentinel_config


def test_wrap_with_context_omits_writer_when_node_does_not_accept_it():
    """Sync wrapper must not forward ``writer`` to nodes that do not accept it."""

    wrap_with_context = _import_wrap_with_context()

    def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True}

    wrapped = wrap_with_context(node)

    def writer(_: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("writer should not be forwarded to non-accepting node")

    result = wrapped(_runtime_context_state(), writer=writer)

    assert result == {"ok": True}


def test_wrap_with_context_forwards_writer_when_node_accepts_it():
    """Sync wrapper forwards ``writer`` when the wrapped node opts in."""

    wrap_with_context = _import_wrap_with_context()

    received: dict = {}

    def node(state: Mapping[str, Any], *, writer=None, context=None) -> dict:
        received["writer"] = writer
        return {"ok": True}

    wrapped = wrap_with_context(node)
    sentinel_writer = lambda payload: None  # noqa: E731 (test sentinel)
    wrapped(_runtime_context_state(), writer=sentinel_writer)

    assert received["writer"] is sentinel_writer


def test_wrap_with_context_drops_unknown_runtime_kwargs():
    """Sync wrapper drops runtime kwargs the wrapped node did not opt into."""

    wrap_with_context = _import_wrap_with_context()

    def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True, "context_task_id": getattr(context, "task_id", None)}

    wrapped = wrap_with_context(node)

    result = wrapped(_runtime_context_state(), runtime=object(), store=object())

    assert result == {"ok": True, "context_task_id": 1}


def test_wrap_with_context_forwards_explicit_extra_runtime_kwargs():
    """Sync wrapper forwards extra runtime kwargs only when explicitly accepted."""

    wrap_with_context = _import_wrap_with_context()
    sentinel_store = object()
    received: dict = {}

    def node(state: Mapping[str, Any], *, context=None, store=None) -> dict:
        received["store"] = store
        return {"ok": True}

    wrapped = wrap_with_context(node)

    result = wrapped(_runtime_context_state(), store=sentinel_store, runtime=object())

    assert result == {"ok": True}
    assert received["store"] is sentinel_store


def test_wrap_with_context_forwards_extra_kwargs_to_var_kwargs_node():
    """Sync wrapper preserves explicit ``**kwargs`` opt-in for runtime kwargs."""

    wrap_with_context = _import_wrap_with_context()
    sentinel_runtime = object()
    received: dict = {}

    def node(state: Mapping[str, Any], *, context=None, **kwargs: Any) -> dict:
        received.update(kwargs)
        return {"ok": True}

    wrapped = wrap_with_context(node)

    result = wrapped(_runtime_context_state(), runtime=sentinel_runtime)

    assert result == {"ok": True}
    assert received == {"runtime": sentinel_runtime}


def test_wrap_with_context_diagnostic_callback_receives_observability_signals():
    """Diagnostic callback fires with ``(node_name, writer_available, config_available)``.

    The wrapped node accepts neither ``writer`` nor ``config``; the
    callback must still report whether the surrounding LangGraph
    invocation supplied them, decoupling diagnostics from node signature.
    """

    wrap_with_context = _import_wrap_with_context()

    diagnostics: list = []

    def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True}

    def on_wrap_log(node_name, writer_available, config_available):
        diagnostics.append((node_name, writer_available, config_available))

    wrapped = wrap_with_context(node, node_name="classification", on_wrap_log=on_wrap_log)

    wrapped(_runtime_context_state(), config={"thread_id": "t-1"}, writer=lambda _: None)

    assert diagnostics == [("classification", True, True)]


def test_wrap_with_context_async_awaits_coroutine_node():
    """Async wrapper awaits coroutine nodes and forwards ``context``."""

    wrap_with_context_async = _import_wrap_with_context_async()

    received: dict = {}

    async def node(state: Mapping[str, Any], *, context=None) -> dict:
        received["context"] = context
        return {"async_ok": True}

    wrapped = wrap_with_context_async(node)
    state = _runtime_context_state(turn_id="async-turn")

    result = asyncio.run(wrapped(state))

    assert result == {"async_ok": True}
    assert received["context"] is not None
    assert received["context"].turn_id == "async-turn"


def test_wrap_with_context_async_calls_sync_node_directly():
    """Async wrapper handles plain (sync) callables on mixed graph surfaces.

    Deep-reasoning currently registers some sync nodes (e.g.
    ``finalize_turn`` / ``classify_turn``) on the same graph as async
    nodes. The async wrapper must therefore detect non-coroutine
    callables and call them directly without ``await``.
    """

    wrap_with_context_async = _import_wrap_with_context_async()

    def sync_node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"sync_ok": True, "context_task_id": getattr(context, "task_id", None)}

    wrapped = wrap_with_context_async(sync_node)

    result = asyncio.run(wrapped(_runtime_context_state()))

    assert result == {"sync_ok": True, "context_task_id": 1}


def test_wrap_with_context_async_omits_config_when_node_does_not_accept_it():
    """Async wrapper must not forward ``config`` to non-accepting nodes."""

    wrap_with_context_async = _import_wrap_with_context_async()

    async def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True}

    wrapped = wrap_with_context_async(node)
    result = asyncio.run(wrapped(_runtime_context_state(), config={"thread_id": "t-1"}))

    assert result == {"ok": True}


def test_wrap_with_context_async_forwards_writer_when_node_accepts_it():
    """Async wrapper forwards ``writer`` when the wrapped node opts in."""

    wrap_with_context_async = _import_wrap_with_context_async()

    received: dict = {}

    async def node(state: Mapping[str, Any], *, writer=None, context=None) -> dict:
        received["writer"] = writer
        return {"ok": True}

    wrapped = wrap_with_context_async(node)
    sentinel_writer = lambda payload: None  # noqa: E731 (test sentinel)
    asyncio.run(wrapped(_runtime_context_state(), writer=sentinel_writer))

    assert received["writer"] is sentinel_writer


def test_wrap_with_context_async_drops_unknown_runtime_kwargs():
    """Async wrapper drops runtime kwargs the wrapped node did not opt into."""

    wrap_with_context_async = _import_wrap_with_context_async()

    async def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True, "context_task_id": getattr(context, "task_id", None)}

    wrapped = wrap_with_context_async(node)
    result = asyncio.run(
        wrapped(_runtime_context_state(), runtime=object(), store=object())
    )

    assert result == {"ok": True, "context_task_id": 1}


def test_wrap_with_context_async_forwards_explicit_extra_runtime_kwargs():
    """Async wrapper forwards extra runtime kwargs only when explicitly accepted."""

    wrap_with_context_async = _import_wrap_with_context_async()
    sentinel_store = object()
    received: dict = {}

    async def node(state: Mapping[str, Any], *, context=None, store=None) -> dict:
        received["store"] = store
        return {"ok": True}

    wrapped = wrap_with_context_async(node)
    result = asyncio.run(
        wrapped(_runtime_context_state(), store=sentinel_store, runtime=object())
    )

    assert result == {"ok": True}
    assert received["store"] is sentinel_store


def test_wrap_with_context_async_diagnostic_callback_receives_observability_signals():
    """Async diagnostic callback fires independently of node signature."""

    wrap_with_context_async = _import_wrap_with_context_async()

    diagnostics: list = []

    async def node(state: Mapping[str, Any], *, context=None) -> dict:
        return {"ok": True}

    def on_wrap_log(node_name, writer_available, config_available):
        diagnostics.append((node_name, writer_available, config_available))

    wrapped = wrap_with_context_async(
        node,
        node_name="memory_retrieval",
        on_wrap_log=on_wrap_log,
    )

    # writer present, config absent: callback must still fire even though
    # the wrapped node accepts neither kwarg.
    asyncio.run(wrapped(_runtime_context_state(), writer=lambda _: None))

    assert diagnostics == [("memory_retrieval", True, False)]


def test_wrap_with_context_async_preserves_langgraph_writer_injection():
    """LangGraph must inject ``writer`` through the shared async wrapper."""

    wrap_with_context_async = _import_wrap_with_context_async()

    async def node(state: Mapping[str, Any], *, writer=None, context=None) -> dict:
        writer({"type": "probe", "content": "writer-injected"})
        return {
            "writer_seen": writer is not None,
            "context_task_id": getattr(context, "task_id", None),
        }

    graph = StateGraph(dict)
    graph.add_node("probe", wrap_with_context_async(node))
    graph.set_entry_point("probe")
    graph.add_edge("probe", END)
    compiled = graph.compile()

    async def collect_events() -> list:
        events = []
        async for event in compiled.astream(
            _runtime_context_state(),
            stream_mode=["custom", "values"],
        ):
            events.append(event)
        return events

    events = asyncio.run(collect_events())

    assert ("custom", {"type": "probe", "content": "writer-injected"}) in events
    assert events[-1][1]["writer_seen"] is True
    assert events[-1][1]["context_task_id"] == 1


# ---------------------------------------------------------------------------
# Task 0.3: Static inventory guards (phase-aware activation)
#
# These guards are pure ``pathlib`` + ``re`` scans of the active production
# tree. They do not import from the targets, so they work even when the
# migration is partially landed. Each guard is marked
# ``pytest.mark.xfail(strict=True, ...)`` and asserts ``occurrences == []``;
# while the pattern still exists, the test xfails (today's expected
# state). Once the owning phase removes the pattern, the test passes and
# ``strict=True`` flips that unexpected pass into a failure, forcing
# removal of the marker. The scanner itself never needs rewriting.
#
# Patterns tracked, per the implementation guide §Phase 0 → Task 0.3:
#
# - ``facts.metadata or {}`` (Phase 5 cleanup, scope: agent/graph +
#   backend/services/langgraph_chat).
# - ``facts.decision_history or []`` (Phase 5 cleanup, scope: agent/graph).
# - ``facts.todo_list or []`` (Phase 5 cleanup, scope: agent/graph).
# - Local wrapper factory definitions outside ``common_edges.py`` (Phase 3
#   cleanup, scope: agent/graph minus
#   ``agent/graph/builders/common_edges.py``).
# - Direct ``extract_runtime_context(state)`` calls in builder wrapper
#   bodies (Phase 4 cleanup, scope: builder + graph_builder modules).
# - Direct ``InteractiveState.from_mapping(state)`` calls inside the
#   builder layer (Phase 2 cleanup, scope: agent/graph/builders +
#   ``agent/graph/graph_builder.py`` minus ``common_edges.py``, which is
#   the canonical adapter home).
# ---------------------------------------------------------------------------


# Verbatim from the implementation guide:
#   ACTIVE_EXCLUDES = {"tests", "_archive", "__pycache__"}
#
# These directory names are pruned at every depth — not just at the top
# level — so an embedded ``tests`` package or an ``_archive`` legacy
# bucket inside a deeper module never contaminates the inventory.
ACTIVE_EXCLUDES = {"tests", "_archive", "__pycache__"}

# Walk up from this test file to the repository root. The test file lives
# at ``agent/graph/tests/test_langgraph_dry_tier2.py``; ``parents[3]``
# resolves to the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_PHASE2_REASON = (
    "enabled in Phase 2.x: builder route conversion (with_interactive_state)"
)
# Phase 2.3 migrated simple-tool builder route functions to
# ``with_interactive_state(...)``. Phase 4.1 then migrated the simple-chat
# wrappers to ``wrap_with_context`` / ``wrap_with_context_async`` and
# preserved the bootstrap-graph helper ``_ensure_state`` by binding it to
# a ``raw_state`` parameter, so it no longer matches the
# ``InteractiveState.from_mapping(state)`` literal-token guard. The
# strict assertion below is therefore the active enforcement gate.
_PHASE3_GUARD_REASON = (
    "enabled in Phase 3.2: deep-reasoning local wrapper factories deleted"
)
_PHASE4_REASON = (
    "enabled in Phase 4.x: builder wrappers use shared wrap_with_context"
)
# Phase 5.4 tightened the metadata/decision_history/todo_list inventory
# guards into strict-passing assertions. ``_PHASE5_REASON`` is intentionally
# omitted now that no remaining test uses it; removing the xfail markers
# turns each guard into the active enforcement gate, the same closeout
# pattern used in Tasks 4.1 and 4.2.


def _iter_active_python_files(*roots: Path) -> Iterable[Path]:
    """Yield ``*.py`` files under ``roots`` skipping ``ACTIVE_EXCLUDES`` dirs.

    Roots may be either directories (walked recursively) or single files
    (yielded as-is when they exist and end in ``.py``). Directory pruning
    happens in-place via ``os.walk``'s ``dirnames`` mutation, so excluded
    directories are not descended into. The skip applies at every depth,
    so a ``tests`` or ``_archive`` directory nested inside an otherwise
    active package is still skipped.
    """

    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".py":
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Mutate in place so os.walk skips excluded subtrees entirely.
            dirnames[:] = [d for d in dirnames if d not in ACTIVE_EXCLUDES]
            for filename in filenames:
                if filename.endswith(".py"):
                    yield Path(dirpath) / filename


def _scan_active(
    pattern: "re.Pattern[str]",
    *roots: Path,
    exclude_files: Iterable[Path] = (),
) -> List[Tuple[str, int, str]]:
    """Return ``(relative_path, line_number, line_text)`` tuples for ``pattern``.

    ``relative_path`` is rendered as a POSIX-style path relative to the
    repository root so failure output is stable across operating systems.
    ``exclude_files`` allowlists specific files that are intentional
    homes for the pattern (for example, ``common_edges.py`` is the
    canonical home for shared adapter helpers and wrapper factories).
    """

    excluded = {Path(p).resolve() for p in exclude_files}
    occurrences: List[Tuple[str, int, str]] = []
    for path in _iter_active_python_files(*roots):
        resolved = path.resolve()
        if resolved in excluded:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Defensive: a binary or unreadable file should not crash the
            # guard; skip it. Production source is UTF-8 Python, so this
            # is a belt-and-braces fallback.
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                rel = resolved.relative_to(_REPO_ROOT).as_posix()
                occurrences.append((rel, line_number, line.rstrip()))
    return occurrences


def _format_occurrences(occurrences: Iterable[Tuple[str, int, str]]) -> str:
    """Render occurrences as a human-readable failure message."""

    return "\n".join(
        f"  {rel}:{line_no}: {text}" for rel, line_no, text in occurrences
    )


# Repository-relative roots used by the guards below. Computed once so
# tests do not repeatedly rebuild ``Path`` objects.
_AGENT_GRAPH = _REPO_ROOT / "agent" / "graph"
_LANGGRAPH_CHAT = _REPO_ROOT / "backend" / "services" / "langgraph_chat"
_AGENT_GRAPH_BUILDERS = _AGENT_GRAPH / "builders"
_GRAPH_BUILDER_FILE = _AGENT_GRAPH / "graph_builder.py"
_DEEP_REASONING_BUILDER_FILE = _AGENT_GRAPH_BUILDERS / "deep_reasoning_builder.py"
_SIMPLE_TOOL_BUILDER_FILE = _AGENT_GRAPH_BUILDERS / "simple_tool_builder.py"
_COMMON_EDGES_FILE = _AGENT_GRAPH_BUILDERS / "common_edges.py"


def test_static_inventory_facts_metadata_or_fallback_under_baseline():
    """No active call site uses ``... facts.metadata or {}`` after Phase 5.

    Matches the close variants called out in the guide (``facts.metadata``,
    ``interactive.facts.metadata``, ``state.facts.metadata``) so the
    inventory tracks every shape that the safe accessor will replace.

    Phase 5.4 closed the metadata migration: Tasks 5.1/5.2/5.3 replaced
    every active ``facts.metadata or {}``-style fallback with
    ``safe_metadata`` / ``metadata_copy()`` / ``ensure_metadata()`` across
    builders, nodes, utils, runtime, and ``backend/services/langgraph_chat``.
    The strict assertion below is now the active enforcement gate; any
    regression that re-introduces the literal pattern fails this guard
    immediately.
    """

    pattern = re.compile(
        r"(?:^|[^.\w])(?:\w+\.)*facts\.metadata\s+or\s+\{\s*\}"
    )
    occurrences = _scan_active(pattern, _AGENT_GRAPH, _LANGGRAPH_CHAT)

    assert occurrences == [], (
        "Active modules still use `facts.metadata or {}`-style fallback; "
        "migrate to FactsState.safe_metadata / metadata_copy() / "
        "ensure_metadata():\n" + _format_occurrences(occurrences)
    )


def test_static_inventory_facts_decision_history_or_fallback_under_baseline():
    """No active call site uses ``... facts.decision_history or []`` after Phase 5.

    Phase 5.4 closed the decision-history migration: Tasks 5.1/5.2/5.3
    replaced every active ``facts.decision_history or []``-style fallback
    with ``safe_decision_history`` / ``ensure_decision_history()`` across
    builders, nodes, and runtime helpers. The strict assertion below is
    now the active enforcement gate.
    """

    pattern = re.compile(
        r"(?:^|[^.\w])(?:\w+\.)*facts\.decision_history\s+or\s+\[\s*\]"
    )
    occurrences = _scan_active(pattern, _AGENT_GRAPH)

    assert occurrences == [], (
        "Active modules still use `facts.decision_history or []`-style "
        "fallback; migrate to FactsState.safe_decision_history / "
        "ensure_decision_history():\n" + _format_occurrences(occurrences)
    )


def test_static_inventory_facts_todo_list_or_fallback_under_baseline():
    """No active call site uses ``... facts.todo_list or []`` after Phase 5.

    Phase 5.4 closed the todo-list migration: Tasks 5.1/5.2/5.3 replaced
    every active ``facts.todo_list or []``-style fallback with
    ``safe_todo_list`` across builders, nodes, and runtime helpers. The
    strict assertion below is now the active enforcement gate.
    """

    pattern = re.compile(
        r"(?:^|[^.\w])(?:\w+\.)*facts\.todo_list\s+or\s+\[\s*\]"
    )
    occurrences = _scan_active(pattern, _AGENT_GRAPH)

    assert occurrences == [], (
        "Active modules still use `facts.todo_list or []`-style fallback; "
        "migrate to FactsState.safe_todo_list:\n"
        + _format_occurrences(occurrences)
    )


def test_static_inventory_local_wrapper_factories_outside_common_edges():
    """Local ``_wrap_with_context*`` factories must vanish outside ``common_edges.py``.

    Phase 3.1 lands ``wrap_with_context`` / ``wrap_with_context_async``
    in ``agent/graph/builders/common_edges.py``. Phase 3.2 deletes the
    deep-reasoning underscore-prefixed copies. After Phase 3.2 ships,
    no underscore-prefixed wrapper factory definition should exist
    anywhere under ``agent/graph`` — the canonical home is
    ``common_edges.py``, and that file is allowlisted here for the
    public (non-underscore) names.
    """

    pattern = re.compile(
        r"^\s*def\s+_wrap_with_context(?:_async)?\s*\("
    )
    occurrences = _scan_active(
        pattern,
        _AGENT_GRAPH,
        exclude_files=[_COMMON_EDGES_FILE],
    )

    assert occurrences == [], (
        "Local _wrap_with_context / _wrap_with_context_async factories "
        "still exist outside common_edges.py; delete them and use the "
        "shared wrap_with_context / wrap_with_context_async helpers:\n"
        + _format_occurrences(occurrences)
    )


def test_static_inventory_extract_runtime_context_in_builder_wrappers():
    """Builder wrapper bodies must not call ``extract_runtime_context(state)`` directly.

    After Phase 4, builder wrappers use ``wrap_with_context`` /
    ``wrap_with_context_async`` from ``common_edges.py``, which calls
    ``extract_runtime_context`` once internally. Direct calls inside
    ``deep_reasoning_builder.py`` / ``simple_tool_builder.py`` /
    ``graph_builder.py`` indicate boilerplate that should be replaced
    by a wrapper-factory call. ``common_edges.py`` retains the canonical
    direct call and is allowlisted by scope (it is not in the file list).

    Phase 4.2 closed out the remaining direct call by migrating the
    simple-tool builder's explicit wrapper helpers to
    ``wrap_with_context`` / ``wrap_with_context_async``, so the strict
    assertion below is now the active enforcement gate.
    """

    pattern = re.compile(r"\bextract_runtime_context\s*\(\s*state\s*\)")
    occurrences = _scan_active(
        pattern,
        _DEEP_REASONING_BUILDER_FILE,
        _SIMPLE_TOOL_BUILDER_FILE,
        _GRAPH_BUILDER_FILE,
    )

    assert occurrences == [], (
        "Builder wrapper bodies still call extract_runtime_context(state) "
        "directly; route them through wrap_with_context / "
        "wrap_with_context_async in common_edges.py:\n"
        + _format_occurrences(occurrences)
    )


def test_static_inventory_from_mapping_in_builder_routes():
    """Builders must not call ``InteractiveState.from_mapping(state)`` directly.

    After Phase 2, builder route and predicate functions use
    ``with_interactive_state(...)`` adapter from ``common_edges.py``.
    The scope mirrors the guide's verification command at line 1096:
    ``agent/graph/builders`` plus ``agent/graph/graph_builder.py``,
    excluding ``common_edges.py`` (the canonical adapter home).

    Phase 4.1 closed out the last remaining offender (the
    ``_ensure_state`` bootstrap helper in ``graph_builder.py``) by binding
    it to a ``raw_state`` parameter, so the literal-token guard now passes
    strictly. Any future regression — a builder route or wrapper that
    re-introduces a literal ``InteractiveState.from_mapping(state)`` call —
    will fail this test and force migration through
    ``with_interactive_state(...)``.
    """

    pattern = re.compile(r"\bInteractiveState\.from_mapping\s*\(\s*state\s*\)")
    occurrences = _scan_active(
        pattern,
        _AGENT_GRAPH_BUILDERS,
        _GRAPH_BUILDER_FILE,
        exclude_files=[_COMMON_EDGES_FILE],
    )

    assert occurrences == [], (
        "Builder layer still calls InteractiveState.from_mapping(state) "
        "outside common_edges.py; migrate route/predicate functions to "
        "with_interactive_state(...) or allowlist intentional "
        "graph/node boundary conversions:\n"
        + _format_occurrences(occurrences)
    )
