"""Single builder authority for the hot-path ``ConversationContextBundle``.

This module owns the *one* assembly step that turns already-loaded
turn inputs into a ``ConversationContextBundle``. Per the Phase 1
contract, every prompt-authoritative LangGraph role consumes this
bundle (directly or via a projection); no parallel assembly path may
exist elsewhere in the codebase.

Inputs expected
---------------
- **Canonical transcript** — OpenAI-style message dicts as produced by
  ``ConversationHistoryReader.build_openai_conversation_history``. The
  transcript-window policy in ``agent.graph.context.transcript`` owns
  selection; this builder only invokes it.
- **Deterministic runtime state** — a ``RuntimeStateSnapshot``. Callers
  may omit it, in which case an explicit empty snapshot is produced.
  Runtime-state narrowing (active target, goal, decision, in-flight
  tool, handles) is owned by ``agent/graph/nodes/working_memory.py``
  and populated on the hot path there, not in this builder.
- **Evidence refs** — a list of ``EvidenceRef`` references. Wiring of
  real evidence refs remains out of scope for this migration; this
  builder accepts an explicit list (defaulting to empty) so the
  contract shape stays stable when that work lands.

Output guarantees
-----------------
- Pure and deterministic for a given input set — same inputs produce
  byte-identical bundles across calls.
- Section ordering is stable: shared transcript window, classifier-only full
  transcript window, runtime state, evidence refs (in caller-provided order), and an empty
  ``retrieved_prior_context`` slot that remains reserved for future
  long-term retrieval work.
- The returned bundle is a plain ``dict`` at runtime (``TypedDict``),
  so it can be placed directly on LangGraph state / metadata without
  further conversion.

Facade integration contract
---------------------------
The single assembly authority is
``backend/services/langgraph_chat/context_builder.LangGraphContextBuilder.build_runtime_config``.
It invokes this builder exactly once during turn setup and writes the
resulting bundle into ``runtime_config.metadata`` under the stable key
``METADATA_CONTEXT_BUNDLE_KEY = "context_bundle"``. Downstream,
``facade_helpers.build_metadata`` copies the pre-built bundle (same
dict reference) into the initial graph state metadata — it does **not**
rebuild the bundle — and ``refresh_bundle_from_working_memory`` keeps
runtime-state / evidence refs aligned with canonical working memory
as nodes mutate it.

After the Phase 5 cutover, every prompt consumer reads this bundle
(directly or via a projection); the legacy
``metadata["conversation_history"]`` key remains for compatibility with
reducers, persistence, and operational bookkeeping, and is **not** a
prompt authority.

Scope notes
-----------
- No projection logic here. Projections live in
  ``agent.graph.context.projections`` and read from this bundle.
- No prompt serialization here. Cache-friendly prompt sections are
  emitted by ``serialize_projection_to_prompt_sections`` in
  ``projections.py``.
- No consumer wiring here. This module only produces the bundle.
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.contracts import (
    ConversationContextBundle,
    EvidenceRef,
    PriorTurnReferences,
    RuntimeStateSnapshot,
)
from agent.graph.context.transcript import (
    select_full_transcript_window,
    select_recent_transcript_window,
)


METADATA_CONTEXT_BUNDLE_KEY = "context_bundle"
"""Stable metadata key used by the facade.

