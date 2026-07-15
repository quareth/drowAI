"""Unit tests for the shared recent-transcript renderer.

Locks in the multiline-safety, ordering, and cache-friendliness
invariants of
``agent.graph.context.serialization.render_recent_transcript``:

- Each message is wrapped in a bounded
  ``<turn n=N role=R>…</turn>`` block with open/close tags on their
  own line — turn boundaries are explicit even when a body contains
  the literal string ``User:``.
- ``n`` is the absolute turn index derived from
  ``TranscriptWindow.dropped_older_turn_count``. The first user
  message in the window is ``dropped_older_turn_count + 1``; every
  non-user message that follows shares that index until the next
  user message.
- Adjacent blocks are separated by exactly one blank line.
- Message bodies are verbatim — multiline assistant answers never
  visually swallow later user turns, and tag values (``n``, ``role``,
  ``latest``) are the only attributes that appear.
- Same input -> byte-identical output (deterministic formatting).
"""

from __future__ import annotations

from typing import Any

from agent.graph.context.contracts import TranscriptWindow
from agent.graph.context.serialization import render_recent_transcript


def _make_window(
    messages: list[dict[str, Any]],
    *,
    dropped_older_turn_count: int = 0,
) -> TranscriptWindow:
    """Return a minimal TranscriptWindow with verbatim messages."""
    return TranscriptWindow(
        turns=messages,
        target_turn_count=10,
        hard_minimum_turn_count=5,
        dropped_older_turn_count=dropped_older_turn_count,
    )


def test_renders_user_and_assistant_blocks_with_bounded_turn_tags() -> None:
    window = _make_window(
        [
            {"role": "user", "content": "scan 10.129.28.200"},
            {"role": "assistant", "content": "## Nmap Scan Summary"},
        ]
    )

    rendered = render_recent_transcript(window)

    expected = (
        "<turn n=1 role=user>\n"
        "scan 10.129.28.200\n"
        "</turn>\n"
        "\n"
        "<turn n=1 role=assistant>\n"
        "## Nmap Scan Summary\n"
        "</turn>"
    )
    assert rendered == expected


def test_multiline_assistant_body_is_bounded_from_next_user_turn() -> None:
    window = _make_window(
        [
            {"role": "user", "content": "scan 10.129.28.200"},
            {
                "role": "assistant",
                "content": (
                    "## Nmap Scan Summary (10.129.28.200)\n"
                    "Command profile: default\n"
                    "Recommended Next Steps\n"
                    "- enumerate services"
                ),
            },
            {"role": "user", "content": "ok then scan 127.0.0.1"},
        ]
    )

    rendered = render_recent_transcript(window)

    # The close tag bounds the assistant body before the blank-line
    # separator and the next user block opens -- multiline assistant
    # content cannot visually swallow the follow-up user turn.
    assert "</turn>\n\n<turn n=2 role=user>\nok then scan 127.0.0.1" in rendered
    # The assistant body is preserved verbatim, including its own
    # internal newlines.
    assert (
        "<turn n=1 role=assistant>\n"
        "## Nmap Scan Summary (10.129.28.200)\n"
    ) in rendered
    assert "- enumerate services\n</turn>" in rendered


def test_tool_and_system_roles_get_their_own_turn_blocks() -> None:
    window = _make_window(
        [
            {"role": "system", "content": "[summary of prior conversation]"},
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "ack"},
            {"role": "tool", "content": "raw tool output"},
        ]
    )

    rendered = render_recent_transcript(window)

    # Leading system message keeps the pre-first-user index (0 here),
    # so the contract ``n = dropped_older_turn_count`` holds even when
    # no turns have been dropped.
    assert rendered.startswith(
        "<turn n=0 role=system>\n[summary of prior conversation]\n</turn>\n\n"
    )
    assert "<turn n=1 role=tool>\nraw tool output\n</turn>" in rendered


def test_turn_index_respects_dropped_older_turn_count() -> None:
    window = _make_window(
        [
            {"role": "user", "content": "latest question"},
            {"role": "assistant", "content": "latest answer"},
        ],
        dropped_older_turn_count=3,
    )

    rendered = render_recent_transcript(window)

    # First user message after a 3-turn drop is the 4th turn overall.
    assert rendered.startswith("<turn n=4 role=user>\nlatest question\n</turn>")
    assert "<turn n=4 role=assistant>\nlatest answer\n</turn>" in rendered


def test_unknown_role_renders_as_lowercase_tag_value() -> None:
    window = _make_window(
        [
            {"role": "developer", "content": "debug note"},
        ]
    )

    rendered = render_recent_transcript(window)

    # Non-canonical role still produces a valid bounded block; the raw
    # value is normalised but never rewritten into a different tag.
    assert rendered == "<turn n=0 role=developer>\ndebug note\n</turn>"


def test_case_insensitive_role_matching_keeps_tag_values_stable() -> None:
    window = _make_window(
        [
            {"role": "USER", "content": "hi"},
            {"role": " Assistant ", "content": "hello"},
        ]
    )

    rendered = render_recent_transcript(window)

    expected = (
        "<turn n=1 role=user>\nhi\n</turn>\n\n"
        "<turn n=1 role=assistant>\nhello\n</turn>"
    )
    assert rendered == expected


