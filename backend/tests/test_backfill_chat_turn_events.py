"""Regression tests for chat_turn_events backfill ordering semantics."""

from types import SimpleNamespace

from backend.scripts.backfill_chat_turn_events import _build_ordered_events


def test_build_ordered_events_interleaves_colliding_indices_deterministically() -> None:
    """Backfill keeps tool/observation alternation when indices collide."""
    message = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(
                id=10,
                turn_index=0,
                tool_call_id="tool-a",
                tool_result="tool-a",
                chat_message_id=123,
                tool_name="tool_a",
                tool_arguments={},
                parent_tool_call_id=None,
            ),
            SimpleNamespace(
                id=11,
                turn_index=1,
                tool_call_id="tool-b",
                tool_result="tool-b",
                chat_message_id=123,
                tool_name="tool_b",
                tool_arguments={},
                parent_tool_call_id=None,
            ),
        ],
        observation_tokens='[{"content":"obs-a","sub_turn_index":0},{"content":"obs-b","sub_turn_index":1}]',
    )

    events = _build_ordered_events(message)

    assert [(event["kind"], event["content"]) for event in events] == [
        ("tool", "tool-a"),
        ("observation", "obs-a"),
        ("tool", "tool-b"),
        ("observation", "obs-b"),
    ]
    assert [event["phase_sequence"] for event in events] == [0, 1, 2, 3]
