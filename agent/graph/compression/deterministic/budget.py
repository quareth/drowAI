"""Pure item budgeting for deterministic compact-output projections.

This module owns tool-neutral selection of already-rendered deterministic
details into bounded compact fields. It does not sort, deduplicate, interpret
tool semantics, call LLMs, or perform runtime side effects.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BudgetedItems:
    """Selected rendered items plus exact budget and omission accounting."""

    items: tuple[str, ...]
    total: int
    shown: int
    omitted: int
    total_characters: int
    shown_characters: int
    rendered_characters: int
    omission_marker: str | None = None

    @property
    def is_complete(self) -> bool:
        """Return whether every normalized input item was selected."""

        return self.omitted == 0


def budget_rendered_items(
    items: Iterable[str],
    *,
    max_items: int,
    max_characters: int,
    omission_label: str,
) -> BudgetedItems:
    """Return a bounded prefix of rendered items plus exact omission metadata.

    ``max_items`` counts the omission marker when one is needed.
    ``max_characters`` counts rendered items joined with newline separators,
    matching how compact key findings are later rendered into prompt text.
    """

    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    if max_characters < 1:
        raise ValueError("max_characters must be at least 1")

    normalized_items = tuple(
        text for item in items if (text := str(item or "").strip())
    )
    total = len(normalized_items)
    total_characters = _rendered_characters(normalized_items)
    complete = total <= max_items and total_characters <= max_characters
    if complete:
        return BudgetedItems(
            items=normalized_items,
            total=total,
            shown=total,
            omitted=0,
            total_characters=total_characters,
            shown_characters=total_characters,
            rendered_characters=total_characters,
        )

    marker_only = _omission_marker(
        label=omission_label,
        shown=0,
        total=total,
        omitted=total,
    )
    if _rendered_characters((marker_only,)) > max_characters:
        raise ValueError("max_characters must allow the omission marker")

    max_detail_items = max(max_items - 1, 0)
    selected: list[str] = []
    for item in normalized_items:
        if len(selected) >= max_detail_items:
            break
        candidate = (*selected, item)
        omitted = total - len(candidate)
        marker = _omission_marker(
            label=omission_label,
            shown=len(candidate),
            total=total,
            omitted=omitted,
        )
        if _rendered_characters((*candidate, marker)) > max_characters:
            break
        selected.append(item)

    shown = len(selected)
    omitted = total - shown
    marker = _omission_marker(
        label=omission_label,
        shown=shown,
        total=total,
        omitted=omitted,
    )
    rendered_items = (*selected, marker)

    return BudgetedItems(
        items=rendered_items,
        total=total,
        shown=shown,
        omitted=omitted,
        total_characters=total_characters,
        shown_characters=_rendered_characters(selected),
        rendered_characters=_rendered_characters(rendered_items),
        omission_marker=marker,
    )


def _omission_marker(
    *,
    label: str,
    shown: int,
    total: int,
    omitted: int,
) -> str:
    """Return the exact omission marker included in compact findings."""

    normalized_label = str(label or "items").strip() or "items"
    return f"{normalized_label} omitted: showing {shown} of {total}; omitted {omitted}."


def _rendered_characters(items: Iterable[str]) -> int:
    """Return character count after newline-separated prompt rendering."""

    return len("\n".join(tuple(items)))


__all__ = ["BudgetedItems", "budget_rendered_items"]
