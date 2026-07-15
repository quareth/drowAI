"""Reusable conditional edge helpers for LangGraph builders.

This module also hosts the shared wrapper factories used to expose graph
nodes to LangGraph with consistent runtime-context extraction and opt-in
``config`` / ``writer`` forwarding. Tier 2 centralizes the factories here
so per-builder wrappers do not drift in shape, and so a single observability
hook (``on_wrap_log``) can stamp diagnostics across all builders.
"""

import asyncio
import inspect
from collections.abc import Callable, MutableMapping
from typing import Any, Dict, Mapping, Optional, TypeVar

from langgraph.config import RunnableConfig
from langgraph.types import StreamWriter

from ..guards import capability_in
from ..infrastructure.state_models import GraphRuntimeContext, build_budget_envelope
from ..state import InteractiveState


RouteResult = TypeVar("RouteResult")

WrapperLogCallback = Callable[[str, bool, bool], None]
"""Diagnostic callback shape: ``(node_name, writer_available, config_available)``.

Fires inside the shared wrapper factories before the wrapped node runs, so
callers get a uniform observability signal regardless of whether the wrapped
node accepts ``config`` or ``writer``.
"""


_KEYWORD_PARAMETER_KINDS = {
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
}
_PROTECTED_WRAPPER_KWARGS = {"state", "context", "config", "writer"}


def _filter_extra_kwargs(
    kwargs: Mapping[str, Any],
    *,
    accepted_extra_kwargs: set[str],
    accepts_var_kwargs: bool,
    protected_keys: set[str],
) -> Dict[str, Any]:
    """Return extra runtime kwargs safe to pass to a wrapped node."""

    if not kwargs:
        return {}
    if accepts_var_kwargs:
        return {
            key: value
            for key, value in kwargs.items()
            if key not in protected_keys
        }
    return {
        key: value
        for key, value in kwargs.items()
        if key in accepted_extra_kwargs and key not in protected_keys
    }


def with_interactive_state(
    handler: Callable[[InteractiveState], RouteResult],
) -> Callable[[Mapping[str, Any]], RouteResult]:
    """Adapt an InteractiveState handler to LangGraph state mappings.

    LangGraph passes the raw state mapping into route/predicate callables.
    Most builder routes immediately convert that mapping into an
    ``InteractiveState`` via ``InteractiveState.from_mapping(state)`` before
    doing anything useful. This adapter centralizes that boilerplate at
    graph wiring sites without changing node semantics: the wrapped handler
    still receives a single typed ``InteractiveState`` argument and returns
    whatever route value the builder expects.

    The adapter intentionally does *not* use ``functools.wraps`` so the
    runner exposes its own ``(state)`` signature to LangGraph rather than
    inheriting the handler's typed ``InteractiveState`` annotation, which
    can confuse LangGraph's input-schema inference for conditional edges.
    It copies display metadata manually so LangGraph branch registries
    still retain route-specific names for introspection and regression
    tests.
    """

    def _runner(state: Mapping[str, Any]) -> RouteResult:
        return handler(InteractiveState.from_mapping(state))

    _runner.__name__ = getattr(handler, "__name__", _runner.__name__)
    _runner.__qualname__ = getattr(handler, "__qualname__", _runner.__qualname__)
    _runner.__doc__ = getattr(handler, "__doc__", _runner.__doc__)
    _runner.__module__ = getattr(handler, "__module__", _runner.__module__)
    return _runner


