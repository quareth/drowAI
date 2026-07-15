"""Tests for ChatStateContainer (: State Container & Handler Integration)."""

import pytest

from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer


def test_append_answer_accumulates():
    c = ChatStateContainer()
    c.append_answer("Hello ")
    c.append_answer("world")
    assert c.get_answer_tokens() == "Hello world"


def test_append_reasoning_accumulates():
    c = ChatStateContainer()
    c.append_reasoning("Step 1. ")
    c.append_reasoning("Step 2.")
    assert c.get_reasoning_tokens() == "Step 1. Step 2."


def test_add_tool_call():
    c = ChatStateContainer()
    c.add_tool_call({"tool_name": "nmap", "tool_id": "nmap", "tool_arguments": {}})
    c.add_tool_call({"tool_name": "curl", "tool_id": "curl", "tool_arguments": {"url": "x"}})
    calls = c.get_tool_calls()
    assert len(calls) == 2
    assert calls[0]["tool_name"] == "nmap"
    assert calls[1]["tool_name"] == "curl"


def test_empty_container():
    c = ChatStateContainer()
    assert c.get_answer_tokens() == ""
    assert c.get_reasoning_tokens() == ""
    assert c.get_observation_tokens() == []
    assert c.get_tool_calls() == []


def test_append_answer_empty_ignored():
    c = ChatStateContainer()
    c.append_answer("")
    c.append_answer("x")
    assert c.get_answer_tokens() == "x"


def test_get_tool_calls_returns_copy():
    c = ChatStateContainer()
    c.add_tool_call({"tool_name": "a"})
    out = c.get_tool_calls()
    out.append({"tool_name": "b"})
    assert len(c.get_tool_calls()) == 1


def test_tool_call_parameters_inherit_from_start():
    c = ChatStateContainer()
    c.record_tool_call_start("tc-1", {"target": "127.0.0.1"})
    stored = c.add_tool_call(
        {
            "tool_call_id": "tc-1",
            "tool_name": "nmap",
            "tool_arguments": {},
        }
    )
    assert stored["tool_arguments"]["target"] == "127.0.0.1"


def test_tool_call_turn_index_increments():
    c = ChatStateContainer()
    first = c.add_tool_call({"tool_call_id": "tc-1", "tool_name": "a"})
    second = c.add_tool_call({"tool_call_id": "tc-2", "tool_name": "b"})
    assert first["turn_index"] == 0
    assert second["turn_index"] == 1
    assert first["phase_sequence"] == 0
    assert second["phase_sequence"] == 1


def test_observation_sections_accumulate_in_order():
    c = ChatStateContainer()
    c.start_observation(sub_turn_index=0)
    c.append_observation("obs-1-a ")
    c.append_observation("obs-1-b")
    c.end_observation()
    c.start_observation(sub_turn_index=1)
    c.append_observation("obs-2")
    c.end_observation()
    assert c.get_observation_tokens() == [
        {"content": "obs-1-a obs-1-b", "phase_sequence": 0, "sub_turn_index": 0},
        {"content": "obs-2", "phase_sequence": 1, "sub_turn_index": 1},
    ]


def test_observation_snapshot_replaces_partial_chunks():
    c = ChatStateContainer()
    c.start_observation()
    c.append_observation("partial ")
    c.append_observation("stream")
    c.append_observation("final snapshot", snapshot=True)
    c.end_observation()
    assert c.get_observation_tokens() == [{"content": "final snapshot", "phase_sequence": 0}]


def test_observation_snapshot_after_section_end_replaces_last_section():
    c = ChatStateContainer()
    c.start_observation(sub_turn_index=0)
    c.append_observation("streamed ")
    c.append_observation("value")
    c.end_observation()
    c.append_observation("snapshot value", snapshot=True, sub_turn_index=0)
    assert c.get_observation_tokens() == [
        {"content": "snapshot value", "phase_sequence": 0, "sub_turn_index": 0}
    ]


