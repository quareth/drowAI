"""Shared prompt-section serializer for the hot-path conversation bundle.

This module is the *one* prompt-facing transcript rendering authority
for the LangGraph hot path. It exists so that transcript rendering,
runtime-state rendering, and section ordering do not continue to grow
inside ``agent/graph/context/projections.py`` (which owns role field
selection only) and so that prompt builders never have to rebuild
transcript text from raw message lists.

Responsibility boundary
-----------------------
- ``transcript.py``  : single authority for turn selection and
  turn grouping. It never renders prompt text.
- ``projections.py`` : single authority for *which* bundle fields a
  role receives. It never shapes transcript text.
- ``serialization.py`` (this module): single authority for *how*
  recent-transcript text and prompt sections are rendered for the
  LLM. Every prompt-authoritative hot-path role (intent classifier,
  category selector, planner, articulation, finalizer) reads the
  byte-identical recent-transcript text produced here.
- ``core/prompts/builders/*`` : template interpolation only. They
  accept already-rendered transcript text; they must never call
  back into turn-grouping or per-message text shaping.

Rendering invariants
--------------------
The recent-transcript block obeys these rules (cache-friendliness and
multiline safety depend on all of them holding simultaneously):

1. Each message is wrapped in a bounded ``<turn n=N role=R>…</turn>``
   block. Open and close tags live on their own line; the message
   body sits between them verbatim. Turn boundaries are therefore
   explicit even when a body contains the literal string ``User:``
   or other role-looking prefixes.
2. The ``n`` attribute is the absolute turn index derived from
   ``TranscriptWindow.dropped_older_turn_count``: the first user
   message in the window is ``n = dropped_older_turn_count + 1``
   and every non-user message that follows shares that ``n`` until
   the next user message. Messages that appear before the first
   user message in the window (the leading segment, typically a
   system summary) use ``n = dropped_older_turn_count``.
3. Adjacent turn blocks are separated by exactly one blank line.
4. Message bodies are preserved verbatim — no indentation, wrapping,
   truncation, or rewriting.
5. No timestamps, message IDs, hashes, or window-relative metadata
   leak into the rendered block.
6. The same input always produces byte-identical output so provider
   prompt prefixes stay cache-hot.
7. The final block of the in-flight turn (whenever the projection's
   ``current_user_turn`` field is a populated message dict) carries a
   ``latest=true`` attribute so downstream LLMs can anchor "act on
   this turn" without guessing. Rendering is presence-based — there
   is no opt-in flag: if the projection carries the turn, it renders.

Section ordering emitted by ``serialize_projection_to_prompt_sections``
is likewise part of the cache contract and must not shift casually:

1. ``recent_transcript`` (also carries the in-flight turn as the final
   ``<turn … latest=true>…</turn>`` block when the projection carries
   a populated ``current_user_turn``)
2. ``runtime_state``
3. ``evidence_refs``
4. ``working_memory`` (emitted only when the projection carries a
   non-empty ``working_memory_summary``)
5. ``referenced_prior_turns`` (emitted only when the projection carries
   materialized canonical prior-turn references)

The in-flight turn is part of the one conversation stream rendered
inside ``recent_transcript`` so consumers never have to stitch two
sources back together — it is not a separately-addressable section.

Working-memory summaries used to be appended to the rendered transcript
text inside prompt builders. They are now their own section so the
single-authority rendering invariant holds across every kind of prompt-
facing content: the serializer is the one place that knows how to
produce this block.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.graph.context.contracts import TranscriptWindow


# -- Stable section-name constants. ------------------------------------

SECTION_RECENT_TRANSCRIPT = "recent_transcript"
SECTION_RUNTIME_STATE = "runtime_state"
SECTION_EVIDENCE_REFS = "evidence_refs"
SECTION_WORKING_MEMORY = "working_memory"
SECTION_REFERENCED_PRIOR_TURNS = "referenced_prior_turns"

_SECTION_ORDER: tuple[str, ...] = (
    SECTION_RECENT_TRANSCRIPT,
    SECTION_RUNTIME_STATE,
    SECTION_EVIDENCE_REFS,
    SECTION_WORKING_MEMORY,
    SECTION_REFERENCED_PRIOR_TURNS,
)

# Maximum characters retained for a memory-summary section. Matches the
# truncation used by the legacy ``_append_*_context`` helpers so the
# rendered bodies stay byte-identical across the migration.
_MEMORY_SUMMARY_MAX_CHARS = 1200
"""Fixed section order used by the serializer.

