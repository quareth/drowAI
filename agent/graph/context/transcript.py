"""Single transcript-window policy authority for the hot-path bundle.

This module owns the *one* decision for "which recent transcript turns
belong in the conversation context bundle." It takes an already-loaded
canonical transcript (OpenAI-style message dicts, as returned by
``ConversationHistoryReader.build_openai_conversation_history``) and returns a
populated ``TranscriptWindow`` contract.

Policy (fixed, deterministic)
-----------------------------
- Target recent turns: ``TARGET_RECENT_TURNS = 10``.
- Hard minimum recent turns: ``HARD_MINIMUM_RECENT_TURNS = 5``.
- Messages inside the selected window are **verbatim** — no per-turn
  content truncation of any kind.
- If the window must shrink, **drop the oldest turn whole** rather than
  trimming any turn's content. ``dropped_older_turn_count`` reports how
  many turns were dropped from the head.

Turn-boundary definition
------------------------
A *turn* is a ``user`` message together with every non-user message
(``assistant``, ``tool``, and anything else produced by the agent) that
follows it up to (but not including) the next ``user`` message. Turns
therefore pair each user question with the assistant's full response
chain for that question, which is the natural continuity unit for the
bundle.

Any non-user messages that appear **before the first user message**
(typically a ``system`` summary marker emitted by the history loader)
are preserved verbatim as a leading segment attached to the head of the
selected window. They do not count as a turn and are never included in
``dropped_older_turn_count`` — their role is to anchor context for the
surviving turns, not to be selected or dropped as a turn.

Scope notes
-----------
- No token counting, no byte budget, and no content truncation helpers
  live here. Those are explicitly out of scope for this policy.
- This module imports contracts from ``agent.graph.context.contracts``
  and must not be imported *by* that module (one-way dependency to keep
  the authority graph acyclic).
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.contracts import TranscriptWindow


TARGET_RECENT_TURNS = 10
"""Target number of recent turns to include in the transcript window."""

HARD_MINIMUM_RECENT_TURNS = 5
"""Lower bound on the window size enforced by the policy."""


def split_transcript_into_turn_groups(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Split an OpenAI-style message list into a head segment and turns.

    This is the single public turn-grouping authority. The shared prompt
    serializer, transcript-window selection, and any other hot-path
    consumer that needs to iterate turn boundaries must call this
    helper instead of re-implementing the boundary rule.

    A *turn* is a ``user`` message together with every non-user message
    (``assistant``, ``tool``, etc.) that follows it up to (but not
    including) the next ``user`` message. Any non-user messages that
    appear before the first user message form the leading segment and
    are never treated as a turn.

    Returns
    -------
    (leading_segment, turns)
        ``leading_segment`` is the ordered list of messages that appear
        before the first ``user`` message (empty when the transcript
        begins with a user message). ``turns`` is an ordered list of
        turns, each a non-empty list of messages whose first entry has
        ``role == "user"``.
    """
    leading_segment: list[dict[str, Any]] = []
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None

    for message in messages:
        role = (message.get("role") or "").strip()
        if role == "user":
            if current is not None:
                turns.append(current)
            current = [message]
        else:
            if current is None:
                leading_segment.append(message)
            else:
                current.append(message)

    if current is not None:
        turns.append(current)

    return leading_segment, turns


def select_recent_transcript_window(
    messages: list[dict[str, Any]],
    *,
    max_turns: int = TARGET_RECENT_TURNS,
    min_turns: int = HARD_MINIMUM_RECENT_TURNS,
) -> TranscriptWindow:
    """Select the recent-turn window for the conversation context bundle.

    Deterministic policy implementation: split ``messages`` into turns,
    keep at most ``max_turns`` most-recent turns verbatim, and report
    how many older turns were dropped whole. No content inside a
    selected turn is ever modified or truncated.

    Parameters
    ----------
    messages:
        Canonical transcript as a list of OpenAI-style message dicts
        (role/content/optional tool_calls). Typically produced by
        ``ConversationHistoryReader.build_openai_conversation_history``.
    max_turns:
        Target window size (defaults to ``TARGET_RECENT_TURNS``). Must
        be >= ``min_turns``; callers almost never need to override it.
    min_turns:
        Hard minimum window size (defaults to
        ``HARD_MINIMUM_RECENT_TURNS``). Recorded in the returned
        ``TranscriptWindow`` so projections can respect it without
        re-reading policy. When the actual transcript has fewer turns
        than ``min_turns``, every available turn is kept — the minimum
        is a floor for *selection*, not a synthesizer of missing history.

    Returns
    -------
    TranscriptWindow
        Populated contract with verbatim turns (flattened back into a
        single ordered message list), policy sizes, and the count of
        older turns dropped from the head.
    """
    if max_turns < min_turns:
        raise ValueError(
            f"max_turns ({max_turns}) must be >= min_turns ({min_turns})"
        )

    leading_segment, turns = split_transcript_into_turn_groups(messages)

    total_turns = len(turns)
    keep_count = min(total_turns, max_turns)
    dropped_older_turn_count = total_turns - keep_count
    selected_turns = turns[dropped_older_turn_count:]

    window_messages: list[dict[str, Any]] = list(leading_segment)
    for turn in selected_turns:
        window_messages.extend(turn)

    return TranscriptWindow(
        turns=window_messages,
        target_turn_count=max_turns,
        hard_minimum_turn_count=min_turns,
        dropped_older_turn_count=dropped_older_turn_count,
    )


def select_full_transcript_window(
    messages: list[dict[str, Any]],
) -> TranscriptWindow:
    """Return the complete canonical transcript using the shared turn policy.

    The intent classifier owns the one full-or-compacted projection. Reusing
    ``select_recent_transcript_window`` keeps turn grouping and metadata shape
    identical while raising the per-call maximum enough to retain every turn.
    """
    _, turns = split_transcript_into_turn_groups(messages)
    return select_recent_transcript_window(
        messages,
        max_turns=max(TARGET_RECENT_TURNS, len(turns)),
    )


__all__ = [
    "HARD_MINIMUM_RECENT_TURNS",
    "TARGET_RECENT_TURNS",
    "select_full_transcript_window",
    "select_recent_transcript_window",
    "split_transcript_into_turn_groups",
]