def test_phase_sequence_monotonic_across_interleaved_detail_events():
    c = ChatStateContainer()

    c.add_tool_call({"tool_call_id": "tc-1", "tool_name": "tool-a"})
    c.start_observation(sub_turn_index=0)
    c.append_observation("obs-a")
    c.end_observation()
    c.add_tool_call({"tool_call_id": "tc-2", "tool_name": "tool-b"})
    c.start_observation(sub_turn_index=1)
    c.append_observation("obs-b")
    c.end_observation()

    tool_sequences = [call["phase_sequence"] for call in c.get_tool_calls()]
    observation_sequences = [token["phase_sequence"] for token in c.get_observation_tokens()]

    assert tool_sequences == [0, 2]
    assert observation_sequences == [1, 3]


# --- Structured reasoning section tests ---


def test_reasoning_sections_preserve_phase_sequence():
    """Multiple reasoning sections get distinct phase_sequence values."""
    container = ChatStateContainer()
    first_identity = container.start_reasoning(
        section_name="intent",
        sub_turn_index=0,
        identity_scope="turn-1",
    )
    container.append_reasoning("Analyzing request.")
    container.end_reasoning()
    second_identity = container.start_reasoning(
        section_name="planner",
        sub_turn_index=1,
        identity_scope="turn-1",
    )
    container.append_reasoning("Building plan.")
    container.end_reasoning()

    sections = container.get_reasoning_sections()
    assert [row["section_name"] for row in sections] == ["intent", "planner"]
    assert [row["sub_turn_index"] for row in sections] == [0, 1]
    assert sections[0]["phase_sequence"] < sections[1]["phase_sequence"]
    assert first_identity["reasoning_section_id"] == "turn-1:reasoning:0"
    assert second_identity["reasoning_section_id"] == "turn-1:reasoning:1"
    assert [row["reasoning_section_id"] for row in sections] == [
        "turn-1:reasoning:0",
        "turn-1:reasoning:1",
    ]


def test_reasoning_sections_accumulate_deltas():
    """Reasoning deltas within a section are joined into one content string."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="think")
    c.append_reasoning("Part A. ")
    c.append_reasoning("Part B.")
    c.end_reasoning()

    sections = c.get_reasoning_sections()
    assert len(sections) == 1
    assert sections[0]["content"] == "Part A. Part B."
    assert sections[0]["section_name"] == "think"


def test_reasoning_sections_capture_start_and_end_timestamps(monkeypatch: pytest.MonkeyPatch):
    """Structured reasoning sections preserve boundary timestamps for replay."""
    timestamps = iter([100.0, 108.4])
    monkeypatch.setattr(
        "backend.services.langgraph_chat.runtime.state_container.time.time",
        lambda: next(timestamps),
    )

    c = ChatStateContainer()
    c.start_reasoning(section_name="timed")
    c.append_reasoning("Measured reasoning")
    c.end_reasoning()

    sections = c.get_reasoning_sections()
    assert sections[0]["started_at"] == 100.0
    assert sections[0]["ended_at"] == 108.4


def test_reasoning_sections_empty_section_ignored():
    """A reasoning section with no content is not stored."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="empty")
    c.end_reasoning()
    assert c.get_reasoning_sections() == []


def test_reasoning_sections_whitespace_only_section_ignored():
    """A reasoning section with only whitespace is not stored."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="blank")
    c.append_reasoning("   ")
    c.end_reasoning()
    assert c.get_reasoning_sections() == []


def test_get_reasoning_tokens_compatibility_with_structured_sections():
    """get_reasoning_tokens() joins structured sections when they exist."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="s1")
    c.append_reasoning("Alpha")
    c.end_reasoning()
    c.start_reasoning(section_name="s2")
    c.append_reasoning("Beta")
    c.end_reasoning()

    assert c.get_reasoning_tokens() == "Alpha\n\nBeta"


def test_get_reasoning_tokens_falls_back_to_flat_buffer():
    """Without structured sections, get_reasoning_tokens() returns flat buffer."""
    c = ChatStateContainer()
    c.append_reasoning("raw delta 1 ")
    c.append_reasoning("raw delta 2")
    assert c.get_reasoning_tokens() == "raw delta 1 raw delta 2"