def wrap_with_context(
    node: Callable[..., Dict[str, Any]],
    *,
    node_name: Optional[str] = None,
    on_wrap_log: Optional[WrapperLogCallback] = None,
) -> Callable[..., Dict[str, Any]]:
    """Wrap a sync node with runtime context plus opt-in LangGraph kwargs.

    LangGraph invokes nodes with ``state`` plus optional ``config`` and
    ``writer`` keyword arguments. Many graph nodes only need the runtime
    ``context`` extracted from ``state``; forwarding ``config`` or ``writer``
    blindly would break those nodes' signatures. This factory introspects the
    wrapped node once at creation time and forwards each optional kwarg only
    when the node's signature actually accepts it.

    The optional ``on_wrap_log`` diagnostic callback fires before the node
    runs and receives ``(node_name, writer_available, config_available)``,
    decoupling observability from the wrapped node's signature so
    instrumentation works uniformly across nodes.

    The wrapper deliberately does not use ``functools.wraps`` so its
    signature exposed via ``inspect.signature`` matches its actual
    accepting parameters ``(state, config, writer)``. This keeps
    LangGraph's signature-driven input-schema inference and config/writer
    detection aligned with how the wrapper is actually invoked.
    """

    signature = inspect.signature(node)
    accepts_config = "config" in signature.parameters
    accepts_writer = "writer" in signature.parameters
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    accepted_extra_kwargs = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in _KEYWORD_PARAMETER_KINDS
    } - _PROTECTED_WRAPPER_KWARGS

    def _runner(
        state: Mapping[str, Any],
        config: RunnableConfig | None = None,
        *,
        writer: StreamWriter = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if on_wrap_log is not None and node_name is not None:
            on_wrap_log(node_name, writer is not None, config is not None)
        node_kwargs: Dict[str, Any] = {"context": extract_runtime_context(state)}
        if accepts_config:
            node_kwargs["config"] = config
        if accepts_writer:
            node_kwargs["writer"] = writer
        node_kwargs.update(
            _filter_extra_kwargs(
                kwargs,
                accepted_extra_kwargs=accepted_extra_kwargs,
                accepts_var_kwargs=accepts_var_kwargs,
                protected_keys=set(node_kwargs),
            )
        )
        return node(state, **node_kwargs)

    return _runner


def wrap_with_context_async(
    node: Callable[..., Any],
    *,
    node_name: Optional[str] = None,
    on_wrap_log: Optional[WrapperLogCallback] = None,
) -> Callable[..., Any]:
    """Wrap an async-or-sync node with runtime context and opt-in LangGraph kwargs.

    Mirrors :func:`wrap_with_context` but returns an async runner so the
    same wrapper can register on graphs that mix coroutine and plain
    callable nodes (e.g. deep-reasoning's ``finalize_turn`` sync node
    alongside async nodes). When the wrapped node is a coroutine function,
    the runner ``await``s it; otherwise it calls the node directly.

    Signature introspection runs once at wrap-creation time and the
    optional diagnostic callback fires independently of whether the
    wrapped node accepts ``config`` / ``writer``.

    The wrapper deliberately does not use ``functools.wraps`` so the
    runner's signature stays accurate for LangGraph's runtime introspection.
    """

    signature = inspect.signature(node)
    accepts_config = "config" in signature.parameters
    accepts_writer = "writer" in signature.parameters
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    accepted_extra_kwargs = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in _KEYWORD_PARAMETER_KINDS
    } - _PROTECTED_WRAPPER_KWARGS
    is_coroutine = asyncio.iscoroutinefunction(node)

    async def _runner(
        state: Mapping[str, Any],
        config: RunnableConfig | None = None,
        *,
        writer: StreamWriter = None,
        **kwargs: Any,
    ) -> Any:
        if on_wrap_log is not None and node_name is not None:
            on_wrap_log(node_name, writer is not None, config is not None)
        node_kwargs: Dict[str, Any] = {"context": extract_runtime_context(state)}
        if accepts_config:
            node_kwargs["config"] = config
        if accepts_writer:
            node_kwargs["writer"] = writer
        node_kwargs.update(
            _filter_extra_kwargs(
                kwargs,
                accepted_extra_kwargs=accepted_extra_kwargs,
                accepts_var_kwargs=accepts_var_kwargs,
                protected_keys=set(node_kwargs),
            )
        )
        if is_coroutine:
            return await node(state, **node_kwargs)
        return node(state, **node_kwargs)

    return _runner


def require_conditional_edges(graph: Any) -> Callable[..., Any]:
    """Return LangGraph's ``add_conditional_edges`` method or fail clearly.

    Both Tier 1 builders previously fetched ``add_conditional_edges`` via
    ``getattr`` and raised if it was missing. Centralizing the check keeps
    that requirement consistent across builders and removes per-call
    ``if callable(...)`` defensive blocks.
    """

    method = getattr(graph, "add_conditional_edges", None)
    if not callable(method):
        raise RuntimeError(
            "LangGraph's 'add_conditional_edges' is not available. "
            "Please ensure LangGraph >= 0.4.8."
        )
    return method


def wire_capability_gate(
    graph: Any,
    *,
    capability: str,
    false_target: str,
    source: str = "classification",
    true_target: str = "update_working_memory",
) -> Callable[..., Any]:
    """Wire the common graph-entry capability gate and return conditional edges.

    Both Tier 1 builders share the same gate shape: from ``classification``,
    if the chosen capability is present route to ``update_working_memory``,
    otherwise route to a builder-specific finalize node. This helper folds
    the duplicated predicate plus the conditional-edge wiring into one call.
    """

    conditional = require_conditional_edges(graph)

    @with_interactive_state
    def _has_capability(interactive: InteractiveState) -> bool:
        return capability_in(interactive.facts, [capability])

    conditional(source, _has_capability, {True: true_target, False: false_target})
    return conditional


def extract_runtime_context(state: Mapping[str, Any]) -> Optional[GraphRuntimeContext]:
    """Extract graph runtime context from state metadata when present."""
    metadata = (state or {}).get("facts", {}).get("metadata", {})
    payload = metadata.get("graph_runtime_context")
    if not payload:
        return None
    return GraphRuntimeContext.model_validate(payload)


def _resolve_runtime_budgets_from_facts(facts: Mapping[str, Any]) -> Dict[str, Any]:
    """Return runtime budget dict from facts payload (top-level or metadata)."""
    budgets = facts.get("runtime_budgets")
    if isinstance(budgets, Mapping):
        return dict(budgets)

    metadata = facts.get("metadata")
    if isinstance(metadata, Mapping):
        meta_budgets = metadata.get("runtime_budgets")
        if isinstance(meta_budgets, Mapping):
            return dict(meta_budgets)
    return {}


