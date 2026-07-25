"""Unit tests for deterministic compact-output item budgeting."""

from __future__ import annotations

import pytest

from agent.graph.compression.deterministic.budget import budget_rendered_items
from core.prompts.constants import (
    COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS,
    POST_TOOL_MAX_SUMMARY_CHARS,
)


def test_compact_key_findings_total_budget_matches_ptr_allocation() -> None:
    """The shared key-findings budget fits inside PTR's section allocation."""

    assert COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS == 4000
    assert COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS <= POST_TOOL_MAX_SUMMARY_CHARS


def test_budget_preserves_complete_items_at_exact_boundaries() -> None:
    """Items exactly at both configured boundaries remain complete."""

    result = budget_rendered_items(
        ["aa", "bbb"],
        max_items=2,
        max_characters=6,
        omission_label="items",
    )

    assert result.items == ("aa", "bbb")
    assert result.total == 2
    assert result.shown == 2
    assert result.omitted == 0
    assert result.total_characters == 6
    assert result.shown_characters == 6
    assert result.rendered_characters == 6
    assert result.omission_marker is None
    assert result.is_complete is True


def test_budget_over_item_limit_reserves_exact_omission_marker() -> None:
    """The omission marker consumes one item slot and reports exact counts."""

    result = budget_rendered_items(
        ["one", "two", "three", "four"],
        max_items=3,
        max_characters=80,
        omission_label="details",
    )

    assert result.items == (
        "one",
        "two",
        "details omitted: showing 2 of 4; omitted 2.",
    )
    assert result.total == 4
    assert result.shown == 2
    assert result.omitted == 2
    assert result.omission_marker == "details omitted: showing 2 of 4; omitted 2."
    assert result.is_complete is False


def test_budget_over_character_limit_is_bounded_and_deterministic() -> None:
    """Character pressure is deterministic and leaves space for the marker."""

    first = budget_rendered_items(
        ["alpha", "bravo", "charlie" * 10],
        max_items=10,
        max_characters=50,
        omission_label="findings",
    )
    second = budget_rendered_items(
        ["alpha", "bravo", "charlie" * 10],
        max_items=10,
        max_characters=50,
        omission_label="findings",
    )

    assert first == second
    assert first.items == (
        "alpha",
        "findings omitted: showing 1 of 3; omitted 2.",
    )
    assert first.rendered_characters <= 50
    assert first.total == 3
    assert first.shown == 1
    assert first.omitted == 2


def test_budget_can_emit_marker_only_without_silent_omission() -> None:
    """If no detail item fits, the exact omission marker is still returned."""

    result = budget_rendered_items(
        ["alpha", "bravo"],
        max_items=1,
        max_characters=50,
        omission_label="dns details",
    )

    assert result.items == ("dns details omitted: showing 0 of 2; omitted 2.",)
    assert result.shown == 0
    assert result.omitted == 2
    assert result.rendered_characters <= 50


def test_budget_rejects_limits_that_cannot_hold_marker() -> None:
    """Invalid budgets fail loudly instead of omitting facts silently."""

    with pytest.raises(ValueError, match="max_characters"):
        budget_rendered_items(
            ["alpha", "bravo"],
            max_items=1,
            max_characters=5,
            omission_label="dns details",
        )