def test_none_content_renders_as_empty_body_without_breaking_block_shape() -> None:
    window = _make_window(
        [
            {"role": "user", "content": None},
            {"role": "assistant", "content": "response"},
        ]
    )

    rendered = render_recent_transcript(window)

    # Empty body collapses to ``<open>\n<close>`` so the block stays
    # visually bounded but does not accrete a blank line inside.
    assert rendered == (
        "<turn n=1 role=user>\n</turn>\n\n"
        "<turn n=1 role=assistant>\nresponse\n</turn>"
    )


def test_non_string_content_is_stringified_deterministically() -> None:
    window = _make_window(
        [
            {"role": "assistant", "content": {"structured": "payload"}},
        ]
    )

    rendered = render_recent_transcript(window)

    assert rendered == (
        "<turn n=0 role=assistant>\n{'structured': 'payload'}\n</turn>"
    )


def test_empty_transcript_window_renders_as_empty_string() -> None:
    window = _make_window([])

    rendered = render_recent_transcript(window)

    assert rendered == ""


def test_output_is_deterministic_across_repeated_calls() -> None:
    window = _make_window(
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first\nmulti\nline answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ]
    )

    first = render_recent_transcript(window)
    second = render_recent_transcript(window)

    assert first == second
    # Also byte-identical when a current-turn is passed -- the extra
    # rendering branch must be deterministic on the same inputs.
    current = {"role": "user", "content": "follow up"}
    assert render_recent_transcript(
        window, current_user_turn=current
    ) == render_recent_transcript(window, current_user_turn=current)


def test_no_metadata_or_relative_markers_leak_into_output() -> None:
    window = _make_window(
        [
            {
                "role": "user",
                "content": "q1",
                "id": "msg-123",
                "timestamp": "2026-04-14T12:00:00Z",
            },
            {"role": "assistant", "content": "a1", "id": "msg-124"},
        ]
    )

    rendered = render_recent_transcript(window)

    # Only ``n``/``role``/``latest`` attributes may appear; message-
    # level metadata (ids, timestamps, legacy "Turn N" markers) must
    # never leak into the prompt surface.
    assert "Turn 1" not in rendered
    assert "Turn 2" not in rendered
    assert "msg-123" not in rendered
    assert "msg-124" not in rendered
    assert "2026-04-14" not in rendered


# -- Cache-stability regression (Task 4.2). -----------------------------


def test_appending_a_new_turn_only_appends_to_the_rendered_tail() -> None:
    """Prompt-prefix stability: new turns add to the tail, never shift the head.

    Cache-friendliness depends on the already-emitted prefix staying
    byte-identical when a new turn is appended. If the renderer ever
    started injecting window-relative markers or recomputed separators
    mid-window, a turn append would invalidate the provider-side
    prompt-prefix cache for every earlier block.
    """
    base_messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    extended_messages = base_messages + [
        {"role": "user", "content": "third question"},
        {"role": "assistant", "content": "third answer"},
    ]

    base_rendered = render_recent_transcript(_make_window(base_messages))
    extended_rendered = render_recent_transcript(_make_window(extended_messages))

    # The extended rendering starts with the base rendering verbatim --
    # the appended turns are the only delta, and they sit in the tail.
    assert extended_rendered.startswith(base_rendered)
    appended_tail = extended_rendered[len(base_rendered) :]
    # Exactly one blank-line separator between the old tail and the new
    # first appended block, and the new user block opens at n=3.
    assert appended_tail.startswith("\n\n<turn n=3 role=user>\nthird question\n</turn>")
    assert appended_tail.endswith("<turn n=3 role=assistant>\nthird answer\n</turn>")


def test_rendered_output_contains_no_dynamic_metadata_fields() -> None:
    """Regression guard against accidental metadata leakage into the prompt.

    The renderer must ignore message metadata (``id``, ``timestamp``,
    ``turn_number``, provider-specific annotations) even when upstream
    persists it on the message dict. Any such field leaking into the
    rendered text would break cache-prefix stability across otherwise
    semantically identical turns.
    """
    window = _make_window(
        [
            {
                "role": "user",
                "content": "stable body",
                "id": "msg-abc",
                "timestamp": "2026-04-14T12:00:00Z",
                "turn_number": 3,
                "custom_marker": "ephemeral",
            },
        ]
    )

    rendered = render_recent_transcript(window)

    # Only the bounded tag wrapper and verbatim body should appear; no
    # metadata field names or values leak into the prompt surface.
    assert rendered == "<turn n=1 role=user>\nstable body\n</turn>"
    for forbidden in ("msg-abc", "2026-04-14", "turn_number", "ephemeral"):
        assert forbidden not in rendered


def test_current_user_turn_is_appended_as_latest_block() -> None:
    """Opt-in current-turn rendering carries the ``latest=true`` tag."""
    window = _make_window(
        [
            {"role": "user", "content": "scan host"},
            {"role": "assistant", "content": "running scan"},
        ]
    )
    current = {"role": "user", "content": "run those by yourself"}

    rendered = render_recent_transcript(window, current_user_turn=current)

    # The window's prior turns render unchanged at the head.
    assert rendered.startswith("<turn n=1 role=user>\nscan host\n</turn>")
    # The current turn is appended as the last block, tagged latest=true,
    # with ``n`` incremented past the last user turn in the window.
    assert rendered.endswith(
        "<turn n=2 role=user latest=true>\nrun those by yourself\n</turn>"
    )