def test_reasoning_sections_auto_finalize_on_new_start():
    """Starting a new section auto-finalizes the previous one."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="first")
    c.append_reasoning("Content A")
    # Start second section without explicit end_reasoning
    c.start_reasoning(section_name="second")
    c.append_reasoning("Content B")
    c.end_reasoning()

    sections = c.get_reasoning_sections()
    assert len(sections) == 2
    assert sections[0]["section_name"] == "first"
    assert sections[1]["section_name"] == "second"


def test_reasoning_sections_auto_finalize_on_get():
    """get_reasoning_sections() finalizes an active section."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="active")
    c.append_reasoning("Still open")
    # Do not call end_reasoning
    sections = c.get_reasoning_sections()
    assert len(sections) == 1
    assert sections[0]["content"] == "Still open"


def test_end_reasoning_provides_late_sub_turn_index():
    """sub_turn_index can be provided at end_reasoning if not set at start."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="late_idx")
    c.append_reasoning("Content")
    c.end_reasoning(sub_turn_index=42)

    sections = c.get_reasoning_sections()
    assert sections[0]["sub_turn_index"] == 42


def test_end_reasoning_does_not_override_existing_sub_turn_index():
    """If sub_turn_index was set at start, end_reasoning does not override."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="keep", sub_turn_index=5)
    c.append_reasoning("Content")
    c.end_reasoning(sub_turn_index=99)

    sections = c.get_reasoning_sections()
    assert sections[0]["sub_turn_index"] == 5


def test_reasoning_sections_optional_fields_omitted():
    """section_name and sub_turn_index are omitted when None."""
    c = ChatStateContainer()
    c.start_reasoning()
    c.append_reasoning("No metadata")
    c.end_reasoning()

    sections = c.get_reasoning_sections()
    assert len(sections) == 1
    assert "section_name" not in sections[0]
    assert "sub_turn_index" not in sections[0]
    assert sections[0]["content"] == "No metadata"


def test_empty_container_has_no_reasoning_sections():
    """Empty container returns empty reasoning sections."""
    c = ChatStateContainer()
    assert c.get_reasoning_sections() == []


def test_phase_sequence_interleaves_reasoning_with_other_events():
    """Reasoning sections participate in global phase_sequence ordering."""
    c = ChatStateContainer()

    c.start_reasoning(section_name="think-1", sub_turn_index=0)
    c.append_reasoning("Reasoning block 1")
    c.end_reasoning()

    c.add_tool_call({"tool_call_id": "tc-1", "tool_name": "nmap"})

    c.start_observation(sub_turn_index=0)
    c.append_observation("obs-1")
    c.end_observation()

    c.start_reasoning(section_name="think-2", sub_turn_index=1)
    c.append_reasoning("Reasoning block 2")
    c.end_reasoning()

    reasoning_seqs = [s["phase_sequence"] for s in c.get_reasoning_sections()]
    tool_seqs = [t["phase_sequence"] for t in c.get_tool_calls()]
    obs_seqs = [o["phase_sequence"] for o in c.get_observation_tokens()]

    # Reasoning-1(0) < Tool(1) < Obs(2) < Reasoning-2(3)
    assert reasoning_seqs == [0, 3]
    assert tool_seqs == [1]
    assert obs_seqs == [2]


def test_append_reasoning_writes_to_both_flat_and_structured():
    """append_reasoning populates both the legacy flat buffer and active section."""
    c = ChatStateContainer()
    c.start_reasoning(section_name="dual")
    c.append_reasoning("Shared ")
    c.append_reasoning("content")
    c.end_reasoning()

    # Structured path
    sections = c.get_reasoning_sections()
    assert sections[0]["content"] == "Shared content"

    # Flat buffer also has the content (used by get_reasoning_tokens compatibility)
    # Since structured sections exist, get_reasoning_tokens joins them
    assert c.get_reasoning_tokens() == "Shared content"
