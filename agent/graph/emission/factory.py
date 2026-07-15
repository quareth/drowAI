"""Factory for capability-based UnifiedEventEmitter selection.

Creates SimpleEmitter for simple_tool_execution and respond_only,
and DeepReasoningEmitter for deep_reasoning capability.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, TYPE_CHECKING

from agent.graph.emission.unified_emitter import (
    DeepReasoningEmitter,
    SimpleEmitter,
    UnifiedEventEmitter,
)

if TYPE_CHECKING:
    from agent.graph.infrastructure.state_models import GraphRuntimeContext
    from agent.graph.state import InteractiveState


def _get_capability(state: Any) -> Optional[str]:
    """Resolve capability from state.facts (capability or metadata.capability)."""
    if state is None or not hasattr(state, "facts"):
        return None
    facts = state.facts
    if facts is None:
        return None
    # Prefer direct capability field
    cap = getattr(facts, "capability", None)
    if cap is not None and isinstance(cap, str):
        return cap
    meta = getattr(facts, "metadata", None)
    if isinstance(meta, Mapping):
        cap = meta.get("capability")
        if isinstance(cap, str):
            return cap
    return None


class EventEmitterFactory:
    """Static factory for creating capability-specific emitters."""

    CAPABILITY_DEEP_REASONING = "deep_reasoning"

    @staticmethod
    def create(
        writer: Any,
        state: "InteractiveState",
        config: Optional[Mapping[str, Any]] = None,
        context: Optional["GraphRuntimeContext"] = None,
    ) -> UnifiedEventEmitter:
        """Create capability-specific emitter.

        Uses state.facts.capability or state.facts.metadata['capability'].
        If capability is 'deep_reasoning', returns DeepReasoningEmitter;
        otherwise returns SimpleEmitter (simple_tool_execution, respond_only, etc.).
        """
        capability = _get_capability(state)
        if capability == EventEmitterFactory.CAPABILITY_DEEP_REASONING:
            return DeepReasoningEmitter(writer, state, config, context)
        return SimpleEmitter(writer, state, config, context)

    @staticmethod
    def create_simple(
        writer: Any,
        state: "InteractiveState",
        config: Optional[Mapping[str, Any]] = None,
        context: Optional["GraphRuntimeContext"] = None,
    ) -> SimpleEmitter:
        """Create SimpleEmitter regardless of capability. Useful for tests."""
        return SimpleEmitter(writer, state, config, context)

    @staticmethod
    def create_deep_reasoning(
        writer: Any,
        state: "InteractiveState",
        config: Optional[Mapping[str, Any]] = None,
        context: Optional["GraphRuntimeContext"] = None,
    ) -> DeepReasoningEmitter:
        """Create DeepReasoningEmitter regardless of capability. Useful for tests."""
        return DeepReasoningEmitter(writer, state, config, context)

    @staticmethod
    def create_turn_level(
        writer: Any,
        state: "InteractiveState",
        config: Optional[Mapping[str, Any]] = None,
        context: Optional["GraphRuntimeContext"] = None,
    ) -> UnifiedEventEmitter:
        """Create emitter with canonical turn identity and no sub-turn tracking.

        Use this for turn-level events (e.g., final answer/message section end).
        The returned emitter resolves canonical identity and sets
        sub_turn_index=None.
        """
        conversation_id, turn_id, turn_sequence = UnifiedEventEmitter.resolve_canonical_identity(
            state, config, context
        )
        return EventEmitterFactory.create_from_identity(
            writer,
            conversation_id,
            turn_id,
            turn_sequence=turn_sequence,
            sub_turn_index=None,
        )

    @staticmethod
    def create_deep_reasoning_finalizer(
        writer: Any,
        state: "InteractiveState",
        config: Optional[Mapping[str, Any]] = None,
        context: Optional["GraphRuntimeContext"] = None,
    ) -> UnifiedEventEmitter:
        """Backward-compatible alias for create_turn_level()."""
        return EventEmitterFactory.create_turn_level(writer, state, config, context)

    @staticmethod
    def create_from_identity(
        writer: Any,
        conversation_id: str,
        turn_id: str,
        turn_sequence: Optional[int] = None,
        sub_turn_index: Optional[int] = None,
    ) -> UnifiedEventEmitter:
        """Create emitter with pre-resolved identity.

        Use this when the caller has already resolved canonical identity
        (e.g., streaming adapters that receive identity as parameters).
        """
        # UnifiedEventEmitter has no abstract methods so it can be instantiated directly
        return UnifiedEventEmitter(
            writer, conversation_id, turn_id,
            turn_sequence=turn_sequence,
            sub_turn_index=sub_turn_index,
        )