This tuple is the *one* ordering authority. Roles must not reorder or
insert sections — new section kinds (if ever needed) should be appended
here so existing cache prefixes remain stable.

The in-flight user turn is not its own section: when a projection
carries a populated ``current_user_turn`` field, it surfaces as the
final ``<turn … latest=true>…</turn>`` block inside
``recent_transcript``. Rendering is presence-based — no opt-in flag.
"""


# -- Turn-block format constants. --------------------------------------

# The open/close tag name and attribute names for rendered turn blocks.
# Exposed at module scope so tests and any format-aware downstream
# validators can reference a single symbol instead of re-literalising
# ``"<turn "`` strings everywhere.

TURN_TAG_NAME = "turn"
TURN_ATTR_INDEX = "n"
TURN_ATTR_ROLE = "role"
TURN_ATTR_LATEST = "latest"


# Known role names emitted in the ``role`` attribute. The set is
# intentionally small and stable — unrecognised roles are passed through
# verbatim (lowercased) so new upstream roles render without silently
# breaking the contract, while still fitting the ``role=<value>`` form.

_CANONICAL_ROLE_TAGS: frozenset[str] = frozenset(
    {"user", "assistant", "tool", "system"}
)


def _message_content_text(message: Mapping[str, Any]) -> str:
    """Return the message body as verbatim text, preserving newlines.

    Non-string content (rare: structured assistant payloads) is
    stringified with ``str`` rather than reshaped so rendering stays
    deterministic even in unexpected shapes. ``None`` becomes the empty
    string so the block still has a clearly bounded body.
    """
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _role_tag_value(message: Mapping[str, Any]) -> str:
    """Return the ``role`` attribute value for a rendered turn block.

    Normalises the raw role with ``strip().lower()`` so whitespace and
    casing differences in upstream persistence cannot introduce cache-
    breaking churn. Canonical roles (user / assistant / tool / system)
    pass through as-is; unknown roles fall back to the normalised value
    or ``"unknown"`` when the message has no role at all.
    """
    raw_role = (message.get("role") or "").strip().lower()
    if raw_role in _CANONICAL_ROLE_TAGS:
        return raw_role
    return raw_role or "unknown"


def _render_turn_block(
    index: int,
    role: str,
    body: str,
    *,
    latest: bool = False,
) -> str:
    """Render one bounded ``<turn …>…</turn>`` block.

    Open and close tags live on their own line; the body sits verbatim
    between them. Empty bodies produce an open tag immediately followed
    by the close tag on the next line so the block stays visually
    bounded but does not accrete extra blank lines inside.

    Attribute values are written unquoted (``role=user``) so the
    ``role`` string must not contain whitespace. ``_role_tag_value``
    is the one normaliser — it strips and lowercases the raw value
    so upstream casing / whitespace never reaches this helper.
    """
    attrs = f"{TURN_ATTR_INDEX}={index} {TURN_ATTR_ROLE}={role}"
    if latest:
        attrs += f" {TURN_ATTR_LATEST}=true"
    open_tag = f"<{TURN_TAG_NAME} {attrs}>"
    close_tag = f"</{TURN_TAG_NAME}>"
    if body:
        return f"{open_tag}\n{body}\n{close_tag}"
    return f"{open_tag}\n{close_tag}"


def render_recent_transcript(
    transcript_window: TranscriptWindow,
    *,
    current_user_turn: Mapping[str, Any] | None = None,
) -> str:
    """Render the recent-transcript section as bounded, numbered turn blocks.

    Each message in ``transcript_window["turns"]`` becomes one
    ``<turn n=N role=R>…</turn>`` block. The ``n`` attribute is the
    absolute turn index: the first user message in the window is
    ``dropped_older_turn_count + 1``, and every non-user message that
    follows shares that index until the next user message. Messages
    that appear before the first user message in the window (the
    leading segment) use ``dropped_older_turn_count`` so they still
    carry a stable index without being counted as a turn.

    When ``current_user_turn`` is provided, it is appended as the final
    block, tagged ``latest=true``, and its ``n`` is the next absolute
    index after the last user message already present in the window.
    Callers pass this through only when the projection opts into
    including the in-flight turn inside the recent-transcript section;
    the default (``None``) preserves the legacy "prior turns only"
    behaviour and is byte-identical to the format before the current-
    turn unification.

    The function is pure and deterministic: same input -> byte-identical
    output. It performs no turn grouping on its own — turn boundaries
    are fixed upstream by
    ``agent.graph.context.transcript.split_transcript_into_turn_groups``
    / ``select_recent_transcript_window``, and this renderer consumes
    the already-windowed message list verbatim.

    Parameters
    ----------
    transcript_window:
        A ``TranscriptWindow`` whose ``turns`` field is the ordered
        list of message dicts selected by the transcript-window policy.
    current_user_turn:
        Optional in-flight message dict ``{role, content}``. When
        provided, appended as the final block with ``latest=true``.

    Returns
    -------
    str
        Multiline string of ``<turn …>…</turn>`` blocks separated by a
        single blank line. Empty string when the window has no turns
        and no current-turn is provided.
    """
    messages = transcript_window.get("turns") or []
    dropped = int(transcript_window.get("dropped_older_turn_count") or 0)
    blocks: list[str] = []
    turn_index = dropped
    for message in messages:
        if (message.get("role") or "").strip().lower() == "user":
            turn_index += 1
        role = _role_tag_value(message)
        body = _message_content_text(message)
        blocks.append(_render_turn_block(turn_index, role, body))

    if current_user_turn is not None:
        turn_index += 1
        role = _role_tag_value(current_user_turn)
        body = _message_content_text(current_user_turn)
        blocks.append(
            _render_turn_block(turn_index, role, body, latest=True)
        )

    return "\n\n".join(blocks)


# -- Internal section renderers. ---------------------------------------


def _render_runtime_state_section(projection: Mapping[str, Any]) -> str:
    """Render the runtime-state section as stable ``key: value`` lines.

    Key order follows the slot-name tuple each projection advertises in
    its ``_runtime_slot_order`` entry so output is deterministic without
    depending on Python dict ordering of arbitrary inputs.
    """
    slot_order: tuple[str, ...] = projection.get("_runtime_slot_order", ())
    runtime_state: dict[str, Any] = projection.get("runtime_state", {})
    lines: list[str] = []
    for slot_name in slot_order:
        if slot_name not in runtime_state:
            continue
        value = runtime_state[slot_name]
        lines.append(f"{slot_name}: {value}")
    return "\n".join(lines)


def _render_evidence_refs_section(projection: Mapping[str, Any]) -> str:
    """Render evidence refs as compact ``kind:id summary`` lines."""
    refs = projection.get("evidence_refs") or []
    lines: list[str] = []
    for ref in refs:
        kind = ref.get("kind", "")
        evidence_id = ref.get("evidence_id", "")
        summary = ref.get("summary", "")
        lines.append(f"{kind}:{evidence_id} {summary}")
    return "\n".join(lines)


def _render_memory_summary_section(
    summary: Any,
    *,
    label: str,
) -> str:
    """Render a labelled, bounded memory-summary block.

    Returns the empty string when ``summary`` is missing or blank after
    truncation so the serializer can skip the section entirely — the
    label is part of the rendered content so the block is
    self-describing when it appears.
    """
    if not summary:
        return ""
    text = str(summary)[:_MEMORY_SUMMARY_MAX_CHARS]
    if not text.strip():
        return ""
    return f"{label}\n{text}"


def render_working_memory_section(projection: Mapping[str, Any]) -> str:
    """Public renderer for the ``working_memory`` prompt section.

    Exposed so tests and future non-prompt consumers can obtain the
    byte-identical rendered block without going through the ordered
    section serializer.
    """
    return _render_memory_summary_section(
        projection.get("working_memory_summary"),
        label="Working Memory Snapshot:",
    )


def render_referenced_prior_turns_section(projection: Mapping[str, Any]) -> str:
    """Render canonical materialized prior turns for prompt consumption."""
    references = projection.get("prior_turn_references")
    if not isinstance(references, Mapping):
        return ""
    materialized_turns = references.get("materialized_turns")
    if not isinstance(materialized_turns, list) or not materialized_turns:
        return ""

    blocks: list[str] = []
    for item in materialized_turns:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        rendered_turn = item.get("rendered_turn_number")
        if isinstance(rendered_turn, int) and not isinstance(rendered_turn, bool):
            turn_index = rendered_turn
        else:
            turn_number = item.get("turn_number")
            turn_index = (
                turn_number
                if isinstance(turn_number, int) and not isinstance(turn_number, bool)
                else 0
            )
        role = _role_tag_value({"role": item.get("speaker")})
        blocks.append(_render_turn_block(turn_index, role, text))
    if not blocks:
        return ""
    return "Referenced Prior Turns:\n" + "\n\n".join(blocks)


# -- Public serializer. ------------------------------------------------


def serialize_projection_to_prompt_sections(
    projection: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Serialize a projection into an ordered list of prompt sections.

    Emits exactly the sections in ``_SECTION_ORDER`` that are relevant
    to the projection, as ``[{"name": <section>, "content": <text>},
    ...]``. The output is deterministic: same projection -> byte-
    identical list, preserving provider-side cache prefixes.

    Section rendering rules
    -----------------------
    - ``recent_transcript``: always emitted when the projection carries
      a ``transcript_window`` (even if the window has zero turns, in
      which case the content is the empty string). Rendering uses the
      bounded turn-block format from ``render_recent_transcript``.
      When the projection carries a populated ``current_user_turn``
      message dict, the in-flight turn is appended as the final block
      with ``latest=true`` — no separate section is emitted for it and
      no opt-in flag is consulted (presence is the signal).
    - ``runtime_state``: emitted when the projection carries a
      ``runtime_state`` mapping. Empty mapping renders as an empty
      content string; this keeps the section count stable across turns
      for cache-prefix friendliness.
    - ``evidence_refs``: emitted only for projections that include the
      ``evidence_refs`` key (i.e., planner / articulation). When the
      list is empty, an empty-content section is still emitted so the
      prefix count stays stable for those roles.
    - ``working_memory``: emitted only when the projection carries a
      non-empty ``working_memory_summary`` (suppressed-on-empty, so the
      section count stays minimal for roles that do not use memory).
      Rendered as a labelled block via ``render_working_memory_section``.
    - ``referenced_prior_turns``: emitted only when the projection
      carries materialized prior-turn references. The section renders
      canonical row text only, never classifier anchor text.

    Parameters
    ----------
    projection:
        A projection mapping produced by one of the ``project_for_*``
        helpers in ``agent.graph.context.projections``.

    Returns
    -------
    list[dict[str, str]]
        Ordered list of section blocks. Each block has a ``name`` drawn
        from ``_SECTION_ORDER`` and a string ``content``. Content is
        never truncated.
    """
    sections: list[dict[str, str]] = []

    for section_name in _SECTION_ORDER:
        if section_name == SECTION_RECENT_TRANSCRIPT:
            if "transcript_window" in projection:
                current_turn = projection.get("current_user_turn")
                sections.append(
                    {
                        "name": SECTION_RECENT_TRANSCRIPT,
                        "content": render_recent_transcript(
                            projection["transcript_window"],
                            current_user_turn=current_turn,
                        ),
                    }
                )
            continue

        if section_name == SECTION_RUNTIME_STATE:
            if "runtime_state" in projection:
                sections.append(
                    {
                        "name": SECTION_RUNTIME_STATE,
                        "content": _render_runtime_state_section(projection),
                    }
                )
            continue

        if section_name == SECTION_EVIDENCE_REFS:
            if "evidence_refs" in projection:
                sections.append(
                    {
                        "name": SECTION_EVIDENCE_REFS,
                        "content": _render_evidence_refs_section(projection),
                    }
                )
            continue

        if section_name == SECTION_WORKING_MEMORY:
            content = render_working_memory_section(projection)
            if content:
                sections.append(
                    {"name": SECTION_WORKING_MEMORY, "content": content}
                )
            continue

        if section_name == SECTION_REFERENCED_PRIOR_TURNS:
            content = render_referenced_prior_turns_section(projection)
            if content:
                sections.append(
                    {
                        "name": SECTION_REFERENCED_PRIOR_TURNS,
                        "content": content,
                    }
                )
            continue

    return sections


