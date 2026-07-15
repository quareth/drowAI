"""Tests structured-output handling for tool category selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.graph.nodes import select_tool_categories as selector_module


class _StubSelectorClient:
    def __init__(self, *, structured_output: dict | None, content: str = "{}") -> None:
        self.structured_output = structured_output
        self.content = content

    async def chat_with_usage(self, *args, **kwargs):  # type: ignore[override]
        return SimpleNamespace(
            content=self.content,
            usage=None,
            structured_output=self.structured_output,
        )


@pytest.mark.asyncio
async def test_call_llm_for_categories_prefers_structured_payload(monkeypatch) -> None:
    stub = _StubSelectorClient(
        structured_output={
            "selected_categories": ["information_gathering"],
            "reasoning": "Network discovery intent.",
        },
        content="this is not json",
    )
    monkeypatch.setattr(selector_module, "resolve_llm_client", lambda *args, **kwargs: stub)

    selected = await selector_module._call_llm_for_categories(
        prompt="pick categories",
        model="gpt-5-mini",
        available_categories=["information_gathering", "database_assessment"],
        interactive=None,
    )

    assert selected == ["information_gathering"]


@pytest.mark.asyncio
async def test_call_llm_for_categories_invalid_structured_categories_fallbacks(monkeypatch) -> None:
    stub = _StubSelectorClient(
        structured_output={
            "selected_categories": ["nonexistent_category"],
            "reasoning": "Bad category.",
        }
    )
    monkeypatch.setattr(selector_module, "resolve_llm_client", lambda *args, **kwargs: stub)

    selected = await selector_module._call_llm_for_categories(
        prompt="pick categories",
        model="gpt-5-mini",
        available_categories=["information_gathering", "database_assessment"],
        interactive=None,
    )

    assert selected == ["information_gathering"]
