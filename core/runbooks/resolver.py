"""Resolve loaded tool runbooks for selected tools and prompt stages."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from core.runbooks.models import LoadedRunbook, RunbookStage, RunbookType


def resolve_for_tools(
    *,
    runbooks: Sequence[LoadedRunbook],
    selected_tools: Iterable[str],
    stage: RunbookStage,
) -> list[LoadedRunbook]:
    """Return tool runbooks matching selected tool ids and stage."""

    selected_tool_ids = set(selected_tools)
    if not selected_tool_ids:
        return []

    return [
        runbook
        for runbook in runbooks
        if runbook.type is RunbookType.TOOL
        and stage in runbook.stages
        and any(tool_id in selected_tool_ids for tool_id in runbook.trigger_tool_ids)
    ]


def resolve_for_categories(
    *,
    runbooks: Sequence[LoadedRunbook],
    selected_categories: Iterable[str],
    stage: RunbookStage,
) -> list[LoadedRunbook]:
    """Return tool runbooks matching selected category ids and stage."""

    selected_category_ids = set(selected_categories)
    if not selected_category_ids:
        return []

    return [
        runbook
        for runbook in runbooks
        if runbook.type is RunbookType.TOOL
        and stage in runbook.stages
        and any(
            category_id in selected_category_ids
            for category_id in runbook.trigger_category_ids
        )
    ]


__all__ = ["resolve_for_categories", "resolve_for_tools"]
