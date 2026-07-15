"""Persistence-to-prompt regression for structured conversation history.

End-to-end lock-in for the Phase 4 success criterion: a realistic
conversation persisted as ``ChatMessage`` rows — including multiline
assistant answers, multiple user follow-ups, and an optional
``SYSTEM_SUMMARY`` marker — must survive intact all the way to the
final prompt-facing recent-transcript text. Specifically:

- ``ConversationHistoryReader.build_openai_conversation_history`` returns
  each persisted user/assistant message as a distinct entry (no
  silent merging, no content loss).
- The shared context-bundle builder preserves the full recent window
  verbatim.
- The shared prompt serializer renders each message as a role-labeled
  block whose body is the exact persisted content.
- Multiline assistant content never visually swallows a later user
  turn in the rendered prompt surface.

These assertions hold without any database schema change — the fix is
the serializer/section-ordering authority, not persistence.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List
from unittest.mock import Mock

from agent.graph.context.builder import build_conversation_context_bundle
from agent.graph.context.projections import (
    project_for_planner,
)
from agent.graph.context.serialization import (
    SECTION_RECENT_TRANSCRIPT,
    serialize_projection_to_section_map,
)
from backend.services.chat.conversation_history_reader import (
    SYSTEM_SUMMARY_MESSAGE_TYPE,
    ConversationHistoryReader,
)

# Persisted ``ChatMessage.message_type`` values for user/assistant turns
# (the loader normalises casing and a small set of aliases; see
# ``ConversationHistoryReader.convert_chat_messages_to_openai``).
MESSAGE_TYPE_USER = "user"
MESSAGE_TYPE_ASSISTANT = "assistant"


def _persisted_message(
    *,
    message_id: int,
    task_id: int,
    conversation_id: str,
    parent_message_id: int | None,
    message_type: str,
    message: str,
    turn_number: int | None,
) -> SimpleNamespace:
    """Build a lightweight persisted ``ChatMessage`` stand-in.

    ``ConversationHistoryReader.convert_chat_messages_to_openai`` only reads
    attribute-style fields on each row, so a ``SimpleNamespace`` with
    the expected attributes is sufficient for this end-to-end test —
    no live DB session needed.
    """
    return SimpleNamespace(
        id=message_id,
        task_id=task_id,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
        message_type=message_type,
        message=message,
        reasoning_tokens=None,
        observation_tokens=None,
        citations=None,
        error=None,
        token_count=0,
        tool_calls=[],
        turn_number=turn_number,
    )


def _build_reader_with_history(
    rows: List[SimpleNamespace],
) -> ConversationHistoryReader:
    """Return a ``ConversationHistoryReader`` whose history loader yields ``rows``."""
    reader = ConversationHistoryReader(Mock())
    reader.get_conversation_history = Mock(return_value=list(rows))  # type: ignore[assignment]
    return reader


MULTILINE_ASSISTANT_BODY = (
    "## Nmap Scan Summary (10.129.28.200)\n"
    "Command profile: default\n"
    "Recommended Next Steps\n"
    "- enumerate services\n"
    "- review banner data"
)


def test_persisted_multiturn_history_survives_to_prompt_surface() -> None:
    """Each persisted user/assistant turn appears verbatim in the prompt surface."""
    conversation_id = "conv-persistence-e2e"
    task_id = 42
    rows = [
        _persisted_message(
            message_id=1,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=None,
            message_type=MESSAGE_TYPE_USER,
            message="scan 10.129.28.200 with version scanning and nse default script",
            turn_number=1,
        ),
        _persisted_message(
            message_id=2,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=1,
            message_type=MESSAGE_TYPE_ASSISTANT,
            message=MULTILINE_ASSISTANT_BODY,
            turn_number=1,
        ),
        _persisted_message(
            message_id=3,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=2,
            message_type=MESSAGE_TYPE_USER,
            message="ok then scan 127.0.0.1 with postgre port",
            turn_number=2,
        ),
        _persisted_message(
            message_id=4,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=3,
            message_type=MESSAGE_TYPE_ASSISTANT,
            message="I'll run a targeted PostgreSQL port scan next.",
            turn_number=2,
        ),
    ]
    reader = _build_reader_with_history(rows)

    # --- Persistence layer: each user/assistant message is distinct ---------
    history = reader.build_openai_conversation_history(
        task_id=task_id,
        conversation_id=conversation_id,
    )
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert history[0]["content"].startswith("scan 10.129.28.200")
    assert history[1]["content"] == MULTILINE_ASSISTANT_BODY
    assert history[2]["content"] == "ok then scan 127.0.0.1 with postgre port"

    # --- Bundle: recent window is the full history (well under policy cap) --
    bundle = build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id="turn-3",
        turn_sequence=2,
        messages=history,
    )
    assert bundle["transcript_window"]["turns"] == history
    assert bundle["transcript_window"]["dropped_older_turn_count"] == 0

    # --- Prompt surface: serialized transcript carries every turn verbatim --
    projection = project_for_planner(bundle)
    section_map = serialize_projection_to_section_map(projection)
    transcript = section_map[SECTION_RECENT_TRANSCRIPT]

    # Each message sits in a bounded ``<turn n=N role=R>…</turn>`` block.
    assert (
        "<turn n=1 role=user>\nscan 10.129.28.200 with version scanning"
        in transcript
    )
    assert (
        f"<turn n=1 role=assistant>\n{MULTILINE_ASSISTANT_BODY}\n</turn>"
        in transcript
    )

    # Multiline-safety: the later user turn's open tag is clearly bounded
    # from the multiline assistant body above it by the close tag + a
    # single blank-line separator.
    assert (
        "- review banner data\n</turn>\n\n"
        "<turn n=2 role=user>\nok then scan 127.0.0.1 with postgre port"
    ) in transcript


def test_persisted_system_summary_marker_anchors_the_recent_window() -> None:
    """``SYSTEM_SUMMARY`` marker renders as a System block at the window head.

    When persistence includes a ``SYSTEM_SUMMARY`` marker, the loader
    emits a leading ``system`` message followed by post-summary turns.
    The transcript-window policy preserves the leading non-user segment
    at the head of the window, and the serializer renders it as a
    ``System:`` block — not a fake user turn, not a silent drop.
    """
    conversation_id = "conv-summary-marker"
    task_id = 77
    rows = [
        _persisted_message(
            message_id=10,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=None,
            message_type=MESSAGE_TYPE_USER,
            message="older pre-summary question",
            turn_number=1,
        ),
        _persisted_message(
            message_id=11,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=10,
            message_type=SYSTEM_SUMMARY_MESSAGE_TYPE,
            message="[summary] prior conversation covered host discovery on 10.0.0.0/24",
            turn_number=None,
        ),
        _persisted_message(
            message_id=12,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=11,
            message_type=MESSAGE_TYPE_USER,
            message="continue with service enumeration",
            turn_number=2,
        ),
        _persisted_message(
            message_id=13,
            task_id=task_id,
            conversation_id=conversation_id,
            parent_message_id=12,
            message_type=MESSAGE_TYPE_ASSISTANT,
            message="Running enumeration now.",
            turn_number=2,
        ),
    ]
    rows[1].citations = {
        "context_compression": {"through_message_id": 10}
    }
    reader = _build_reader_with_history(rows)

    history = reader.build_openai_conversation_history(
        task_id=task_id,
        conversation_id=conversation_id,
    )
    # The loader drops anything before the latest summary and emits the
    # summary as the leading ``system`` message.
    assert history[0]["role"] == "system"
    assert "prior conversation covered host discovery" in history[0]["content"]
    assert [message["role"] for message in history[1:]] == ["user", "assistant"]

    bundle = build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id="turn-2",
        turn_sequence=1,
        messages=history,
    )
    # Leading ``system`` segment is pinned at the head of the window, not
    # treated as a turn (``dropped_older_turn_count`` reports turns only).
    assert bundle["transcript_window"]["turns"][0]["role"] == "system"
    assert bundle["transcript_window"]["dropped_older_turn_count"] == 0

    projection = project_for_planner(bundle)
    section_map = serialize_projection_to_section_map(projection)
    transcript = section_map[SECTION_RECENT_TRANSCRIPT]

    # System summary renders as its own bounded block at the top; being
    # in the leading segment (pre-first-user) it carries the pre-offset
    # index (0 when nothing was dropped).
    assert transcript.startswith(
        "<turn n=0 role=system>\n[summary] prior conversation"
    )
    # Follow-up user/assistant blocks are separated from the summary and
    # each other by the close tag + exactly one blank line.
    assert "</turn>\n\n<turn n=1 role=user>\ncontinue with service enumeration" in transcript
    assert "</turn>\n\n<turn n=1 role=assistant>\nRunning enumeration now." in transcript