Prompt consumers read from this key (directly or via a projection).
The legacy ``metadata["conversation_history"]`` key remains a
compatibility-only write after the Phase 5 authority cutover; it is
not a prompt authority.
"""


def _empty_runtime_state() -> RuntimeStateSnapshot:
    """Produce an explicit empty ``RuntimeStateSnapshot``.

    Kept private and trivial: the real runtime-state narrowing logic
    (active target, goal, decision, in-flight tool, handles) is owned
    by ``agent/graph/nodes/working_memory.py`` and populated on the
    hot path there. This helper returns a fully-shaped empty snapshot
    so the bundle contract stays valid when a caller (e.g., setup-time
    facade build) has no runtime state yet.
    """
    return RuntimeStateSnapshot(
        active_target=None,
        current_goal=None,
        current_decision=None,
        in_flight_tool=None,
        handles={},
        active_todo=None,
    )


def _coerce_current_user_turn(
    current_message: str | None,
) -> dict[str, Any] | None:
    """Normalise the in-flight user message into the bundle shape.

    Takes the raw string form callers hold (``chat_inputs.message``)
    and returns the canonical ``{"role": "user", "content": ...}``
    dict, or ``None`` when no message is in flight (``None`` or
    empty string).
    """
    if not current_message:
        return None
    return {"role": "user", "content": str(current_message)}


def empty_prior_turn_references_context() -> PriorTurnReferences:
    """Return the default materialized prior-turn reference section."""
    return PriorTurnReferences(
        operation="none",
        status="none",
        materialized_turns=[],
        unresolved_hints=[],
    )


def _coerce_prior_turn_references(
    prior_turn_references: dict[str, Any] | None,
) -> PriorTurnReferences:
    """Normalize a materialized prior-turn reference payload for the bundle."""
    if not isinstance(prior_turn_references, dict):
        return empty_prior_turn_references_context()
    operation = prior_turn_references.get("operation")
    status = prior_turn_references.get("status")
    materialized_turns = prior_turn_references.get("materialized_turns")
    unresolved_hints = prior_turn_references.get("unresolved_hints")
    return PriorTurnReferences(
        operation=operation if isinstance(operation, str) and operation else "none",
        status=status if isinstance(status, str) and status else "none",
        materialized_turns=list(materialized_turns)
        if isinstance(materialized_turns, list)
        else [],
        unresolved_hints=list(unresolved_hints)
        if isinstance(unresolved_hints, list)
        else [],
    )


def update_prior_turn_references(
    bundle: ConversationContextBundle,
    prior_turn_references: dict[str, Any] | None,
) -> None:
    """Update the bundle's materialized prior-turn reference section in place."""
    bundle["prior_turn_references"] = _coerce_prior_turn_references(
        prior_turn_references
    )


def build_conversation_context_bundle(
    conversation_id: str,
    turn_id: str,
    turn_sequence: int,
    messages: list[dict[str, Any]],
    *,
    runtime_state: RuntimeStateSnapshot | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
    prior_turn_references: dict[str, Any] | None = None,
    current_message: str | None = None,
) -> ConversationContextBundle:
    """Assemble the hot-path ``ConversationContextBundle`` for one turn.

    This is the single assembly authority. Callers pass already-loaded
    inputs (transcript messages, optional runtime state, optional
    evidence refs, and the in-flight user message) and receive the
    canonical bundle consumed by every prompt-authoritative role
    (directly or via a projection).

    Parameters
    ----------
    conversation_id:
        Stable identifier for the enclosing conversation / task.
    turn_id:
        Identifier of the turn this bundle is built for.
    turn_sequence:
        Monotonic zero-based index of the turn within the conversation.
    messages:
        Canonical transcript as OpenAI-style message dicts. Passed
        through to ``select_recent_transcript_window`` without
        modification. The in-flight user message is **not** expected
        to be appended here — pass it via ``current_message`` so the
        bundle keeps prior and in-flight turns separate.
    runtime_state:
        Optional deterministic runtime-state snapshot. When ``None``,
        an explicit empty snapshot is produced. Runtime-state narrowing
        is owned by ``agent/graph/nodes/working_memory.py`` and is not
        re-implemented here.
    evidence_refs:
        Optional ordered list of evidence references. When ``None``, an
        empty list is used. Real evidence-ref wiring remains out of
        scope for this migration.
    prior_turn_references:
        Optional materialized prior-turn reference context. When
        ``None``, the bundle carries the safe empty ``none`` shape.
    current_message:
        Optional in-flight user message (raw string from
        ``chat_inputs.message``). When ``None`` or an empty string,
        ``current_user_turn`` is ``None`` on the bundle. This field
        is the single authority for the current turn across every
        consumer — see ``ConversationContextBundle``.

    Returns
    -------
    ConversationContextBundle
        A populated bundle with stable section ordering and an empty
        ``retrieved_prior_context`` slot (reserved for future
        long-term retrieval work).
    """
    transcript_window = select_recent_transcript_window(messages)
    classifier_transcript_window = select_full_transcript_window(messages)

    bundle_runtime_state = runtime_state if runtime_state is not None else _empty_runtime_state()
    bundle_evidence_refs: list[EvidenceRef] = list(evidence_refs) if evidence_refs else []

    return ConversationContextBundle(
        conversation_id=conversation_id,
        turn_id=turn_id,
        turn_sequence=turn_sequence,
        transcript_window=transcript_window,
        classifier_transcript_window=classifier_transcript_window,
        runtime_state=bundle_runtime_state,
        evidence_refs=bundle_evidence_refs,
        prior_turn_references=_coerce_prior_turn_references(prior_turn_references),
        current_user_turn=_coerce_current_user_turn(current_message),
        retrieved_prior_context=[],
    )


__all__ = [
    "METADATA_CONTEXT_BUNDLE_KEY",
    "build_conversation_context_bundle",
    "empty_prior_turn_references_context",
    "update_prior_turn_references",
]
