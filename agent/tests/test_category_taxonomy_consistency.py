"""Consistency checks for canonical tool-category taxonomy and runtime registry."""

from __future__ import annotations

from agent.tools.category_utils import get_tool_categories
from core.tool_category_taxonomy import (
    find_missing_descriptions,
    get_canonical_categories,
    get_category_descriptions,
)


def test_runtime_categories_have_descriptions_and_match_canonical_taxonomy() -> None:
    runtime_categories = sorted(get_tool_categories())
    canonical_categories = sorted(get_canonical_categories())
    descriptions = get_category_descriptions()

    assert find_missing_descriptions(runtime_categories, descriptions=descriptions) == []
    assert set(runtime_categories).issubset(set(canonical_categories))
    assert sorted(cat for cat in descriptions if cat in runtime_categories) == runtime_categories
    assert sorted(descriptions.keys()) == canonical_categories
