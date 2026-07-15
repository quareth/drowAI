"""Shared per-role projections for the hot-path ``ConversationContextBundle``.

This module is the *single* projection authority for the LangGraph
hot path. Every prompt-authoritative role (intent classifier, category
selector, planner, articulation/finalizer) reads a projection produced
here instead of rebuilding history or assembling prompt context
locally.

Responsibility boundary
-----------------------
``projections.py`` owns **which** bundle fields a role receives
(role-field selection). Prompt-facing text rendering of those fields
— turn-labelled transcript blocks, runtime-state lines, evidence-ref
lines, section ordering — is owned by
``agent.graph.context.serialization``. No node, prompt builder, or
other module may re-implement transcript or section rendering; they
consume the byte-identical output of the shared serializer.

What a projection is
--------------------
A *projection* is a deterministic, role-specific dict derived **only**
from a ``ConversationContextBundle``. Projections never re-fetch the
transcript, never reach into other metadata, and never synthesize
content. Every field they expose is a selected subset of the bundle's
already-authoritative sections.

Projection key contract
-----------------------
Every projection returned by this module uses the same stable key
vocabulary so consumers and the shared serializer can treat projections
uniformly. Keys are listed in the order they appear in the constructed
dict (the order itself is not load-bearing, but matching it keeps docs
and code aligned):

- ``role``: Stable role identifier (``"intent_classifier"``,
  ``"category_selector"``, ``"planner"``, ``"articulation"``).
- ``turn_identity``: ``{"conversation_id": ..., "turn_id": ...,
  "turn_sequence": ...}`` — carried through verbatim so diagnostics
  and cache-key derivation have a stable shape.
- ``transcript_window``: Verbatim ``TranscriptWindow`` contract from the
  bundle (not a copy, not a rewrite). Consumers must not mutate it.
- ``runtime_state``: Role-filtered mapping with a *fixed* set of slot
  names for that role. Slots not relevant to the role are simply
  omitted from the projection (they are not set to ``None``) — each
  projection's docstring below enumerates its slots.
- ``evidence_refs``: Ordered list of evidence refs (may be empty).
  Roles that do not consume evidence omit the key entirely.
- ``current_user_turn``: The in-flight user message as
  ``{"role": "user", "content": ...}`` or ``None``. Rendering is
  presence-based: when this field is a populated message dict, the
  shared serializer appends it as the final ``<turn … latest=true>``
  block inside ``recent_transcript``. When it is ``None``, no
  latest-tagged block is emitted. No separate opt-in flag exists.
  Consumers that want the prior-only view simply clear this key on
  the projection before serialising (or pass a bundle whose
  ``current_user_turn`` is ``None``).
- ``working_memory_summary`` (planner only, optional): Runtime-context
  memory blob rendered as its own ``working_memory`` section by the
  shared serializer. Presence-based and suppressed-on-empty —
  projections that pass empty strings or ``None`` omit the key and the
  serializer emits no section.
- ``prior_turn_references`` (planner / articulation only, optional):
  Runtime-materialized canonical prior-turn rows. Omitted for roles
  that should not consume referenced transcript context and suppressed
  when no materialized rows exist.

Serialization
-------------
The prompt-section serializer lives in
``agent.graph.context.serialization``. It emits projections as an
ordered list of ``{"name", "content"}`` blocks using the fixed section
order declared in that module. Ordering is intentionally stable to
preserve provider-side cache prefixes.

For backward compatibility during the migration, this module
re-exports the section-name constants and
``serialize_projection_to_prompt_sections`` from
``agent.graph.context.serialization``; new code should import them
from ``agent.graph.context.serialization`` directly.

Scope notes
-----------
- No transcript text shaping lives here. Rendering invariants
  (multiline safety, role headers, deterministic separators) are
  enforced by the shared serializer.
- No token counting, byte budgeting, or content truncation. Transcript
  messages and runtime-state values are surfaced verbatim; budget
  decisions live in the transcript-window policy.
- No prompt-builder logic, no model-specific shaping. Prompt builders
  consume pre-rendered section text; they do not reach into
  projection internals.
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.contracts import (
    CLASSIFIER_TRANSCRIPT_WINDOW_KEY,
    ConversationContextBundle,
)

# Re-export the section-name constants and the serializer from the
# shared serializer boundary so existing imports (``from
# agent.graph.context.projections import ...``) keep working during
# the migration window. The final cleanup pass (Phase 5) can drop
# these re-exports once all consumers import from serialization.py
# directly.
from agent.graph.context.serialization import (
    SECTION_EVIDENCE_REFS,
    SECTION_REFERENCED_PRIOR_TURNS,
    SECTION_RECENT_TRANSCRIPT,
    SECTION_RUNTIME_STATE,
    serialize_projection_to_prompt_sections,
)


# -- Role identifiers. --------------------------------------------------

ROLE_INTENT_CLASSIFIER = "intent_classifier"
ROLE_CATEGORY_SELECTOR = "category_selector"
ROLE_PLANNER = "planner"
ROLE_ARTICULATION = "articulation"


# -- Internal helpers. --------------------------------------------------


def _turn_identity(bundle: ConversationContextBundle) -> dict[str, Any]:
    """Return the stable turn-identity triple carried by every projection."""
    return {
        "conversation_id": bundle["conversation_id"],
        "turn_id": bundle["turn_id"],
        "turn_sequence": bundle["turn_sequence"],
    }


def _current_user_turn(
    bundle: ConversationContextBundle,
) -> dict[str, Any] | None:
    """Return the bundle's in-flight user turn or ``None``.

    Reads the single authority (``bundle["current_user_turn"]``) and
    tolerates bundles produced by legacy paths that have not yet
    populated the field — a missing key is treated as ``None`` rather
    than raising, so the migration window remains non-breaking.
    """
    return bundle.get("current_user_turn")


def _select_runtime_slots(
    bundle: ConversationContextBundle,
    slot_names: tuple[str, ...],
) -> dict[str, Any]:
    """Pick the named runtime-state slots from the bundle.

    Slots with a ``None`` value are omitted from the projection so the
    rendered prompt-section content is stable and compact for the
    common empty-state case. For mapping slots such as ``handles``, an
    empty dict is treated as empty and omitted as well — this keeps the
    serializer output free of placeholder noise without losing
    determinism.
    """
    runtime_state = bundle["runtime_state"]
    selected: dict[str, Any] = {}
    for slot_name in slot_names:
        value = runtime_state.get(slot_name)
        if value is None:
            continue
        if isinstance(value, dict) and not value:
            continue
        selected[slot_name] = value
    return selected


def _prior_turn_references_for_prompt(
    bundle: ConversationContextBundle,
) -> dict[str, Any] | None:
    """Return materialized prior-turn references only when prompt-useful."""
    references = bundle.get("prior_turn_references")
    if not isinstance(references, dict):
        return None
    materialized_turns = references.get("materialized_turns")
    if not isinstance(materialized_turns, list) or not materialized_turns:
        return None
    return references


# -- Public projection helpers. -----------------------------------------

# Each projection advertises, via the private ``_runtime_slot_order``
# key, the exact ordered tuple of runtime-state slots it considers.
# The serializer uses this tuple to keep key ordering stable regardless
# of the input bundle's dict iteration order.

_CLASSIFIER_RUNTIME_SLOTS: tuple[str, ...] = ("active_target", "current_goal")
_CATEGORY_SELECTOR_RUNTIME_SLOTS: tuple[str, ...] = (
    "active_target",
    "active_todo",
    "current_decision",
)
_PLANNER_RUNTIME_SLOTS: tuple[str, ...] = (
    "active_target",
    "active_todo",
    "current_goal",
    "in_flight_tool",
    "handles",
)
_ARTICULATION_RUNTIME_SLOTS: tuple[str, ...] = (
    "active_target",
    "active_todo",
    "current_decision",
)


def project_for_intent_classifier(
    bundle: ConversationContextBundle,
) -> dict[str, Any]:
    """Project the bundle for the intent classifier role.

    The classifier needs just enough to decide whether the current user
    turn continues, pivots, or refines prior intent. Per the guide, it
    gets:

    - ``transcript_window``: the required classifier-only full-or-compacted
      window, never the shared bounded role window.
    - ``runtime_state``: minimal slice (``active_target``,
      ``current_goal`` if present).

    It does *not* receive evidence refs — classifier continuity should
    not hinge on evidence payloads.
    """
    return {
        "role": ROLE_INTENT_CLASSIFIER,
        "turn_identity": _turn_identity(bundle),
        "transcript_window": bundle[CLASSIFIER_TRANSCRIPT_WINDOW_KEY],
        "runtime_state": _select_runtime_slots(
            bundle, _CLASSIFIER_RUNTIME_SLOTS
        ),
        "_runtime_slot_order": _CLASSIFIER_RUNTIME_SLOTS,
        "current_user_turn": _current_user_turn(bundle),
    }


def project_for_category_selector(
    bundle: ConversationContextBundle,
) -> dict[str, Any]:
    """Project the bundle for the tool-category selector role.

    The category selector needs the recent transcript plus the latest
    structured decision so it can pick the correct tool family without
    rebuilding history. It gets:

    - ``transcript_window``: verbatim from the bundle.
    - ``runtime_state``: ``active_target`` and ``current_decision``
      when present.

    No evidence refs — category selection is a routing step, not a
    synthesis step.
    """
    return {
        "role": ROLE_CATEGORY_SELECTOR,
        "turn_identity": _turn_identity(bundle),
        "transcript_window": bundle["transcript_window"],
        "runtime_state": _select_runtime_slots(
            bundle, _CATEGORY_SELECTOR_RUNTIME_SLOTS
        ),
        "_runtime_slot_order": _CATEGORY_SELECTOR_RUNTIME_SLOTS,
        "current_user_turn": _current_user_turn(bundle),
    }


def project_for_planner(
    bundle: ConversationContextBundle,
    *,
    working_memory_summary: str | None = None,
) -> dict[str, Any]:
    """Project the bundle for the planner / tool-selection role.

    The planner needs a broader runtime-state slice because it decides
    what to execute next. It gets:

    - ``transcript_window``: verbatim from the bundle.
    - ``runtime_state``: ``active_target``, ``current_goal``,
      ``in_flight_tool``, and ``handles`` (all filtered to non-empty).
    - ``evidence_refs``: verbatim ordered list from the bundle.
    - ``working_memory_summary``: the optional runtime memory-summary
      blob (runtime-context state, not bundle data). Passed through
      verbatim so the shared serializer can emit it as its own prompt
      section when populated. This is the one authority for memory-
      summary rendering — builders must not append it to transcript
      text themselves.
    """
    projection: dict[str, Any] = {
        "role": ROLE_PLANNER,
        "turn_identity": _turn_identity(bundle),
        "transcript_window": bundle["transcript_window"],
        "runtime_state": _select_runtime_slots(
            bundle, _PLANNER_RUNTIME_SLOTS
        ),
        "evidence_refs": list(bundle["evidence_refs"]),
        "_runtime_slot_order": _PLANNER_RUNTIME_SLOTS,
        "current_user_turn": _current_user_turn(bundle),
    }
    prior_turn_references = _prior_turn_references_for_prompt(bundle)
    if prior_turn_references:
        projection["prior_turn_references"] = prior_turn_references
    if working_memory_summary:
        projection["working_memory_summary"] = working_memory_summary
    return projection


def project_for_articulation(
    bundle: ConversationContextBundle,
) -> dict[str, Any]:
    """Project the bundle for the articulation / finalizer role.

    Articulation produces the user-facing answer. It gets:

    - ``transcript_window``: verbatim from the bundle.
    - ``runtime_state``: ``active_target`` and ``current_decision``
      (so the reply is grounded in what was just decided / executed).
    - ``evidence_refs``: verbatim ordered list from the bundle.
    """
    projection: dict[str, Any] = {
        "role": ROLE_ARTICULATION,
        "turn_identity": _turn_identity(bundle),
        "transcript_window": bundle["transcript_window"],
        "runtime_state": _select_runtime_slots(
            bundle, _ARTICULATION_RUNTIME_SLOTS
        ),
        "evidence_refs": list(bundle["evidence_refs"]),
        "_runtime_slot_order": _ARTICULATION_RUNTIME_SLOTS,
        "current_user_turn": _current_user_turn(bundle),
    }
    prior_turn_references = _prior_turn_references_for_prompt(bundle)
    if prior_turn_references:
        projection["prior_turn_references"] = prior_turn_references
    return projection


__all__ = [
    "ROLE_ARTICULATION",
    "ROLE_CATEGORY_SELECTOR",
    "ROLE_INTENT_CLASSIFIER",
    "ROLE_PLANNER",
    "SECTION_EVIDENCE_REFS",
    "SECTION_REFERENCED_PRIOR_TURNS",
    "SECTION_RECENT_TRANSCRIPT",
    "SECTION_RUNTIME_STATE",
    "project_for_articulation",
    "project_for_category_selector",
    "project_for_intent_classifier",
    "project_for_planner",
    "serialize_projection_to_prompt_sections",
]
