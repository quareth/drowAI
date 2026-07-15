"""Unit tests for the transcript-window policy authority.

Exercises ``select_recent_transcript_window`` and the advertised
policy constants in ``agent.graph.context.transcript``. These tests
lock in the Phase 1 guarantees that matter for prompt continuity:

- Recent turns inside the selected window are preserved verbatim (no
  per-message content truncation).
- Over-budget selection drops oldest turns **whole** and reports the
  drop count exactly.
- Leading non-user segment (e.g., a system summary marker) is pinned
  to the head of the window and is not treated as a turn.
- ``min_turns`` is a floor for *selection*, never a synthesizer of
  missing history.
- Empty input is well-formed.
- Invariant ``max_turns >= min_turns`` is enforced.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.graph.context.transcript import (
    HARD_MINIMUM_RECENT_TURNS,
    TARGET_RECENT_TURNS,
    select_full_transcript_window,
    select_recent_transcript_window,
    split_transcript_into_turn_groups,
)


def _turn_messages(index: int) -> list[dict[str, Any]]:
    """Build a 3-message turn for test input: user + assistant + tool.

    Content strings are intentionally long so any per-turn truncation
    would show up as a length mismatch in assertions.
    """
    long_user = f"user question {index} " + ("x" * 512)
    long_assistant = f"assistant answer {index} " + ("y" * 512)
    long_tool = f"tool output {index} " + ("z" * 512)
    return [
        {"role": "user", "content": long_user},
        {"role": "assistant", "content": long_assistant},
        {"role": "tool", "content": long_tool, "tool_call_id": f"t{index}"},
    ]


def _build_messages(turn_count: int) -> list[dict[str, Any]]:
    """Flatten ``turn_count`` sequential turns into an OpenAI-style list."""
    messages: list[dict[str, Any]] = []
    for i in range(turn_count):
        messages.extend(_turn_messages(i))
    return messages


def test_policy_constants_have_expected_values() -> None:
    assert TARGET_RECENT_TURNS == 10
    assert HARD_MINIMUM_RECENT_TURNS == 5


def test_selects_target_recent_turns_verbatim_when_more_are_provided() -> None:
    # 15 turns provided, target is 10 — expect the last 10 kept verbatim,
    # with 5 dropped from the head.
    messages = _build_messages(turn_count=15)

    window = select_recent_transcript_window(messages)

    assert window["target_turn_count"] == TARGET_RECENT_TURNS
    assert window["hard_minimum_turn_count"] == HARD_MINIMUM_RECENT_TURNS
    assert window["dropped_older_turn_count"] == 5

    # 10 turns * 3 messages per turn = 30 verbatim messages in the window.
    assert len(window["turns"]) == 10 * 3

    # The first user message in the window must be turn index 5 (0-based),
    # confirming the oldest 5 turns were dropped whole.
    first_user = next(m for m in window["turns"] if m.get("role") == "user")
    assert first_user["content"].startswith("user question 5 ")

    # Every kept message is byte-identical to the original input — no
    # per-turn content truncation.
    kept = messages[5 * 3 :]
    assert window["turns"] == kept


def test_drops_oldest_turns_whole_and_reports_drop_count() -> None:
    # 12 turns -> drop the 2 oldest whole, keep 10.
    messages = _build_messages(turn_count=12)

    window = select_recent_transcript_window(messages)

    assert window["dropped_older_turn_count"] == 2
    assert len(window["turns"]) == 10 * 3

    # Each dropped turn is removed entirely — no partial leakage.
    for dropped_index in (0, 1):
        dropped_marker = f"user question {dropped_index} "
        assert all(
            dropped_marker not in (m.get("content") or "")
            for m in window["turns"]
        )


def test_preserves_leading_non_user_segment_at_head_of_window() -> None:
    summary_marker = {
        "role": "system",
        "content": "[summary of prior conversation]",
    }
    messages: list[dict[str, Any]] = [summary_marker]
    messages.extend(_build_messages(turn_count=12))

    window = select_recent_transcript_window(messages)

    # Leading segment is pinned to the head and is not a turn.
    assert window["turns"][0] == summary_marker

    # Drop count still reflects turns only (12 turns - 10 kept = 2).
    assert window["dropped_older_turn_count"] == 2

    # Selected-turn payload size matches 10 turns * 3 messages, plus the
    # leading segment anchor message.
    assert len(window["turns"]) == 1 + 10 * 3


def test_fewer_turns_than_hard_minimum_returns_all_turns_without_synthesis() -> None:
    # 3 turns is below HARD_MINIMUM_RECENT_TURNS (5) — policy is a floor
    # for *selection*, not a synthesizer of missing history.
    messages = _build_messages(turn_count=3)

    window = select_recent_transcript_window(messages)

    assert window["dropped_older_turn_count"] == 0
    assert len(window["turns"]) == 3 * 3
    assert window["turns"] == messages
    # Policy sizes surface unchanged so projections can diagnose without
    # re-reading policy.
    assert window["target_turn_count"] == TARGET_RECENT_TURNS
    assert window["hard_minimum_turn_count"] == HARD_MINIMUM_RECENT_TURNS


def test_empty_input_returns_well_formed_empty_window() -> None:
    window = select_recent_transcript_window([])

    assert window["turns"] == []
    assert window["dropped_older_turn_count"] == 0
    assert window["target_turn_count"] == TARGET_RECENT_TURNS
    assert window["hard_minimum_turn_count"] == HARD_MINIMUM_RECENT_TURNS


def test_full_window_reuses_policy_shape_without_dropping_canonical_turns() -> None:
    messages = _build_messages(turn_count=12)

    window = select_full_transcript_window(messages)

    assert window["turns"] == messages
    assert window["target_turn_count"] == 12
    assert window["hard_minimum_turn_count"] == HARD_MINIMUM_RECENT_TURNS
    assert window["dropped_older_turn_count"] == 0


def test_raises_when_max_turns_less_than_min_turns() -> None:
    with pytest.raises(ValueError):
        select_recent_transcript_window(
            _build_messages(turn_count=3),
            max_turns=2,
            min_turns=5,
        )


def test_split_transcript_into_turn_groups_returns_head_segment_and_turns() -> None:
    summary_marker = {
        "role": "system",
        "content": "[summary of prior conversation]",
    }
    messages: list[dict[str, Any]] = [summary_marker]
    messages.extend(_build_messages(turn_count=3))

    leading_segment, turns = split_transcript_into_turn_groups(messages)

    # Leading non-user message (e.g., system summary) is pinned to the head
    # and never treated as a turn.
    assert leading_segment == [summary_marker]
    assert len(turns) == 3

    # Each turn is a non-empty list whose first message is the user message.
    for index, turn in enumerate(turns):
        assert turn[0]["role"] == "user"
        assert turn[0]["content"].startswith(f"user question {index} ")
        # Every turn here contains the user + assistant + tool messages built
        # by ``_turn_messages`` — grouping boundary is "up to the next user".
        assert [message["role"] for message in turn] == [
            "user",
            "assistant",
            "tool",
        ]


def test_split_transcript_groups_multiple_ptr_tool_iterations_into_one_conversation_turn() -> None:
    """PTR iterations stay inside the user run that owns them."""
    messages = [
        {"role": "user", "content": "scan the target"},
        {"role": "assistant", "content": "reasoning iteration 1"},
        {"role": "tool", "content": "tool result 1", "tool_call_id": "call-1"},
        {"role": "assistant", "content": "reasoning iteration 2"},
        {"role": "tool", "content": "tool result 2", "tool_call_id": "call-2"},
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "follow up"},
        {"role": "assistant", "content": "follow-up answer"},
    ]

    leading_segment, turns = split_transcript_into_turn_groups(messages)

    assert leading_segment == []
    assert len(turns) == 2
    assert turns[0] == messages[:6]
    assert [entry["role"] for entry in turns[0]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert turns[1] == messages[6:]


def test_split_transcript_into_turn_groups_handles_transcript_starting_with_user() -> None:
    messages = _build_messages(turn_count=2)

    leading_segment, turns = split_transcript_into_turn_groups(messages)

    # No leading non-user messages -> leading segment is empty.
    assert leading_segment == []
    assert len(turns) == 2
    assert turns[0][0]["role"] == "user"


def test_split_transcript_into_turn_groups_handles_empty_input() -> None:
    leading_segment, turns = split_transcript_into_turn_groups([])

    assert leading_segment == []
    assert turns == []


def test_select_recent_transcript_window_uses_public_turn_grouping_helper() -> None:
    # Locks in the contract that the window selector relies on the *same*
    # grouping semantics as the public helper, so no serializer or prompt
    # builder has a reason to re-split messages independently.
    summary_marker = {
        "role": "system",
        "content": "[summary of prior conversation]",
    }
    messages: list[dict[str, Any]] = [summary_marker]
    messages.extend(_build_messages(turn_count=12))

    leading_segment, turns = split_transcript_into_turn_groups(messages)
    window = select_recent_transcript_window(messages)

    # 12 turns - target 10 = 2 dropped from head.
    assert window["dropped_older_turn_count"] == len(turns) - TARGET_RECENT_TURNS

    # The window's flattened messages equal: leading segment + last 10 turns.
    expected = list(leading_segment)
    for turn in turns[-TARGET_RECENT_TURNS:]:
        expected.extend(turn)
    assert window["turns"] == expected