def ensure_metadata_runtime_budgets(metadata: MutableMapping[str, Any]) -> None:
    """Fill missing ``metadata['runtime_budgets']`` slots from shared defaults.

    Preserves existing non-null values so resumed turns keep decremented
    budgets. Only supplies defaults for keys that are absent or ``None``.
    """
    runtime_budgets = metadata.get("runtime_budgets")
    if not isinstance(runtime_budgets, Mapping):
        runtime_budgets = {}
    else:
        runtime_budgets = dict(runtime_budgets)

    defaults = build_budget_envelope().model_dump()
    for key, default_value in defaults.items():
        if runtime_budgets.get(key) is None and default_value is not None:
            runtime_budgets[key] = default_value

    metadata["runtime_budgets"] = runtime_budgets


def decrement_iteration_budget(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Decrement iteration counters in both facts and runtime budgets.
    
    Returns: Update dict suitable for merging into graph state
    """
    facts = dict((state or {}).get("facts", {}))
    
    # Increment iteration counter
    facts["iterations"] = facts.get("iterations", 0) + 1
    
    # Decrement runtime budget if present
    budgets = _resolve_runtime_budgets_from_facts(facts)
    if "remaining_iterations" in budgets and budgets["remaining_iterations"] is not None:
        budgets["remaining_iterations"] = max(0, budgets["remaining_iterations"] - 1)
        facts["runtime_budgets"] = budgets
    
    return {"facts": facts}


def decrement_tool_call_budget(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Decrement tool call counters in both facts and runtime budgets.
    
    Returns: Update dict suitable for merging into graph state
    """
    facts = dict((state or {}).get("facts", {}))
    
    # Increment tool calls used counter
    facts["tool_calls_used"] = facts.get("tool_calls_used", 0) + 1
    
    # Decrement runtime budget if present
    budgets = _resolve_runtime_budgets_from_facts(facts)
    if "remaining_tool_calls" in budgets and budgets["remaining_tool_calls"] is not None:
        budgets["remaining_tool_calls"] = max(0, budgets["remaining_tool_calls"] - 1)
        facts["runtime_budgets"] = budgets
    
    return {"facts": facts}


def increment_stuck_counter(state: Mapping[str, Any], action: str) -> Dict[str, Any]:
    """Track repeated actions to detect stuck loops.
    
    Increments counter if the same action is repeated, resets if action changes.
    
    Args:
        state: Current graph state
        action: Current action being taken
    
    Returns: Update dict suitable for merging into graph state
    """
    facts = dict((state or {}).get("facts", {}))
    
    # Get last action from decision history
    # Decision history entries are formatted as "action: reasoning", so extract just the action
    decision_history = facts.get("decision_history", [])
    if not decision_history:
        # First action, initialize counter to 0
        facts["stuck_counter"] = 0
        return {"facts": facts}
    
    # Get the last entry and extract action name (before the colon).
    # Local import avoids a cycle: ``nodes.decision_router`` imports from
    # ``builders.common_edges`` for ``increment_stuck_counter``.
    from ..nodes.decision_router.helpers import extract_action_label

    last_action = extract_action_label(decision_history[-1])
    
    # Increment if same action, reset if different
    if last_action == action:
        facts["stuck_counter"] = facts.get("stuck_counter", 0) + 1
    else:
        facts["stuck_counter"] = 0
    
    return {"facts": facts}


def build_router_action_map(
    *,
    call_tool_target: str,
    finalize_target: str,
) -> Dict[str, str]:
    """Build a route map for the full router action vocabulary.

    Router-facing maps include ``synthesis`` because the higher-level
    decision router may emit it. Builders supply graph-specific targets for
    ``call_tool`` and ``finalize`` (e.g. ``select_categories`` /
    ``format_results``) while every other action keeps its identity routing.
    """

    return {
        "think_more": "think_more",
        "call_tool": call_tool_target,
        "reflect": "reflect",
        "synthesis": "synthesis",
        "finalize": finalize_target,
    }


def build_post_tool_action_map(
    *,
    call_tool_target: str,
    finalize_target: str,
) -> Dict[str, str]:
    """Build a route map for the PTR (post-tool reasoning) action vocabulary.

    PTR-facing maps intentionally omit ``synthesis``: PTR's contract is the
    four-action set ``call_tool``, ``think_more``, ``reflect``, ``finalize``
    (see ``core.prompts.constants.VALID_POST_TOOL_ACTIONS``). A manually
    inserted ``"synthesis: ..."`` decision is therefore an invalid PTR
    action, not a valid PTR route, and routing must fall through to the
    builder's terminal default.
    """

    return {
        "think_more": "think_more",
        "call_tool": call_tool_target,
        "reflect": "reflect",
        "finalize": finalize_target,
    }


__all__ = [
    "extract_runtime_context",
    "ensure_metadata_runtime_budgets",
    "decrement_iteration_budget",
    "decrement_tool_call_budget",
    "increment_stuck_counter",
    "build_router_action_map",
    "build_post_tool_action_map",
    "require_conditional_edges",
    "wire_capability_gate",
    "with_interactive_state",
    "wrap_with_context",
    "wrap_with_context_async",
    "WrapperLogCallback",
]
