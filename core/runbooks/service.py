"""Facade for resolving and rendering validated runbooks for prompt stages."""

from __future__ import annotations

from collections.abc import Sequence

from core.runbooks.models import RunbookStage
from core.runbooks.registry import RunbookRegistry
from core.runbooks.renderer import render_runbooks
from core.runbooks.resolver import resolve_for_categories, resolve_for_tools


class RunbookService:
    """Resolve and render validated runbooks for prompt stages."""

    def __init__(self, *, registry: RunbookRegistry | None = None) -> None:
        self._registry = registry or RunbookRegistry()

    def render_for_tools(
        self,
        *,
        selected_tools: Sequence[str],
        stage: RunbookStage,
    ) -> str:
        """Return prompt-ready runbook text for selected tools and stage."""

        if not selected_tools:
            return ""

        runbooks = self._registry.load_builtin_tool_runbooks()
        resolved_runbooks = resolve_for_tools(
            runbooks=runbooks,
            selected_tools=selected_tools,
            stage=stage,
        )
        return render_runbooks(resolved_runbooks, stage=stage)

    def render_for_categories(
        self,
        *,
        selected_categories: Sequence[str],
        stage: RunbookStage,
    ) -> str:
        """Return prompt-ready runbook text for selected categories and stage."""

        if not selected_categories:
            return ""

        runbooks = self._registry.load_builtin_tool_runbooks()
        resolved_runbooks = resolve_for_categories(
            runbooks=runbooks,
            selected_categories=selected_categories,
            stage=stage,
        )
        return render_runbooks(resolved_runbooks, stage=stage)


__all__ = ["RunbookService"]
