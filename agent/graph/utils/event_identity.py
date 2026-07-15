"""Graph event identity helpers.

These helpers provide canonical conversation/turn identity and turn-sequence
resolution without relying on deprecated streaming helper modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional, Tuple

from .dr_iteration_state import _advance_dr_iteration, _ensure_dr_iteration


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                return None
    return None


def _get_facts(state: Any) -> Any:
    if state is None:
        return None
    if hasattr(state, "facts"):
        return state.facts
    if isinstance(state, Mapping):
        return state.get("facts")
    return None


def _get_metadata(facts: Any) -> Mapping[str, Any]:
    metadata = getattr(facts, "metadata", None) if facts is not None else None
    if isinstance(metadata, Mapping):
        return metadata
    return {}


def _metadata_turn_id(metadata: Mapping[str, Any]) -> Optional[str]:
    turn_id = metadata.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id

    runtime_context = metadata.get("graph_runtime_context")
    if isinstance(runtime_context, Mapping):
        context_turn_id = runtime_context.get("turn_id")
        if isinstance(context_turn_id, str) and context_turn_id:
            return context_turn_id
    elif runtime_context is not None:
        context_turn_id = getattr(runtime_context, "turn_id", None)
        if isinstance(context_turn_id, str) and context_turn_id:
            return context_turn_id

    return None


def resolve_turn_sequence(
    context: Optional[Any] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Resolve canonical turn sequence from runtime context or metadata."""
    if context is not None:
        resolved = _coerce_int(getattr(context, "turn_sequence", None))
        if resolved is not None:
            return resolved

    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    resolved = _coerce_int(metadata_map.get("turn_sequence"))
    if resolved is not None:
        return resolved

    runtime_context = metadata_map.get("graph_runtime_context")
    if isinstance(runtime_context, Mapping):
        return _coerce_int(runtime_context.get("turn_sequence"))
    if runtime_context is not None:
        return _coerce_int(getattr(runtime_context, "turn_sequence", None))

    return None


def resolve_identity_from_config(
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str, Optional[int]]:
    """Resolve canonical identity fields from LangGraph config."""
    configurable = {}
    if isinstance(config, Mapping):
        candidate = config.get("configurable")
        if isinstance(candidate, Mapping):
            configurable = candidate

    conversation_id = configurable.get("canonical_conversation_id")
    turn_id = configurable.get("canonical_turn_id")
    turn_sequence = _coerce_int(configurable.get("canonical_turn_sequence"))

    return (
        conversation_id if isinstance(conversation_id, str) else "",
        turn_id if isinstance(turn_id, str) else "",
        turn_sequence,
    )


def _resolve_turn_id_from_state(
    state: Any,
    config: Optional[Mapping[str, Any]] = None,
) -> str:
    facts = _get_facts(state)
    task_id = getattr(facts, "task_id", None) if facts is not None else None
    default_turn_id = f"lg-{task_id if task_id is not None else 'unknown'}"

    metadata = _get_metadata(facts)
    metadata_turn_id = _metadata_turn_id(metadata)
    if metadata_turn_id:
        return metadata_turn_id

    if isinstance(config, Mapping):
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            thread_id = configurable.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id

    return default_turn_id


