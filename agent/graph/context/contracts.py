"""Canonical hot-path memory contracts for LangGraph conversation context.

This module is the single source of truth for the prompt-authoritative
hot-path memory contract consumed by every LangGraph role. A
``ConversationContextBundle`` is assembled **once per turn** by the
dedicated builder (Task 1.3) and projected deterministically per role
by the shared projection layer (Phase 2). No other module in the
codebase may introduce a parallel prompt-authoritative memory
contract — readers consume these typed sections, or a projection
derived from them.

Data contract vs. render contract
---------------------------------
The types declared in this module are the **data contract** only —
the typed, machine-readable shape of the hot-path memory envelope.
Prompt-facing text rendering (role-labeled transcript blocks,
section ordering, cache-friendly separators) is the separate
**render contract** owned by ``agent.graph.context.serialization``.
The two must stay distinct:

- Adding or removing data fields here is a contract change (bundle
  readers and the builder must be updated).
- Changing how the serializer renders those fields into prompt
  sections is a render change (only ``serialization.py`` and its
  tests are affected).

``TranscriptWindow`` in particular is never a prompt-text contract.
Prompt builders and role nodes must not reach into
``transcript_window["turns"]`` and format message text themselves —
they consume the shared rendered transcript text produced by
``serialization.render_recent_transcript`` / the section serializer.
No second prompt-facing transcript data contract may be introduced
alongside ``TranscriptWindow``.

Section ownership
-----------------
- ``TranscriptWindow``
    Recent-turn transcript *data* authority. Verbatim messages
    within the selected window; window-selection policy lives in
    ``agent.graph.context.transcript`` and its public turn-grouping
    helper is the single authority for turn boundaries. Turn content
    is **never** truncated inside the window — if budget pressure
    requires trimming, the oldest turn is dropped whole. Prompt-text
    rendering of these turns is performed exclusively by the shared
    serializer (``agent.graph.context.serialization``), not here and
    not by individual prompt builders.

- ``RuntimeStateSnapshot``
    Deterministic, structured working-memory snapshot: active target,
    current goal / decision, in-flight tool, and durable handles. This
    section is explicitly *not* a natural-language summary — it is
    typed and machine-derived so every role reads consistent state.

- ``EvidenceRef``
    Pointer to a piece of evidence (finding, artifact, observation)
    that downstream roles may resolve through the evidence layer.
    References only; the evidence payloads themselves stay in their
    canonical stores to preserve cache-friendly prompt prefixes.

- ``PriorTurnReferences``
    Materialized canonical transcript rows referenced by the current
    turn. Classifier output is only a hint; this section carries the
    runtime-verified ``ChatMessage`` text that downstream prompts may
    quote or reason from.

- ``ConversationContextBundle``
    The single hot-path envelope. Combines identity fields
    (``conversation_id``, ``turn_id``, ``turn_sequence``) with the
    authoritative sections above plus a reserved
    ``retrieved_prior_context`` slot.

Scope notes
-----------
- ``retrieved_prior_context`` is **reserved and intentionally empty**
  for this migration. It is placed in the contract now so future
  long-term retrieval work can slot in without reshaping the bundle,
  but no hot-path consumer populates or reads it in this scope.
- Long-term memory summaries and ``trace.scratchpad`` continuity are not
  sections of this contract. The classifier-only transcript may contain the
  validated persisted compaction summary; no other role projection reads it.

Structural choice
-----------------
``TypedDict`` is used throughout so the contract is cheap to pass
through LangGraph state (plain ``dict`` at runtime) while remaining
statically checkable. Field shapes are kept as loose ``dict`` / ``list``
collections at this boundary — the richer per-section schemas land in
their dedicated modules (transcript, runtime state, evidence refs) and
are validated there, not re-declared here.
"""

from __future__ import annotations

from typing import Any, TypedDict


CLASSIFIER_TRANSCRIPT_WINDOW_KEY = "classifier_transcript_window"
"""Required classifier-only full-or-compacted projection key in the bundle."""


class TranscriptWindow(TypedDict):
    """Recent-turn transcript *data* section of the bundle.

    Carries the verbatim recent-turn messages selected by the
    transcript-window policy. Per the ownership rules above, turns
    inside the window are never content-truncated; if budget pressure
    requires trimming, the oldest turn is dropped whole.

    This type is a data contract, not a render contract. Prompt-facing
    transcript text is produced by the shared serializer in
    ``agent.graph.context.serialization``. Prompt builders and role
    nodes must not format ``turns`` entries themselves, and no parallel
    prompt-facing transcript data contract may be introduced alongside
    this one.

    Fields
    ------
    turns:
        Ordered list (oldest -> newest) of turn-shaped dicts. Exact
        per-turn schema is defined by the transcript module — this
        contract is intentionally permissive so the window authority
        owns the turn shape without forcing re-declarations here.
    target_turn_count:
        The configured target window size (e.g., 10). Included so
        projections can render diagnostics without re-reading policy.
    hard_minimum_turn_count:
        The policy's hard minimum (e.g., 5). Projections must respect
        this even when upstream shrinks the window.
    dropped_older_turn_count:
        Number of older turns that existed but were dropped whole to
        fit the window. Zero when the full history fit.
    """

    turns: list[dict[str, Any]]
    target_turn_count: int
    hard_minimum_turn_count: int
    dropped_older_turn_count: int


