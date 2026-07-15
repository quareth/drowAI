"""Regression tests for category-selector taxonomy consistency and prompt rendering.

Phase 3 Task 3.1 removed the transcript-driven ``history_text`` input
from ``_build_category_selection_prompt``; these tests now exercise the
brief-only signature.
"""

from __future__ import annotations

import pytest

from agent.graph.nodes.select_tool_categories import _build_category_selection_prompt
from core.tool_category_taxonomy import get_category_descriptions


def test_category_prompt_uses_canonical_labels_without_placeholder() -> None:
    prompt = _build_category_selection_prompt(
        available_categories=[
            "information_gathering",
            "database_assessment",
            "vulnerability_analysis",
        ],
        category_descriptions=get_category_descriptions(),
        next_tool_hint=None,
        intent_brief={"resolved_user_intent": "check postgres cves"},
    )

    assert "Tools in this category" not in prompt
    assert '"web_application"' not in prompt
    assert '"exploitation"' not in prompt
    assert '"reporting"' not in prompt


def test_category_prompt_raises_when_description_missing() -> None:
    with pytest.raises(ValueError, match="Missing category descriptions"):
        _build_category_selection_prompt(
            available_categories=["information_gathering", "database_assessment"],
            category_descriptions={"information_gathering": "Recon tools"},
            next_tool_hint=None,
            intent_brief={"resolved_user_intent": "scan target"},
        )