def resolve_stream_identifiers(
    state: Any,
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Resolve (conversation_id, turn_id) for streaming events."""
    config_conversation_id, config_turn_id, _ = resolve_identity_from_config(config)
    facts = _get_facts(state)

    if config_conversation_id:
        conversation_id = config_conversation_id
    else:
        conversation_id = getattr(facts, "conversation_id", None) if facts is not None else None
        if not isinstance(conversation_id, str):
            conversation_id = ""

    turn_id = config_turn_id or _resolve_turn_id_from_state(state, config)
    return conversation_id, turn_id


def resolve_canonical_identity(
    state: Any,
    config: Optional[Mapping[str, Any]] = None,
    context: Optional[Any] = None,
) -> Tuple[str, str, Optional[int]]:
    """Resolve (conversation_id, turn_id, turn_sequence) with canonical precedence."""
    config_conversation_id, config_turn_id, config_turn_sequence = resolve_identity_from_config(config)
    facts = _get_facts(state)
    metadata = _get_metadata(facts)

    if config_conversation_id:
        conversation_id = config_conversation_id
    else:
        conversation_id = getattr(facts, "conversation_id", None) if facts is not None else None
        if not isinstance(conversation_id, str):
            conversation_id = ""

    turn_id = config_turn_id or _resolve_turn_id_from_state(state, config)
    turn_sequence = (
        config_turn_sequence
        if config_turn_sequence is not None
        else resolve_turn_sequence(context, metadata)
    )

    return conversation_id, turn_id, turn_sequence


def resolve_sub_turn_index(
    metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Resolve a stable sub-turn index for repeated intra-turn sections.

    This is used to segregate repeated observation/reasoning sections that share
    the same canonical `turn_id` (for example DR iterations or simple-tool
    retry attempts).

    Precedence:
    1) explicit `metadata.sub_turn_index`
    2) `metadata.dr_iteration_meta.counter` (or `active_iteration`)
    3) `metadata[RETRY_METADATA_KEY].count`
    """
    meta = metadata if isinstance(metadata, Mapping) else {}

    explicit = _coerce_int(meta.get("sub_turn_index"))
    if explicit is not None and explicit >= 0:
        return explicit

    dr_meta = meta.get("dr_iteration_meta")
    if isinstance(dr_meta, Mapping):
        dr_counter = _coerce_int(dr_meta.get("counter"))
        if dr_counter is None:
            dr_counter = _coerce_int(dr_meta.get("active_iteration"))
        if dr_counter is not None and dr_counter >= 0:
            return dr_counter

    # Lazy import: ``nodes.post_tool_reasoning.__init__`` imports back from
    # this module (``derive_dr_stream_identifiers``); a top-level import of
    # the canonical retry-metadata key would create a cycle. The deferred
    # import lets us reach the active ``RETRY_METADATA_KEY`` without
    # restructuring the package init.
    from ..nodes.post_tool_reasoning.core.retry_logic import RETRY_METADATA_KEY

    retry_meta = meta.get(RETRY_METADATA_KEY)
    if isinstance(retry_meta, Mapping):
        retry_count = _coerce_int(retry_meta.get("count"))
        if retry_count is not None and retry_count >= 0:
            return retry_count

    return None


def resolve_direct_executor_step_index(state: Any) -> int:
    """Return the current direct-executor step index from prior sanctioned steps.

    The direct executor now supports bounded multi-step execution within a
    single turn. Repeated tool/observation sections therefore need the same
    shared repeated-section identity model that DR uses, but without copying
    DR-specific planner iteration state.

    This helper derives the current direct-executor step index from existing
    turn state only:

    - Count prior ``call_tool`` decisions already recorded in
      ``facts.decision_history``.
    - The first tool execution in the turn therefore resolves to ``0``.
    - Each sanctioned follow-up step (bounded continuation or corrective
      retry) increments naturally because PTR already records ``call_tool``
      decisions before the graph loops back to dispatch.

    No new counters or metadata structures are introduced here.
    """
    facts = _get_facts(state)
    if facts is None:
        return 0

    # Local import: ``decision_router.helpers`` triggers loading of the
    # ``nodes`` package which transitively imports event identity helpers,
    # so the import must be deferred to call time to avoid a cycle.
    from ..nodes.decision_router.helpers import extract_action_label

    decision_history = getattr(facts, "decision_history", None) or []
    count = 0
    for entry in decision_history:
        action = extract_action_label(str(entry or "")).lower()
        if action == "call_tool":
            count += 1
    return count


from ..state import InteractiveState  # noqa: E402


def legacy_resolve_turn_sequence(
    context: Optional[Any] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """Legacy turn-sequence resolver used by streaming_helpers compatibility wrappers."""
    if context is not None:
        seq = getattr(context, "turn_sequence", None)
        if isinstance(seq, (int, float)):
            return int(seq)

    if metadata and isinstance(metadata, Mapping):
        seq = metadata.get("turn_sequence")
        if isinstance(seq, (int, float)):
            return int(seq)

        graph_runtime_context = metadata.get("graph_runtime_context")
        if isinstance(graph_runtime_context, Mapping):
            seq = graph_runtime_context.get("turn_sequence")
            if isinstance(seq, (int, float)):
                return int(seq)
        elif graph_runtime_context is not None:
            seq = getattr(graph_runtime_context, "turn_sequence", None)
            if isinstance(seq, (int, float)):
                return int(seq)

    return None


def legacy_resolve_identity_from_config(
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str, Optional[int]]:
    """Legacy config resolver used by streaming_helpers compatibility wrappers."""
    configurable = (config or {}).get("configurable", {})
    return (
        configurable.get("canonical_conversation_id", ""),
        configurable.get("canonical_turn_id", ""),
        configurable.get("canonical_turn_sequence"),
    )


def legacy_get_thread_identifier(
    interactive: InteractiveState,
    config: Optional[Mapping[str, Any]],
) -> str:
    """Legacy thread-id fallback chain used by streaming_helpers compatibility wrappers."""
    facts = interactive.facts
    default_thread_id = f"lg-{facts.task_id}"
    metadata = getattr(facts, "metadata", None)
    if isinstance(metadata, Mapping):
        turn_id = metadata.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
        graph_runtime_context = metadata.get("graph_runtime_context")
        if isinstance(graph_runtime_context, Mapping):
            context_turn_id = graph_runtime_context.get("turn_id")
            if isinstance(context_turn_id, str) and context_turn_id:
                return context_turn_id
        elif graph_runtime_context is not None:
            context_turn_id = getattr(graph_runtime_context, "turn_id", None)
            if isinstance(context_turn_id, str) and context_turn_id:
                return context_turn_id
    if config and isinstance(config, Mapping):
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            thread_id = configurable.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return default_thread_id


def legacy_derive_stream_identifiers(
    interactive: InteractiveState,
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Legacy stream identity resolution used by streaming_helpers compatibility wrappers."""
    conversation_id = getattr(getattr(interactive, "facts", None), "conversation_id", None) or ""
    task_id = getattr(getattr(interactive, "facts", None), "task_id", None) or "unknown"
    turn_id = legacy_get_thread_identifier(interactive, config) or f"lg-{task_id}"
    return conversation_id, turn_id


def derive_dr_stream_identifiers(
    interactive: InteractiveState,
    config: Optional[Mapping[str, Any]],
    *,
    advance_iteration: bool = False,
) -> Tuple[str, str, int]:
    """Compatibility wrapper returning DR iteration-specific stream identifiers."""
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    dr_meta = metadata.setdefault("dr_iteration_meta", {})

    if advance_iteration:
        iteration = _advance_dr_iteration(dr_meta)
    else:
        iteration = _ensure_dr_iteration(dr_meta)

    metadata["dr_iteration_meta"] = dr_meta
    conversation_id = interactive.facts.conversation_id or ""
    base_turn_id = legacy_get_thread_identifier(interactive, config)
    return conversation_id, f"{base_turn_id}-dr-iter-{iteration}", iteration


__all__ = [
    "resolve_direct_executor_step_index",
    "resolve_canonical_identity",
    "resolve_identity_from_config",
    "resolve_sub_turn_index",
    "resolve_stream_identifiers",
    "resolve_turn_sequence",
]
