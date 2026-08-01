"""Tests for validated, deterministic final-answer rendering."""

from __future__ import annotations

import json

import pytest

from agent.graph.nodes.final_answer_contract import render_final_answer
from agent.providers.llm.core.exceptions import LLMResponseError


def _sections() -> dict[str, str]:
    return {
        "action": "Ran a bounded scan against 10.0.0.5.",
        "findings": "- 443/tcp was confirmed open.\n- TLS was detected.",
        "impact": "The HTTPS service is reachable from the tested path.",
        "recommended_next_action": "Enumerate HTTPS on 10.0.0.5:443.",
    }


def test_markdown_renderer_owns_all_user_visible_headings() -> None:
    rendered = render_final_answer(_sections())

    assert rendered.count("## ") == 4
    assert rendered.startswith("## Action\n")
    assert "\n\n## Findings\n" in rendered
    assert rendered.endswith("Enumerate HTTPS on 10.0.0.5:443.")


def test_json_renderer_serializes_only_contract_fields() -> None:
    rendered = render_final_answer(_sections(), output_format="json")
    payload = json.loads(rendered.removeprefix("```json\n").removesuffix("\n```"))

    assert list(payload) == [
        "action",
        "findings",
        "impact",
        "recommended_next_action",
    ]


@pytest.mark.parametrize(
    "payload",
    (
        None,
        {"action": "only one field"},
        {**_sections(), "extra": "untrusted preamble"},
        {**_sections(), "action": "## Injected Heading\ncontent"},
    ),
)
def test_renderer_fails_closed_for_invalid_provider_payload(payload: object) -> None:
    with pytest.raises(LLMResponseError):
        render_final_answer(payload)