def serialize_projection_to_section_map(
    projection: Mapping[str, Any],
) -> dict[str, str]:
    """Return serialized section content keyed by section name.

    Thin convenience wrapper around
    ``serialize_projection_to_prompt_sections`` for consumers that only
    need to look up a single section by name (``recent_transcript``,
    ``runtime_state``, etc.) without repeating the loop-over-sections
    pattern in every role node.

    This helper does not bypass the ordered serializer — it calls it and
    collapses the result into a mapping — so section rendering stays a
    single authoritative path.
    """
    return {
        section["name"]: section["content"]
        for section in serialize_projection_to_prompt_sections(projection)
    }


__all__ = [
    "SECTION_EVIDENCE_REFS",
    "SECTION_REFERENCED_PRIOR_TURNS",
    "SECTION_RECENT_TRANSCRIPT",
    "SECTION_RUNTIME_STATE",
    "SECTION_WORKING_MEMORY",
    "TURN_ATTR_INDEX",
    "TURN_ATTR_LATEST",
    "TURN_ATTR_ROLE",
    "TURN_TAG_NAME",
    "render_recent_transcript",
    "render_referenced_prior_turns_section",
    "render_working_memory_section",
    "serialize_projection_to_prompt_sections",
    "serialize_projection_to_section_map",
]
