"""Tests for the current-turn iteration-memory helper.

Covers the Phase 1 render characterization and Phase 2 storage move contract:

- counter initializes and increments monotonically per turn,
- counter resets cleanly on turn boundary,
- ledger records remain ordered and runtime-stamped,
- render-focused characterization targets multiline ``<phase ...>`` blocks,
- reads use the public helper API only,
- storage stays under metadata["working_memory"],
- ledger remains uncapped (no eviction above 50 records).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from agent.graph.utils.iteration_memory import (
    append,
    get_current_turn_scope,
    get_ledger,
    has_renderable_sections,
    latest_recorded_phase_sequence,
    peek_next_phase_sequence,
    render,
    render_latest_phase_memory_section,
    render_phase_memory_section,
    reserve_next_phase_sequence,
)


def _working_memory(metadata: Dict[str, Any]) -> Dict[str, Any]:
    raw = metadata.get("working_memory")
    if isinstance(raw, dict):
        return raw
    return {}


def _ledger_ref(metadata: Dict[str, Any]) -> list[Any]:
    raw = _working_memory(metadata).get("current_turn_phases")
    if isinstance(raw, list):
        return raw
    return []


def _counter(metadata: Dict[str, Any]) -> int | None:
    value = _working_memory(metadata).get("current_turn_phase_counter")
    return value if isinstance(value, int) else None


def _turn_scope(metadata: Dict[str, Any]) -> int | None:
    value = _working_memory(metadata).get("current_turn_phase_turn")
    return value if isinstance(value, int) else None


def _phase_record(
    *,
    turn_sequence: int,
    phase_sequence: int,
    source: str,
    sections: list[tuple[str, str]],
) -> Dict[str, Any]:
    return {
        "turn_sequence": turn_sequence,
        "phase_sequence": phase_sequence,
        "source": source,
        "sections": [
            {"heading": heading, "body": body} for heading, body in sections
        ],
    }


def _metadata_with_phase_records(*records: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "working_memory": {
            "current_turn_phases": list(records),
        }
    }


class TestHasRenderableSections:
    """Validation helper recognizes only sanitized section snapshots."""

    def test_returns_true_when_any_section_survives_sanitization(self) -> None:
        assert has_renderable_sections(
            {
                "sections": [
                    {"heading": "  Tool Output Summary  ", "body": "  ok  "},
                    {"heading": "", "body": "missing heading"},
                ]
            }
        ) is True

    def test_returns_false_for_legacy_semantic_payload_without_sections(self) -> None:
        assert has_renderable_sections(
            {
                "kind": "reasoning_step",
                "summary": "legacy semantic payload",
            }
        ) is False


class TestReserveNextPhaseSequence:
    """Counter behavior for ``reserve_next_phase_sequence``."""

    def test_initializes_to_zero_for_first_call_of_turn(self) -> None:
        metadata: Dict[str, Any] = {}

        first = reserve_next_phase_sequence(metadata, turn_sequence=12)

        assert first == 0
        assert _turn_scope(metadata) == 12
        assert _counter(metadata) == 1

    def test_increments_monotonically_within_same_turn(self) -> None:
        metadata: Dict[str, Any] = {}

        values = [
            reserve_next_phase_sequence(metadata, turn_sequence=3)
            for _ in range(4)
        ]

        assert values == [0, 1, 2, 3]
        assert _counter(metadata) == 4

    def test_resets_on_new_turn_boundary(self) -> None:
        metadata: Dict[str, Any] = {}

        reserve_next_phase_sequence(metadata, turn_sequence=7)
        reserve_next_phase_sequence(metadata, turn_sequence=7)
        first_in_next_turn = reserve_next_phase_sequence(
            metadata, turn_sequence=8
        )

        assert first_in_next_turn == 0
        assert _turn_scope(metadata) == 8
        assert _counter(metadata) == 1


class TestPeekNextPhaseSequence:
    """``peek_next_phase_sequence`` must not mutate the counter."""

    def test_returns_zero_when_turn_not_yet_seen(self) -> None:
        metadata: Dict[str, Any] = {}

        assert peek_next_phase_sequence(metadata, turn_sequence=4) == 0
        # peek must not mutate
        assert _counter(metadata) is None
        assert _turn_scope(metadata) is None

    def test_returns_counter_without_advancing(self) -> None:
        metadata: Dict[str, Any] = {}
        reserve_next_phase_sequence(metadata, turn_sequence=4)
        reserve_next_phase_sequence(metadata, turn_sequence=4)

        peeked = peek_next_phase_sequence(metadata, turn_sequence=4)
        peeked_again = peek_next_phase_sequence(metadata, turn_sequence=4)

        assert peeked == 2
        assert peeked_again == 2
        assert _counter(metadata) == 2

    def test_returns_zero_after_turn_boundary(self) -> None:
        metadata: Dict[str, Any] = {}
        reserve_next_phase_sequence(metadata, turn_sequence=4)

        assert peek_next_phase_sequence(metadata, turn_sequence=5) == 0


class TestAppend:
    """Append contract: runtime identity, section filtering, ordering."""

    def test_appends_record_with_runtime_stamped_identity(self) -> None:
        metadata: Dict[str, Any] = {}

        record = append(
            metadata,
            turn_sequence=12,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Tool Output Summary",
                        "body": "21/tcp filtered",
                    }
                ],
            },
        )

        assert record["turn_sequence"] == 12
        assert record["phase_sequence"] == 0
        assert record["source"] == "tool"
        assert record["sections"] == [
            {"heading": "Tool Output Summary", "body": "21/tcp filtered"}
        ]

        ledger = get_ledger(metadata)
        assert len(ledger) == 1
        assert ledger[0] == record
        assert _ledger_ref(metadata)[0] is record
        assert len(_ledger_ref(metadata)) == 1

    def test_ignores_identity_fields_supplied_in_payload(self) -> None:
        metadata: Dict[str, Any] = {}

        record = append(
            metadata,
            turn_sequence=12,
            source="ptr",
            payload={
                "turn_sequence": 999,
                "phase_sequence": 42,
                "source": "tool",
                "sections": [
                    {
                        "heading": "PTR Decision",
                        "body": "next_action: call_tool",
                    }
                ],
            },
        )

        # Runtime identity wins over LLM-supplied values.
        assert record["turn_sequence"] == 12
        assert record["phase_sequence"] == 0
        assert record["source"] == "ptr"
        assert record["sections"] == [
            {"heading": "PTR Decision", "body": "next_action: call_tool"}
        ]

    def test_drops_unknown_semantic_fields(self) -> None:
        metadata: Dict[str, Any] = {}

        record = append(
            metadata,
            turn_sequence=1,
            source="ptr",
            payload={
                "sections": [
                    {
                        "heading": "Action Reasoning",
                        "body": "known summary",
                    }
                ],
                "kind": "reasoning_step",
                "summary": "known summary",
                "not_in_schema": "should-not-leak",
                "another_stray": 42,
            },
        )

        assert record["sections"] == [
            {"heading": "Action Reasoning", "body": "known summary"}
        ]
        assert "kind" not in record
        assert "summary" not in record
        assert "not_in_schema" not in record
        assert "another_stray" not in record

    def test_drops_empty_sections_and_trims_whitespace(self) -> None:
        metadata: Dict[str, Any] = {}

        record = append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={
                "sections": [
                    {"heading": "  Tool Output Summary  ", "body": "  ok  "},
                    {"heading": "", "body": "missing heading"},
                    {"heading": "Missing Body", "body": "   "},
                ],
            },
        )

        assert record["sections"] == [
            {"heading": "Tool Output Summary", "body": "ok"}
        ]

    def test_preserves_chronological_order_across_mixed_sources(self) -> None:
        metadata: Dict[str, Any] = {}

        append(
            metadata,
            turn_sequence=2,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "first step"}]},
        )
        append(
            metadata,
            turn_sequence=2,
            source="tool",
            payload={
                "sections": [
                    {"heading": "Tool Output Summary", "body": "tool step"},
                ]
            },
        )
        append(
            metadata,
            turn_sequence=2,
            source="think_more",
            payload={
                "sections": [{"heading": "Action Reasoning", "body": "third step"}]
            },
        )
        append(
            metadata,
            turn_sequence=2,
            source="reflect",
            payload={"sections": [{"heading": "Reflection", "body": "fourth step"}]},
        )

        ledger = get_ledger(metadata)
        assert [r["phase_sequence"] for r in ledger] == [0, 1, 2, 3]
        assert [r["source"] for r in ledger] == ["ptr", "tool", "think_more", "reflect"]
        assert [r["sections"][0]["heading"] for r in ledger] == [
            "PTR Decision",
            "Tool Output Summary",
            "Action Reasoning",
            "Reflection",
        ]

    def test_honors_pre_reserved_phase_sequence(self) -> None:
        metadata: Dict[str, Any] = {}

        reserved = reserve_next_phase_sequence(metadata, turn_sequence=3)
        record = append(
            metadata,
            turn_sequence=3,
            source="ptr",
            payload={
                "sections": [{"heading": "PTR Decision", "body": "reserved step"}]
            },
            phase_sequence=reserved,
        )

        assert record["phase_sequence"] == reserved == 0

    def test_resets_counter_on_turn_boundary_without_pre_reserve(self) -> None:
        metadata: Dict[str, Any] = {}

        append(
            metadata,
            turn_sequence=1,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "seed-0"}]},
        )
        append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={
                "sections": [{"heading": "Tool Output Summary", "body": "seed-1"}]
            },
        )
        assert _counter(metadata) == 2
        assert _turn_scope(metadata) == 1

        first_new_turn = append(
            metadata,
            turn_sequence=2,
            source="ptr",
            payload={
                "sections": [{"heading": "PTR Decision", "body": "first in turn 2"}]
            },
        )
        second_new_turn = append(
            metadata,
            turn_sequence=2,
            source="tool",
            payload={
                "sections": [
                    {"heading": "Tool Output Summary", "body": "second in turn 2"}
                ]
            },
        )

        assert first_new_turn["phase_sequence"] == 0
        assert second_new_turn["phase_sequence"] == 1
        assert _counter(metadata) == 2
        assert _turn_scope(metadata) == 2

    def test_explicit_phase_in_new_turn_does_not_preserve_stale_counter(self) -> None:
        metadata: Dict[str, Any] = {
            "working_memory": {
                "current_turn_phase_turn": 1,
                "current_turn_phase_counter": 9,
                "current_turn_phases": [],
            }
        }

        explicit = append(
            metadata,
            turn_sequence=2,
            source="ptr",
            payload={
                "sections": [{"heading": "PTR Decision", "body": "explicit phase"}]
            },
            phase_sequence=0,
        )
        followup = append(
            metadata,
            turn_sequence=2,
            source="tool",
            payload={
                "sections": [{"heading": "Tool Output Summary", "body": "followup phase"}]
            },
        )

        assert explicit["phase_sequence"] == 0
        assert followup["phase_sequence"] == 1
        assert _counter(metadata) == 2
        assert _turn_scope(metadata) == 2

    def test_rejects_payload_without_renderable_sections(self) -> None:
        metadata: Dict[str, Any] = {}

        with pytest.raises(ValueError, match="renderable section snapshot"):
            append(
                metadata,
                turn_sequence=8,
                source="ptr",
                payload={
                    "sections": [
                        {"heading": "", "body": "missing heading"},
                        {"heading": "Missing Body", "body": "   "},
                    ]
                },
            )

        assert get_ledger(metadata) == []


class TestLatestRecordedPhaseSequence:
    """``latest_recorded_phase_sequence`` computes the last stored phase."""

    def test_returns_none_when_ledger_is_empty(self) -> None:
        metadata: Dict[str, Any] = {}

        assert latest_recorded_phase_sequence(metadata, turn_sequence=9) is None

    def test_returns_max_phase_within_active_turn_only(self) -> None:
        metadata: Dict[str, Any] = {}

        append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "first"}]},
        )
        append(
            metadata,
            turn_sequence=1,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "second"}]},
        )

        # During turn 1, the ledger holds turn=1 records.
        assert latest_recorded_phase_sequence(metadata, turn_sequence=1) == 1
        assert latest_recorded_phase_sequence(metadata, turn_sequence=99) is None

        # Crossing into turn 2 prunes the prior turn's records (see
        # MemoryManager.reduce_phase_ledger_append).
        append(
            metadata,
            turn_sequence=2,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "third"}]},
        )

        # Turn 2 has its own counter restarting at 0.
        assert latest_recorded_phase_sequence(metadata, turn_sequence=2) == 0
        assert latest_recorded_phase_sequence(metadata, turn_sequence=1) is None


class TestRender:
    """Prompt rendering: chronological phase blocks, turn scoping, clean omission."""

    def test_returns_empty_string_when_ledger_absent(self) -> None:
        metadata: Dict[str, Any] = {}
        assert render(metadata) == ""

    def test_returns_empty_string_when_turn_filter_matches_nothing(self) -> None:
        metadata: Dict[str, Any] = {}
        append(
            metadata,
            turn_sequence=1,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "record"}]},
        )

        assert render(metadata, turn_sequence=99) == ""

    def test_renders_records_in_insertion_order(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=12,
                phase_sequence=0,
                source="ptr",
                sections=[
                    ("PTR Decision", "next_action: call_tool"),
                    ("Action Reasoning", "Retry with -Pn"),
                ],
            ),
            _phase_record(
                turn_sequence=12,
                phase_sequence=1,
                source="tool",
                sections=[
                    (
                        "Tool Executed",
                        "Tool: nmap.scan\nParameters: target=10.0.0.1, args=-Pn -p 21",
                    ),
                    ("Tool Output Summary", "21/tcp filtered"),
                ],
            ),
        )

        rendered = render(metadata, turn_sequence=12)

        phase_0 = "<phase turn=12 phase=0 source=ptr>"
        phase_1 = "<phase turn=12 phase=1 source=tool>"

        assert phase_0 in rendered
        assert phase_1 in rendered
        assert rendered.index(phase_0) < rendered.index(phase_1)
        assert "## PTR Decision" in rendered
        assert "## Action Reasoning" in rendered
        assert "## Tool Executed" in rendered
        assert "## Tool Output Summary" in rendered
        assert rendered.count("</phase>") == 2
        assert "[turn=12 phase=0 source=ptr]" not in rendered
        assert (
            "kind=network_probe; result=negative; summary=21/tcp filtered"
        ) not in rendered

    def test_filters_by_turn_sequence_when_provided(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=1,
                phase_sequence=0,
                source="tool",
                sections=[("Tool Output Summary", "old summary")],
            ),
            _phase_record(
                turn_sequence=2,
                phase_sequence=0,
                source="ptr",
                sections=[("Action Reasoning", "Use the confirmed target")],
            ),
        )

        rendered_turn_2 = render(metadata, turn_sequence=2)

        assert "<phase turn=2 phase=0 source=ptr>" in rendered_turn_2
        assert "## Action Reasoning" in rendered_turn_2
        assert "Use the confirmed target" in rendered_turn_2
        assert "<phase turn=1" not in rendered_turn_2
        assert "old summary" not in rendered_turn_2
        assert "[turn=2 phase=0 source=ptr]" not in rendered_turn_2

    def test_prunes_prior_turn_records_on_turn_boundary(self) -> None:
        metadata: Dict[str, Any] = {}
        append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "summary a"}]},
        )
        append(
            metadata,
            turn_sequence=2,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "summary b"}]},
        )

        # Storage is scoped to the active turn — turn=1 records are dropped
        # when turn=2 appends. This pins the storage-side contract only.
        ledger = metadata["working_memory"]["current_turn_phases"]
        assert all(record["turn_sequence"] == 2 for record in ledger)
        assert len(ledger) == 1

    def test_same_turn_appends_accumulate(self) -> None:
        metadata: Dict[str, Any] = {}
        append(
            metadata,
            turn_sequence=5,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "summary a"}]},
        )
        append(
            metadata,
            turn_sequence=5,
            source="ptr",
            payload={"sections": [{"heading": "PTR Decision", "body": "summary b"}]},
        )

        ledger = metadata["working_memory"]["current_turn_phases"]
        assert len(ledger) == 2
        assert ledger[0]["phase_sequence"] == 0
        assert ledger[1]["phase_sequence"] == 1

    def test_preserves_multiline_section_bodies(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=1,
                phase_sequence=0,
                source="ptr",
                sections=[
                    ("Action Reasoning", "line one\nline two\nline three"),
                ],
            )
        )

        rendered = render(metadata, turn_sequence=1)

        assert "<phase turn=1 phase=0 source=ptr>" in rendered
        assert "## Action Reasoning\nline one\nline two\nline three" in rendered
        assert "[turn=1 phase=0 source=ptr]" not in rendered

    def test_get_ledger_returns_copy(self) -> None:
        metadata: Dict[str, Any] = {}
        append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "summary a"}]},
        )

        snapshot = get_ledger(metadata)
        snapshot.append({"bogus": "entry"})  # type: ignore[typeddict-item]

        # Mutating the snapshot must not leak into stored metadata.
        assert len(_ledger_ref(metadata)) == 1

    def test_render_phase_memory_section_wraps_multiline_phase_blocks(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=11,
                phase_sequence=0,
                source="tool",
                sections=[("Tool Output Summary", "recorded summary")],
            )
        )

        section = render_phase_memory_section(metadata, turn_sequence=11)

        assert section.startswith("## Prior Current-Turn Phase Memory\n")
        assert "<phase turn=11 phase=0 source=tool>" in section
        assert "## Tool Output Summary" in section
        assert "recorded summary" in section
        assert "</phase>" in section
        assert "[turn=11 phase=0 source=tool]" not in section
        assert "kind=" not in section

    def test_render_phase_memory_section_characterizes_multiline_phase_blocks(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=3,
                phase_sequence=0,
                source="tool",
                sections=[
                    (
                        "Tool Executed",
                        "Tool: exploitation_tools.metasploit.run_exploit\n"
                        "Parameters: target=cve-2018-7600-web-1:80, "
                        "payload=php/unix/cmd/reverse_bash",
                    ),
                    (
                        "Tool Output Summary",
                        "Handler started but no session created.",
                    ),
                ],
            )
        )

        section = render_phase_memory_section(metadata, turn_sequence=3)

        assert section.startswith("## Prior Current-Turn Phase Memory\n")
        assert "<phase turn=3 phase=0 source=tool>" in section
        assert "## Tool Executed" in section
        assert "Tool: exploitation_tools.metasploit.run_exploit" in section
        assert (
            "Parameters: target=cve-2018-7600-web-1:80, "
            "payload=php/unix/cmd/reverse_bash"
        ) in section
        assert "## Tool Output Summary" in section
        assert "Handler started but no session created." in section
        assert "</phase>" in section
        assert "[turn=3 phase=0 source=tool]" not in section
        assert "kind=" not in section
        assert "status=" not in section

    def test_render_phase_memory_section_returns_empty_without_matches(self) -> None:
        metadata: Dict[str, Any] = {}

        assert render_phase_memory_section(metadata, turn_sequence=3) == ""


class TestDefensiveInputs:
    """Helper is tolerant of pre-existing invalid working-memory values."""

    def test_resets_when_counter_is_corrupt(self) -> None:
        metadata: Dict[str, Any] = {
            "working_memory": {
                "current_turn_phase_turn": 1,
                "current_turn_phase_counter": "not-an-int",
            }
        }

        reserved = reserve_next_phase_sequence(metadata, turn_sequence=1)

        assert reserved == 0
        assert _counter(metadata) == 1

    def test_get_ledger_handles_non_list_state(self) -> None:
        metadata: Dict[str, Any] = {
            "working_memory": {
                "current_turn_phases": "garbage",
            }
        }

        assert get_ledger(metadata) == []

    def test_append_initializes_non_list_ledger(self) -> None:
        metadata: Dict[str, Any] = {
            "working_memory": {
                "current_turn_phases": None,
            }
        }

        append(
            metadata,
            turn_sequence=1,
            source="tool",
            payload={"sections": [{"heading": "Tool Output Summary", "body": "summary x"}]},
        )

        ledger = _ledger_ref(metadata)
        assert isinstance(ledger, list)
        assert len(ledger) == 1


class TestPhase2Regressions:
    """Phase 2 storage-move regressions (no behavior drift)."""

    def test_round_trip_append_get_ledger_keeps_identity_and_order(self) -> None:
        metadata: Dict[str, Any] = {}

        append(
            metadata,
            turn_sequence=21,
            source="ptr",
            payload={
                "sections": [
                    {
                        "heading": "PTR Decision",
                        "body": "PTR found filtered service state",
                    }
                ],
            },
        )
        append(
            metadata,
            turn_sequence=21,
            source="tool",
            payload={
                "sections": [
                    {
                        "heading": "Tool Output Summary",
                        "body": "No reachable FTP endpoint",
                    }
                ],
            },
        )

        assert get_current_turn_scope(metadata) == 21
        ledger = get_ledger(metadata)
        assert len(ledger) == 2
        assert [record["phase_sequence"] for record in ledger] == [0, 1]
        assert [record["source"] for record in ledger] == ["ptr", "tool"]
        assert ledger[0]["sections"] == [
            {"heading": "PTR Decision", "body": "PTR found filtered service state"}
        ]
        assert ledger[1]["sections"] == [
            {"heading": "Tool Output Summary", "body": "No reachable FTP endpoint"}
        ]

    def test_ledger_stays_lossless_above_fifty_records(self) -> None:
        metadata: Dict[str, Any] = {}

        for idx in range(75):
            append(
                metadata,
                turn_sequence=33,
                source="tool" if idx % 2 else "ptr",
                payload={
                    "sections": [
                        {
                            "heading": f"Heading {idx}",
                            "body": f"summary-{idx}\nresult-{idx}",
                        }
                    ],
                },
            )

        ledger = get_ledger(metadata)
        assert len(ledger) == 75
        assert latest_recorded_phase_sequence(metadata, turn_sequence=33) == 74
        assert ledger[0]["sections"] == [
            {"heading": "Heading 0", "body": "summary-0\nresult-0"}
        ]
        assert ledger[-1]["sections"] == [
            {"heading": "Heading 74", "body": "summary-74\nresult-74"}
        ]


class TestLatestPhaseMemorySection:
    """Latest-phase renderer narrows the ledger without changing phase markup."""

    def test_empty_or_missing_ledger_returns_empty_string(self) -> None:
        assert render_latest_phase_memory_section({}, turn_sequence=1) == ""

    def test_ignores_old_turn_records(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=1,
                phase_sequence=5,
                source="tool",
                sections=[("Tool Output Summary", "old turn")],
            )
        )

        assert render_latest_phase_memory_section(metadata, turn_sequence=2) == ""

    def test_selects_latest_phase_sequence_for_current_turn(self) -> None:
        metadata = _metadata_with_phase_records(
            _phase_record(
                turn_sequence=4,
                phase_sequence=0,
                source="tool",
                sections=[("Tool Output Summary", "first phase")],
            ),
            _phase_record(
                turn_sequence=4,
                phase_sequence=2,
                source="reflect",
                sections=[("Reflection", "latest phase")],
            ),
            _phase_record(
                turn_sequence=3,
                phase_sequence=9,
                source="tool",
                sections=[("Tool Output Summary", "other turn")],
            ),
        )

        rendered = render_latest_phase_memory_section(metadata, turn_sequence=4)

        assert "## Latest Current-Turn Phase" in rendered
        assert "<phase turn=4 phase=2 source=reflect>" in rendered
        assert "## Reflection\nlatest phase" in rendered
        assert "first phase" not in rendered
        assert "other turn" not in rendered


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    pytest.main([__file__, "-v"])