class RuntimeStateSnapshot(TypedDict):
    """Deterministic runtime/working-memory snapshot section.

    Structured, typed view of the task's current operational state.
    This section is explicitly machine-derived: it is **never**
    replaced by a natural-language summary, and no prompt consumer
    may treat ``trace.scratchpad`` or a compression output as an
    authoritative substitute.

    Fields
    ------
    active_target:
        The target currently in focus (host / URL / service handle)
        or ``None`` when no target is active.
    current_goal:
        Short structured description of the active goal.
    current_decision:
        The most recent structured decision record (what the agent
        chose to do and why), or ``None``.
    in_flight_tool:
        Descriptor of the tool currently executing (name + handle),
        or ``None`` when no tool is running.
    handles:
        Durable handles the agent is tracking (e.g., session IDs,
        artifact handles, persistent references). Kept as a mapping
        so new handle types can be added without contract churn.
    active_todo:
        Compact descriptor of the single in-progress plan todo the
        agent is currently resolving, or ``None`` when no plan is
        active. Shape: ``{"index": int, "description": str}``. Only
        the one IN_PROGRESS item is surfaced — the full plan and the
        other todos are intentionally omitted to keep prompt size
        down and match the "current step only" authority the tool
        selection layers need.
    """

    active_target: dict[str, Any] | None
    current_goal: dict[str, Any] | None
    current_decision: dict[str, Any] | None
    in_flight_tool: dict[str, Any] | None
    handles: dict[str, Any]
    active_todo: dict[str, Any] | None


class EvidenceRef(TypedDict):
    """Reference to a single piece of evidence.

    The bundle carries *references*, not payloads. Downstream roles
    resolve these through the evidence layer when they need content,
    which keeps prompt prefixes stable and cache-friendly.

    Fields
    ------
    evidence_id:
        Stable identifier for the evidence record in its canonical
        store.
    kind:
        Category label (e.g., ``"finding"``, ``"artifact"``,
        ``"observation"``). Consumers may filter by this without
        resolving the payload.
    summary:
        Short, human-readable summary suitable for inclusion in a
        role projection without pulling the full payload. Kept
        minimal — full content stays in the evidence layer.
    source:
        Provenance marker (e.g., tool name / node that produced the
        evidence). Used for debugging and grouping, not authority.
    """

    evidence_id: str
    kind: str
    summary: str
    source: str


class PriorTurnReferences(TypedDict):
    """Runtime-materialized prior-turn reference context.

    This section is populated after the classifier emits resolver hints
    and runtime validates them against canonical ``ChatMessage`` rows.
    Prompt-facing serializers may render ``materialized_turns`` text;
    they must not render classifier-only anchor text as canonical
    transcript content.
    """

    operation: str
    status: str
    materialized_turns: list[dict[str, Any]]
    unresolved_hints: list[dict[str, Any]]


class ConversationContextBundle(TypedDict):
    """Canonical hot-path memory envelope assembled once per turn.

    Every prompt-authoritative role reads either this bundle directly
    or a projection of it. No parallel prompt-authoritative memory
    contract may be introduced elsewhere in the codebase.

    Fields
    ------
    conversation_id:
        Stable identifier for the enclosing conversation / task.
    turn_id:
        Identifier of the turn this bundle was built for.
    turn_sequence:
        Monotonic zero-based index of the turn within the
        conversation. Useful for projections that want stable
        ordering or diagnostics.
    transcript_window:
        Recent-turn transcript section. See ``TranscriptWindow``.
        ``turns`` inside the window are **prior** turns only — the
        in-flight user message is carried separately in
        ``current_user_turn``.
    classifier_transcript_window:
        Required full-or-compacted projection consumed only by the intent
        classifier. Other roles continue to read ``transcript_window``.
    runtime_state:
        Deterministic runtime-state section. See ``RuntimeStateSnapshot``.
    evidence_refs:
        Ordered list of evidence references available to the current
        turn's roles. References only — payloads live in the evidence
        layer.
    prior_turn_references:
        Runtime-materialized prior-turn reference context. Defaults to
        an empty ``none`` shape. This is the only prompt-authoritative
        place for referenced prior-turn transcript text; consumers must
        not read ad-hoc transcript evidence from top-level metadata.
    current_user_turn:
        The in-flight user message as an OpenAI-style
        ``{"role": "user", "content": ...}`` dict, or ``None`` when
        no user message is being processed (e.g., pre-turn assembly
        with no message yet). This is the single authority for the
        current turn across every consumer — text-rendering prompts
        read it via the serializer (opt-in), and simple_chat reads
        it directly when building the chat-API message list.
    retrieved_prior_context:
        **Reserved; intentionally empty in this migration.** Future
        long-term retrieval work will populate this list with
        retrieval-tier items. Hot-path consumers must not populate
        or read from it in this scope; it exists in the contract now
        so the bundle shape does not need to change when retrieval
        lands.
    """

    conversation_id: str
    turn_id: str
    turn_sequence: int
    transcript_window: TranscriptWindow
    classifier_transcript_window: TranscriptWindow
    runtime_state: RuntimeStateSnapshot
    evidence_refs: list[EvidenceRef]
    prior_turn_references: PriorTurnReferences
    current_user_turn: dict[str, Any] | None
    retrieved_prior_context: list[dict[str, Any]]


__all__ = [
    "CLASSIFIER_TRANSCRIPT_WINDOW_KEY",
    "ConversationContextBundle",
    "EvidenceRef",
    "PriorTurnReferences",
    "RuntimeStateSnapshot",
    "TranscriptWindow",
]
